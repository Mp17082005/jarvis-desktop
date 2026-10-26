"""Core tests for the installable voice app (jarvis_desktop): pure logic only, no mic/speaker/network."""
import io
import json
import threading
import time
import wave

import httpx
import numpy as np
import pytest

SR = 16000


# ---------------------------------------------------------------- helpers
def noise(seconds, amp=0.002, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(SR * seconds)) * amp).astype(np.float32)


def tone(seconds, amp=0.3, freq=220.0):
    t = np.arange(int(SR * seconds)) / SR
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.float32)


def feed_all(ep, signal, chunk=1600):
    out = []
    for i in range(0, len(signal), chunk):
        out.extend(ep.feed(signal[i:i + chunk]))
    return out


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DESKTOP_HOME", str(tmp_path / "Jarvis"))
    monkeypatch.delenv("JARVIS_LLM_API_KEY", raising=False)
    from jarvis_desktop import config
    kr = FakeKeyring()
    monkeypatch.setattr(config, "_keyring", lambda: kr)
    return tmp_path / "Jarvis", kr


# ---------------------------------------------------------------- endpointing
def test_endpointer_detects_one_utterance_after_900ms_silence():
    from jarvis_desktop.audio import Endpointer
    ep = Endpointer(sample_rate=SR)
    ep.calibrate(noise(1.0))
    sig = np.concatenate([noise(0.5, seed=1), tone(1.2) + noise(1.2, seed=2), noise(0.5, seed=3)])
    assert feed_all(ep, sig) == []  # only 500 ms of silence so far: not ended yet
    out = feed_all(ep, noise(0.5, seed=4))  # total trailing silence 1.0 s > 900 ms
    assert len(out) == 1
    dur = len(out[0]) / SR
    assert 1.2 <= dur <= 2.0, dur  # speech + pre-roll + trimmed tail


def test_endpointer_discards_short_clicks_and_noise():
    from jarvis_desktop.audio import Endpointer
    ep = Endpointer(sample_rate=SR)
    ep.calibrate(noise(1.0))
    sig = np.concatenate([noise(0.5), tone(0.15), noise(1.5, seed=5)])
    assert feed_all(ep, sig) == []
    assert feed_all(ep, noise(3.0, seed=6)) == []


def test_endpointer_caps_utterance_at_max_seconds():
    from jarvis_desktop.audio import Endpointer
    ep = Endpointer(sample_rate=SR, max_seconds=30.0)
    ep.calibrate(noise(1.0))
    syllables = np.concatenate([np.concatenate([tone(0.4), noise(0.2, seed=i)]) for i in range(55)])  # 33 s
    out = feed_all(ep, syllables)
    assert len(out) == 1
    assert len(out[0]) / SR <= 30.5


def test_endpointer_noise_floor_adapts_to_louder_room():
    from jarvis_desktop.audio import Endpointer
    ep = Endpointer(sample_rate=SR)
    ep.calibrate(noise(1.0, amp=0.001))
    quiet = ep.noise_floor
    feed_all(ep, noise(6.0, amp=0.004, seed=9))  # a step change may yield one segment while adapting
    assert ep.noise_floor > quiet * 2
    # once adapted, steady louder room noise never becomes an utterance
    assert feed_all(ep, noise(5.0, amp=0.004, seed=10)) == []


def test_microphone_listen_uses_injected_frames_and_times_out():
    from jarvis_desktop.audio import Microphone
    frames = list(np.split(noise(1.0), 10)) + list(np.split(np.concatenate([tone(1.0), noise(1.2, seed=3)]), 22))
    src = iter(frames)
    mic = Microphone(read_frame=lambda timeout: next(src, None), calibrate_seconds=1.0)
    pcm = mic.listen(timeout=5)
    assert pcm is not None and 0.9 <= len(pcm) / SR <= 2.0
    silent = Microphone(read_frame=lambda timeout: noise(0.1), calibrate_seconds=0.2)
    t0 = time.monotonic()
    assert silent.listen(timeout=0.5) is None
    assert time.monotonic() - t0 < 3


def test_listen_max_seconds_is_per_call_and_close_keeps_calibration():
    from jarvis_desktop.audio import Microphone
    src = iter(list(np.split(noise(1.0), 10)) + [tone(0.1)] * 60)
    mic = Microphone(read_frame=lambda timeout: next(src, None), calibrate_seconds=1.0)
    full = mic.endpointer.max_frames
    pcm = mic.listen(timeout=5, max_seconds=3)
    assert pcm is not None and len(pcm) / SR <= 3.1
    assert mic.endpointer.max_frames == full
    mic.close()
    assert mic._calibrated


# ---------------------------------------------------------------- audio decode
def _wav(pcm_int16, sr=SR):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm_int16.tobytes())
    return buf.getvalue()


def test_decode_wav_and_resample_to_16k_float():
    from jarvis_desktop.audio import decode_to_float, to_wav_bytes
    pcm = (tone(0.5, amp=0.5, freq=440) * 32767).astype(np.int16)
    x = decode_to_float(_wav(np.repeat(pcm, 2)[: len(pcm) * 2], sr=32000), target_rate=SR)
    assert abs(len(x) / SR - 0.5) < 0.02 and x.dtype == np.float32
    w = to_wav_bytes(x, SR)
    assert w[:4] == b"RIFF"


def test_player_uses_backend_and_interrupts():
    from jarvis_desktop.audio import Player
    calls = []

    class Backend:
        def play_file(self, path):
            calls.append(("play", path)); return True

        def stop(self):
            calls.append(("stop",))

    p = Player(backend=Backend())
    assert p.play(_wav((tone(2.0) * 30000).astype(np.int16)), block=False)
    assert p.is_playing
    p.interrupt()
    assert not p.is_playing and calls[-1] == ("stop",)


# ---------------------------------------------------------------- yes/no
@pytest.mark.parametrize("text", ["yes", "Yes.", "yeah go ahead", "do it", "okay", "OK", "haan", "हाँ",
                                   "haan kar do", "sure, go ahead", "yes please", "ji haan", "confirm"])
def test_confirm_classifier_accepts_clear_yes(text):
    from jarvis_desktop.confirm import is_clear_yes
    assert is_clear_yes(text) is True


@pytest.mark.parametrize("text", ["", "no", "nahi", "नहीं", "don't do it", "yes no", "maybe", "wait",
                                   "not okay", "what?", "cancel", "stop", "yes but first tell me the weather",
                                   "mat karo", "I said the word yes yesterday in the meeting with the team"])
def test_confirm_classifier_rejects_everything_else(text):
    from jarvis_desktop.confirm import is_clear_yes
    assert is_clear_yes(text) is False


def test_spoken_confirm_speaks_listens_once_and_defaults_no():
    from jarvis_desktop.confirm import SpokenConfirm
    said = []
    c = SpokenConfirm(speak=said.append, listen=lambda timeout: np.ones(16000, np.float32),
                      transcribe=lambda pcm: "go ahead")
    assert c("Send the email to Bob?") is True and said == ["Send the email to Bob?"]
    assert SpokenConfirm(speak=said.append, listen=lambda timeout: None, transcribe=lambda p: "yes")("x?") is False

    def boom(_):
        raise RuntimeError("stt down")
    assert SpokenConfirm(speak=said.append, listen=lambda timeout: np.ones(8, np.float32), transcribe=boom)("x?") is False


# ---------------------------------------------------------------- sanitizer
def test_sanitize_strips_markdown_emoji_urls_json():
    from jarvis_desktop.tts import sanitize_for_speech
    s = sanitize_for_speech("## Result\n**Done!** 🎉 See https://example.com/x?y=1 and `code`.\n- item one\n- item two")
    assert "#" not in s and "*" not in s and "http" not in s and "🎉" not in s and "`" not in s
    assert "Done!" in s and "item one" in s and "item two" in s
    assert sanitize_for_speech('{"a": 1, "b": [2]}') == ""
    assert sanitize_for_speech("[the docs](https://x.y/z) are here") == "the docs are here"
    assert sanitize_for_speech("नमस्ते, काम हो गया।") == "नमस्ते, काम हो गया।"


def test_provider_speech_retries_without_rejected_voice():
    from jarvis_desktop.tts import provider_speech, TTSError
    bodies = []

    def handler(r):
        b = json.loads(r.content); bodies.append(b)
        if b.get("voice") == "nova-custom":
            return httpx.Response(400, json={"error": "unknown voice"})
        return httpx.Response(200, content=b"ID3fakeaudio", headers={"content-type": "audio/mpeg"})
    assert provider_speech("https://p/v1", "k", "tts-x", "nova-custom", "hi", transport=httpx.MockTransport(handler)) == b"ID3fakeaudio"
    assert [b.get("voice") for b in bodies] == ["nova-custom", "alloy"]
    bodies.clear()
    provider_speech("https://p/v1", "k", "tts-x", "", "hi", transport=httpx.MockTransport(handler))
    assert [b.get("voice") for b in bodies] == ["alloy"]  # blank voice: provider default
    with pytest.raises(TTSError):
        provider_speech("https://p/v1", "k", "m", "v", "hi",
                        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="k leaked?")))


def test_transcriber_provider_mode_falls_back_to_local():
    from jarvis_desktop.config import Settings
    from jarvis_desktop.stt import ProviderSTT, Transcriber

    class Local:
        def transcribe(self, pcm):
            return "local text"
    ok = ProviderSTT("https://p/v1", lambda: "k", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"text": " provider text "})))
    t = Transcriber(Settings(stt_mode="provider"), lambda: "k", local=Local(), provider=ok)
    assert t.transcribe(np.zeros(16000, np.float32)) == "provider text"
    bad = ProviderSTT("https://p/v1", lambda: "k", transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    t = Transcriber(Settings(stt_mode="provider"), lambda: "k", local=Local(), provider=bad)
    assert t.transcribe(np.zeros(16000, np.float32)) == "local text"


def test_command_phrases():
    from jarvis_desktop.app import classify_command
    assert classify_command("Stop.") == "stop" and classify_command("ruko") == "stop"
    assert classify_command("stop the music in the kitchen") is None
    assert classify_command("Goodbye Jarvis") == "goodbye" and classify_command("go to sleep") == "sleep"


# ---------------------------------------------------------------- config
def test_config_round_trip_and_key_only_in_keyring(home):
    from jarvis_desktop import config
    root, kr = home
    assert not config.is_configured()
    s = config.Settings(base_url="https://llm.example/v1/", chat_model="m1", autonomy="careful")
    config.save_settings(s)
    config.set_api_key("sk-secret-123")
    raw = (root / "settings.json").read_text()
    assert "sk-secret" not in raw
    loaded = config.load_settings()
    assert loaded.base_url == "https://llm.example/v1" and loaded.chat_model == "m1" and loaded.autonomy == "careful"
    assert kr.store[("Jarvis", "provider_api_key")] == "sk-secret-123"
    assert config.get_api_key() == "sk-secret-123" and config.is_configured()
    config.delete_api_key()
    assert config.get_api_key() is None


def test_config_env_key_override_and_invalid_values_fall_back(home, monkeypatch):
    from jarvis_desktop import config
    root, _ = home
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.json").write_text(json.dumps({"autonomy": "reckless", "stt_mode": "x", "junk": 1,
                                                    "api_key": "never-read"}))
    s = config.load_settings()
    assert s.autonomy == "balanced" and s.stt_mode == "local" and not hasattr(s, "junk")
    monkeypatch.setenv("JARVIS_LLM_API_KEY", "env-key")
    assert config.get_api_key() == "env-key"
    assert config.key_source() == "environment"


# ---------------------------------------------------------------- setup
def _provider(seen):
    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m1"}]})
        body = json.loads(request.content)
        assert body["max_tokens"] == 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "h"}}]})
    return httpx.MockTransport(handler)


def test_test_connection_success_and_failure_never_echo_key():
    from jarvis_desktop.setup_wizard import test_connection
    seen = []
    ok, msg = test_connection("https://p.example/v1", "sk-abc", "m1", transport=_provider(seen))
    assert ok and "sk-abc" not in msg
    assert [s[:2] for s in seen] == [("GET", "/v1/models"), ("POST", "/v1/chat/completions")]
    assert seen[0][2] == "Bearer sk-abc"
    bad = httpx.MockTransport(lambda r: httpx.Response(401, text="invalid key sk-abc"))
    ok, msg = test_connection("https://p.example/v1", "sk-abc", "m1", transport=bad)
    assert not ok and "sk-abc" not in msg and "401" in msg


def test_setup_non_interactive_reads_key_from_env(home, monkeypatch):
    from jarvis_desktop import config
    from jarvis_desktop.__main__ import main
    root, kr = home
    monkeypatch.setenv("JARVIS_LLM_API_KEY", "sk-from-env")
    rc = main(["--setup", "--non-interactive", "--base-url", "https://p.example/v1", "--model", "m9",
               "--autonomy", "autonomous"])
    assert rc == 0
    s = config.load_settings()
    assert (s.base_url, s.chat_model, s.autonomy) == ("https://p.example/v1", "m9", "autonomous")
    assert kr.store[("Jarvis", "provider_api_key")] == "sk-from-env"
    assert "sk-from-env" not in (root / "settings.json").read_text()


def test_setup_non_interactive_without_key_fails(home, capsys):
    from jarvis_desktop.__main__ import main
    assert main(["--setup", "--non-interactive", "--base-url", "https://p/v1", "--model", "m"]) == 2


def test_setup_with_claude_key_saves_provider_and_offline_voice(home, monkeypatch):
    from jarvis_desktop import config
    from jarvis_desktop.__main__ import main
    root, kr = home
    monkeypatch.setenv("JARVIS_LLM_API_KEY", "sk-ant-abc")
    assert main(["--setup", "--non-interactive", "--model", "claude-sonnet-4-5"]) == 0
    s = config.load_settings()
    assert (s.provider, s.base_url, s.chat_model) == ("anthropic", "https://api.anthropic.com", "claude-sonnet-4-5")
    assert s.tts_model == ""  # no speech endpoint at Anthropic: offline Windows voice


def test_setup_ollama_needs_no_key(home, monkeypatch):
    from jarvis_desktop import config
    from jarvis_desktop.__main__ import main
    monkeypatch.delenv("JARVIS_LLM_API_KEY", raising=False)
    assert main(["--setup", "--non-interactive", "--provider", "ollama", "--model", "llama3.1"]) == 0
    s = config.load_settings()
    assert (s.provider, s.base_url) == ("ollama", "http://localhost:11434/v1")
    assert config.is_configured()


def test_setup_test_runs_chat_and_tool_check(home, monkeypatch, capsys):
    from jarvis_desktop import setup_wizard
    from jarvis_desktop.__main__ import main
    monkeypatch.setenv("JARVIS_LLM_API_KEY", "sk-x")
    calls = []
    monkeypatch.setattr(setup_wizard, "verify", lambda kind, base, key, model, transport=None:
                        (calls.append((kind, base, model)), (False, "OpenAI returned HTTP 401: the API key was rejected.", {}))[1])
    assert main(["--setup", "--non-interactive", "--provider", "openai", "--model", "gpt-4o-mini", "--test"]) == 1
    assert calls == [("openai", "https://api.openai.com/v1", "gpt-4o-mini")]
    assert "rejected" in capsys.readouterr().out


# ---------------------------------------------------------------- state machine
class FakeWake:
    def __init__(self):
        self.fired = False; self.started = 0; self.stopped = 0

    def start(self):
        self.started += 1; return True

    def stop(self):
        self.stopped += 1

    def pop_event(self):
        f, self.fired = self.fired, False
        return f


class FakeMic:
    """listen() returns scripted utterances (as strings tagged in an array) or None."""

    def __init__(self, script):
        self.script = list(script); self.flushed = 0; self.closed = 0

    def listen(self, timeout, max_seconds=None):
        if not self.script:
            return None
        item = self.script.pop(0)
        return None if item is None else item

    def flush(self):
        self.flushed += 1

    def close(self):
        self.closed += 1


class FakeSTT:
    def transcribe(self, pcm):
        return pcm  # scripts pass text straight through


class FakeSpeaker:
    def __init__(self):
        self.said = []; self.interrupted = 0; self._playing = False

    def say(self, text, block=True):
        self.said.append(text)

    def interrupt(self):
        self.interrupted += 1

    @property
    def is_playing(self):
        return False


class FakeAgent:
    def __init__(self, delay=0.0, reply="It is sunny."):
        self.delay = delay; self.reply = reply; self.seen = []; self.cb = None

    def handle(self, text):
        self.seen.append(text); time.sleep(self.delay); return self.reply

    def on_event(self, cb):
        self.cb = cb

    def close(self):
        pass


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make_app(script, agent=None, clock=None):
    from jarvis_desktop.app import VoiceApp
    agent = agent or FakeAgent()
    app = VoiceApp(agent=agent, speaker=FakeSpeaker(), mic=FakeMic(script), stt=FakeSTT(), wake=FakeWake(),
                   clock=clock or time.monotonic, sleep=lambda s: None, filler_after=1.5)
    return app


def test_wake_listen_think_speak_then_follow_up_without_rewake():
    from jarvis_desktop.app import State
    app = make_app(["what's the weather", "and tomorrow?"])
    assert app.state is State.IDLE
    app.step()  # no wake yet
    assert app.state is State.IDLE and app.wake.started == 1
    app.wake.fired = True
    app.step()
    assert app.state is State.LISTENING and app.wake.stopped >= 1  # wake releases mic before capture
    app.step(); assert app.state is State.THINKING
    app.step(); assert app.state is State.SPEAKING
    app.step(); assert app.state is State.LISTENING
    assert app.speaker.said[-1] == "It is sunny." and app.mic.flushed >= 1
    app.step(); app.step(); app.step()
    assert app.agent.seen == ["what's the weather", "and tomorrow?"]
    assert app.state is State.LISTENING


def test_follow_up_window_expires_to_idle_after_20s():
    from jarvis_desktop.app import State
    clock = Clock()
    app = make_app([None, None], clock=clock)
    app.wake.fired = True
    app.step(); assert app.state is State.LISTENING
    clock.t = 5; app.step(); assert app.state is State.LISTENING
    clock.t = 21; app.step(); assert app.state is State.IDLE


@pytest.mark.parametrize("phrase", ["stop listening", "Go to sleep.", "goodbye jarvis", "Goodbye, Jarvis!"])
def test_sleep_and_goodbye_phrases_return_to_idle_without_agent(phrase):
    from jarvis_desktop.app import State
    app = make_app([phrase])
    app.wake.fired = True
    app.step(); app.step()
    assert app.state is State.IDLE and app.agent.seen == []


def test_filler_only_when_agent_slow():
    fast = make_app(["hi"], agent=FakeAgent(delay=0.0))
    fast.wake.fired = True
    for _ in range(4):
        fast.step()
    assert "On it." not in fast.speaker.said
    slow = make_app(["hi"], agent=FakeAgent(delay=0.3))
    slow.filler_after = 0.1
    slow.wake.fired = True
    for _ in range(4):
        slow.step()
    assert slow.speaker.said[0] == "On it." and slow.speaker.said[-1] == "It is sunny."


def test_agent_error_is_spoken_plainly():
    class Bad(FakeAgent):
        def handle(self, text):
            raise RuntimeError("boom secret trace")
    app = make_app(["hi"], agent=Bad())
    app.wake.fired = True
    for _ in range(4):
        app.step()
    assert "boom" not in app.speaker.said[-1] and "sorry" in app.speaker.said[-1].lower()


def test_proactive_announcements_spoken_when_idle_and_between_turns():
    from jarvis_desktop.app import State
    agent = FakeAgent()
    app = make_app(["hi"], agent=agent)
    agent.cb("Your report job finished.")
    app.step()
    assert app.speaker.said == ["Your report job finished."] and app.state is State.IDLE
    app.wake.fired = True
    app.step(); app.step()  # LISTENING -> THINKING
    threading.Thread(target=agent.cb, args=("Download done.",)).start(); time.sleep(0.05)
    app.step(); app.step()  # THINKING -> SPEAKING -> LISTENING
    assert app.speaker.said[-2:] == ["It is sunny.", "Download done."]


def test_stop_during_speech_interrupts_playback():
    from jarvis_desktop.app import VoiceApp

    class PlayingSpeaker(FakeSpeaker):
        def __init__(self):
            super().__init__(); self.n = 3

        @property
        def is_playing(self):
            self.n -= 1
            return self.n > 0 and not self.interrupted

    sp = PlayingSpeaker()
    app = VoiceApp(agent=FakeAgent(), speaker=sp, mic=FakeMic(["hi", "stop"]), stt=FakeSTT(), wake=FakeWake(),
                   sleep=lambda s: None)
    app.wake.fired = True
    for _ in range(4):
        app.step()
    assert sp.interrupted == 1


def test_echo_during_speech_is_discarded_not_sent_to_agent():
    from jarvis_desktop.app import VoiceApp

    class PlayingSpeaker(FakeSpeaker):
        def __init__(self):
            super().__init__(); self.n = 3

        @property
        def is_playing(self):
            self.n -= 1
            return self.n > 0

    agent = FakeAgent()
    app = VoiceApp(agent=agent, speaker=PlayingSpeaker(), mic=FakeMic(["hi", "yes go ahead and send it"]),
                   stt=FakeSTT(), wake=FakeWake(), sleep=lambda s: None)
    app.wake.fired = True
    for _ in range(6):
        app.step()
    assert agent.seen == ["hi"]


def test_pause_stops_wake_and_resume_restarts():
    from jarvis_desktop.app import State
    app = make_app([])
    app.step()
    app.set_paused(True)
    app.step()
    assert app.wake.stopped >= 1 and app.state is State.IDLE
    started = app.wake.started
    app.wake.fired = True
    app.step()
    assert app.state is State.IDLE and app.wake.started == started
    app.set_paused(False)
    app.step()
    assert app.wake.started == started + 1


def test_text_mode_runs_one_utterance(capsys):
    from jarvis_desktop.app import run_text
    sp = FakeSpeaker()
    reply = run_text(FakeAgent(reply="**Done** https://x.y"), sp, "open notepad")
    assert reply == "**Done** https://x.y"
    assert "Done" in capsys.readouterr().out and sp.said == ["**Done** https://x.y"]


def test_doctor_reports_missing_mic_and_never_key(home, monkeypatch, capsys):
    from jarvis_desktop.app import doctor_report
    from jarvis_desktop import config
    config.set_api_key("sk-zzz-secret")

    class SD:
        @staticmethod
        def query_devices(kind=None):
            if kind:
                raise Exception("Error querying device -1")
            return []
    chat_ok = httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]})
    rep = doctor_report(sd_module=SD, http_transport=httpx.MockTransport(lambda r: chat_ok))
    text = json.dumps(rep)
    assert rep["microphone"]["available"] is False and "no microphone" in rep["microphone"]["detail"].lower()
    assert rep["api_key"]["present"] is True and "sk-zzz" not in text
    assert rep["provider"]["reachable"] is True and rep["provider"]["authorized"] is True
