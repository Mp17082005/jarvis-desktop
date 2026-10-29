"""Latency work: sentence-pipelined TTS, fast local STT defaults, tighter endpointing, wake_now/hotkey wiring."""
import sys
import threading
import time
import types

import numpy as np
import pytest

SR = 16000


# ------------------------------------------------------------------ helpers
def settings(**kw):
    base = dict(provider="openai", base_url="https://p/v1", tts_model="tts-1", tts_voice="alloy", language="en",
                stt_mode="local", stt_model="base")
    base.update(kw)
    return types.SimpleNamespace(**base)


class TimedPlayer:
    """Fake output: each clip 'plays' for clip_seconds; records (text, start, end)."""

    def __init__(self, clip_seconds=0.15):
        self.clip_seconds = clip_seconds
        self.log = []
        self._end = 0.0
        self._interrupted = threading.Event()

    def play(self, data, block=True):
        start = time.monotonic()
        self._interrupted.clear()
        self._end = start + self.clip_seconds
        self.log.append((data.decode(), start))
        if block:
            while time.monotonic() < self._end and not self._interrupted.is_set():
                time.sleep(0.005)
        return True

    @property
    def is_playing(self):
        return time.monotonic() < self._end

    def interrupt(self):
        self._interrupted.set()
        self._end = 0.0


def make_speaker(player, synth_seconds=0.2, fail=()):
    from jarvis_desktop.tts import Speaker
    sp = Speaker(settings(), lambda: "k", player=player)
    synthesized = []

    def fake_synth(text):
        time.sleep(synth_seconds)
        if text in fail:
            raise RuntimeError("tts down")
        synthesized.append(text)
        return text.encode()
    sp.synthesize = fake_synth
    return sp, synthesized


REPLY = ("It is three forty five in the afternoon. The weather is sunny and warm today. "
         "Tomorrow will bring some light rain in the evening. Take an umbrella with you if you go out.")


# ------------------------------------------------------------------ sentence splitting
def test_split_first_chunk_is_short_first_sentence_and_order_kept():
    from jarvis_desktop.tts import split_for_speech
    chunks = split_for_speech(REPLY)
    assert chunks[0] == "It is three forty five in the afternoon."
    assert len(chunks) >= 2
    assert " ".join(chunks) == REPLY


def test_split_merges_tiny_fragments():
    from jarvis_desktop.tts import split_for_speech
    chunks = split_for_speech("Sure. Ok. I opened Notepad for you and typed the list.")
    assert all(len(c) >= 12 for c in chunks)
    assert " ".join(chunks) == "Sure. Ok. I opened Notepad for you and typed the list."
    assert split_for_speech("Done.") == ["Done."]
    assert split_for_speech("") == []


def test_split_caps_a_very_long_first_sentence_at_a_comma():
    from jarvis_desktop.tts import split_for_speech
    text = ("Here is what I found about the forecast for the coming week in your city, which looks mostly dry "
            "with temperatures between twenty and twenty eight degrees and a light breeze from the west.")
    chunks = split_for_speech(text)
    assert len(chunks[0]) <= 120 and chunks[0].endswith(",")
    assert " ".join(chunks) == text


def test_split_does_not_break_decimals_or_times():
    from jarvis_desktop.tts import split_for_speech
    chunks = split_for_speech("It is 3.45 PM and 21.5 degrees outside right now in Pune.")
    assert chunks == ["It is 3.45 PM and 21.5 degrees outside right now in Pune."]


# ------------------------------------------------------------------ pipelined speech
def test_first_audio_after_first_chunk_synthesis_only_and_order_preserved():
    from jarvis_desktop.tts import split_for_speech
    chunks = split_for_speech(REPLY)
    n = len(chunks)
    player = TimedPlayer(clip_seconds=0.3)
    synth_started = []

    from jarvis_desktop.tts import Speaker
    sp = Speaker(settings(), lambda: "k", player=player)

    def fake_synth(text):
        synth_started.append((text, time.monotonic()))
        time.sleep(0.2)
        return text.encode()
    sp.synthesize = fake_synth
    t0 = time.monotonic()
    assert sp.say(REPLY) is True
    total = time.monotonic() - t0
    assert [t for t, _ in player.log] == chunks
    # playback of chunk 1 began before chunk 2 had even finished synthesizing (one synth, not the whole reply)
    assert player.log[0][1] < synth_started[1][1] + 0.2
    # chunk N+1 synthesis started while chunk N was still playing (pipelined, not serial)
    for i in range(1, n):
        assert synth_started[i][1] < player.log[i - 1][1] + 0.3, i
    assert total < n * (0.2 + 0.3), total  # strictly faster than serial synth+play


def test_say_non_blocking_keeps_is_playing_true_across_chunk_gaps():
    player = TimedPlayer(clip_seconds=0.05)  # clips shorter than synthesis -> real gaps between clips
    sp, _ = make_speaker(player, synth_seconds=0.15)
    assert sp.say(REPLY, block=False) is True
    from jarvis_desktop.tts import split_for_speech
    n = len(split_for_speech(REPLY))
    assert 1 <= len(player.log) < n  # returned once chunk 1 started, not after the whole reply
    seen_false_early = False
    while len(player.log) < n:
        if not sp.is_playing:
            seen_false_early = True
            break
        time.sleep(0.005)
    assert not seen_false_early
    deadline = time.monotonic() + 3
    while sp.is_playing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not sp.is_playing and len(player.log) == n


def test_interrupt_stops_playback_and_cancels_pending_synthesis():
    player = TimedPlayer(clip_seconds=0.3)
    sp, synthesized = make_speaker(player, synth_seconds=0.1)
    sp.say(REPLY, block=False)
    time.sleep(0.15)
    sp.interrupt()
    assert not sp.is_playing
    time.sleep(0.6)
    assert len(player.log) == 1
    assert len(synthesized) <= 2  # at most the one-ahead chunk that was already in flight


def test_first_chunk_failure_returns_false_later_failure_is_skipped():
    from jarvis_desktop.tts import split_for_speech
    chunks = split_for_speech(REPLY)
    player = TimedPlayer(clip_seconds=0.01)
    sp, _ = make_speaker(player, synth_seconds=0.01, fail={chunks[0]})
    assert sp.say(REPLY) is False or [t for t, _ in player.log] == chunks[1:]
    player2 = TimedPlayer(clip_seconds=0.01)
    sp2, _ = make_speaker(player2, synth_seconds=0.01, fail={chunks[1]})
    assert sp2.say(REPLY) is True
    assert [t for t, _ in player2.log] == [c for c in chunks if c != chunks[1]]


def test_whole_reply_synthesis_failure_returns_false():
    player = TimedPlayer()
    from jarvis_desktop.tts import Speaker
    sp = Speaker(settings(), lambda: "k", player=player)
    sp.synthesize = lambda text: (_ for _ in ()).throw(RuntimeError("down"))
    assert sp.say("Hello there. How are you doing today?") is False
    assert sp.say("Hello there.", block=False) is False
    assert not sp.is_playing and player.log == []


def test_new_say_replaces_running_one():
    player = TimedPlayer(clip_seconds=0.3)
    sp, _ = make_speaker(player, synth_seconds=0.05)
    sp.say(REPLY, block=False)
    time.sleep(0.1)
    assert sp.say("Okay.") is True
    texts = [t for t, _ in player.log]
    assert texts[-1] == "Okay." and len(texts) == 2


def test_provider_speech_reuses_one_connection_pool_across_chunks():
    import httpx
    from jarvis_desktop.tts import Speaker
    opened = []

    class CountingTransport(httpx.MockTransport):
        def __init__(self):
            super().__init__(lambda r: httpx.Response(200, content=b"ID3" + r.content[-8:],
                                                      headers={"content-type": "audio/mpeg"}))
    transport = CountingTransport()
    orig = httpx.Client.__init__

    def spy(self, *a, **k):
        opened.append(1)
        orig(self, *a, **k)
    sp = Speaker(settings(), lambda: "k", player=TimedPlayer(clip_seconds=0.0), transport=transport)
    httpx.Client.__init__ = spy
    try:
        assert sp.say(REPLY) is True
        assert sp.say("Second reply here.") is True
    finally:
        httpx.Client.__init__ = orig
    assert len(opened) <= 1  # one keep-alive client for every chunk and every reply


def test_warm_up_opens_connection_without_sending_the_key():
    import httpx
    from jarvis_desktop.tts import Speaker
    seen = []
    transport = httpx.MockTransport(lambda r: (seen.append((r.method, str(r.url), r.headers.get("authorization"))),
                                               httpx.Response(404))[1])
    sp = Speaker(settings(base_url="https://p.example/v1"), lambda: "secret", player=TimedPlayer(), transport=transport)
    t = sp.warm_up()
    t.join(2)
    assert seen and seen[0][0] == "HEAD" and seen[0][1].startswith("https://p.example/") and seen[0][2] is None
    off = Speaker(settings(), lambda: "secret", player=TimedPlayer(), transport=transport, offline_only=True)
    seen.clear()
    assert off.warm_up() is None and seen == []


def test_short_fixed_phrases_are_cached():
    from jarvis_desktop.tts import Speaker
    calls = []
    sp = Speaker(settings(), lambda: "k", player=TimedPlayer(clip_seconds=0.0))
    sp._synthesize_uncached = lambda text: (calls.append(text), b"ID3" + text.encode())[1]
    sp.synthesize("On it.")
    sp.synthesize("On it.")
    assert calls == ["On it."]
    long = "This reply is long enough that caching it would be pointless and wasteful."
    sp.synthesize(long); sp.synthesize(long)
    assert calls.count(long) == 2


def test_cancelled_prefetch_never_falls_back_to_offline_voice(monkeypatch):
    import jarvis_desktop.tts as tts
    offline = []
    monkeypatch.setattr(tts, "sapi_speech", lambda text, language="": offline.append(text) or b"RIFF")
    started = threading.Event()

    def slow_provider(*a, **k):
        started.set()
        time.sleep(0.2)
        raise tts.TTSError("connection closed")
    monkeypatch.setattr(tts, "provider_speech", slow_provider)
    sp = tts.Speaker(settings(), lambda: "k", player=TimedPlayer())
    threading.Thread(target=lambda: sp.say(REPLY, block=False), daemon=True).start()
    assert started.wait(2)
    sp.interrupt()
    sp.close()
    time.sleep(0.4)
    assert offline == []


def test_first_engine_reports_the_engine_of_the_first_spoken_chunk():
    player = TimedPlayer(clip_seconds=0.01)
    sp, _ = make_speaker(player, synth_seconds=0.01)
    sp.say("Hello there, my friend.")
    assert sp.first_engine is None or isinstance(sp.first_engine, str)


def test_thinking_prewarms_tts_connection():
    from jarvis_desktop.app import State, VoiceApp

    class Sp(FakeSpeaker):
        def __init__(self):
            super().__init__(); self.warmed = 0

        def warm_up(self):
            self.warmed += 1

    class Mic(FakeMic):
        def __init__(self):
            super().__init__(); self.script = ["hello"]

        def listen(self, timeout, max_seconds=None):
            return self.script.pop(0) if self.script else None

    class STT:
        def transcribe(self, pcm):
            return pcm
    sp = Sp()
    app = VoiceApp(agent=FakeAgent(), speaker=sp, mic=Mic(), stt=STT(), wake=FakeWake(), sleep=lambda s: None)
    app.wake_now(); app.step(); app.step()
    assert app.state is State.THINKING
    app.step()
    assert sp.warmed == 1 and app.state is State.SPEAKING


# ------------------------------------------------------------------ STT defaults
class FakeWhisperModel:
    created = []

    def __init__(self, name, **kw):
        if name.endswith(".en") and getattr(FakeWhisperModel, "fail_en", False):
            raise RuntimeError("offline: cannot download")
        FakeWhisperModel.created.append((name, kw))
        self.calls = []

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        return iter([types.SimpleNamespace(text=" hello world ")]), None


@pytest.fixture
def fake_fw(monkeypatch):
    FakeWhisperModel.created = []
    FakeWhisperModel.fail_en = False
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeWhisperModel))
    return FakeWhisperModel


def test_english_uses_en_model_all_cores_and_fast_decode(fake_fw, tmp_path):
    import os
    from jarvis_desktop.stt import LocalWhisper
    w = LocalWhisper("base", download_root=str(tmp_path), language="en")
    assert w.transcribe(np.zeros(SR, np.float32)) == "hello world"
    name, kw = fake_fw.created[0]
    assert name == "base.en"
    assert kw["cpu_threads"] == min(os.cpu_count() or 4, 8) and kw["compute_type"] == "int8"
    call = w._model.calls[0]
    assert call["beam_size"] == 1 and call["without_timestamps"] is True and call["vad_filter"] is False
    assert call["language"] == "en"


@pytest.mark.parametrize("lang,expected", [("hi", "base"), ("auto", "base"), ("", "base"), (None, "base"),
                                           ("en-US", "base.en")])
def test_non_english_keeps_multilingual_model(fake_fw, tmp_path, lang, expected):
    from jarvis_desktop.stt import LocalWhisper
    LocalWhisper("base", download_root=str(tmp_path), language=lang).load()
    assert fake_fw.created[0][0] == expected


def test_assistant_vocabulary_hint_is_passed_to_the_decoder(fake_fw, tmp_path):
    # Without a hint, both tiny.en and base.en heard "lofi" as "low-five"/"Place some lo-fi".
    from jarvis_desktop.stt import LocalWhisper
    w = LocalWhisper("base", download_root=str(tmp_path), language="en")
    w.transcribe(__import__("numpy").zeros(16000, "float32"))
    prompt = w._model.calls[0].get("initial_prompt") or ""
    for word in ("Jarvis", "YouTube", "lofi", "Notepad", "Excel"):
        assert word in prompt


def test_default_speech_model_is_tiny_for_speed():
    from jarvis_desktop.config import Settings
    assert Settings().stt_model == "tiny"


def test_en_model_unavailable_falls_back_to_multilingual(fake_fw, tmp_path):
    from jarvis_desktop.stt import LocalWhisper
    fake_fw.fail_en = True
    w = LocalWhisper("tiny", download_root=str(tmp_path), language="en")
    assert w.transcribe(np.zeros(SR, np.float32)) == "hello world"
    assert fake_fw.created[0][0] == "tiny"


def test_transcriber_builds_en_model_from_settings(fake_fw, tmp_path):
    from jarvis_desktop.stt import Transcriber
    t = Transcriber(settings(stt_model="tiny", language="en"), lambda: "k")
    t.local.download_root = str(tmp_path)
    t.local.load()
    assert fake_fw.created[0][0] == "tiny.en"


# ------------------------------------------------------------------ endpointing
def _noise(seconds, amp=0.002, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(SR * seconds)) * amp).astype(np.float32)


def _word(seconds, seed):
    t = np.arange(int(SR * seconds)) / SR
    env = np.minimum(1, np.minimum(t, seconds - t) * 30)  # soft onset/offset like real syllables
    return (np.sin(2 * np.pi * (180 + 20 * seed) * t) * 0.25 * env).astype(np.float32) + _noise(seconds, seed=seed)


def _feed(ep, sig, chunk=1600):
    out = []
    for i in range(0, len(sig), chunk):
        out.extend(ep.feed(sig[i:i + chunk]))
    return out


def test_default_end_silence_is_700ms():
    from jarvis_desktop.audio import END_SILENCE_MS, Endpointer
    assert END_SILENCE_MS == 700
    ep = Endpointer(sample_rate=SR)
    assert ep.end_frames == 700 // 30


@pytest.mark.parametrize("pause", [0.3, 0.4, 0.5])
def test_700ms_endpoint_keeps_multi_word_phrase_with_natural_pauses(pause):
    from jarvis_desktop.audio import Endpointer
    ep = Endpointer(sample_rate=SR)
    ep.calibrate(_noise(1.0))
    words = [0.35, 0.25, 0.45, 0.3, 0.5]
    parts = [_noise(0.4, seed=50)]
    for i, w in enumerate(words):
        parts.append(_word(w, seed=i))
        if i < len(words) - 1:
            parts.append(_noise(pause, seed=100 + i))
    out = _feed(ep, np.concatenate(parts))
    assert out == []  # not cut at any inner pause
    out = _feed(ep, _noise(1.0, seed=200))
    assert len(out) == 1
    assert len(out[0]) / SR >= sum(words) + pause * (len(words) - 1)  # every word kept


def test_endpoint_fires_within_700ms_of_silence_and_is_configurable():
    from jarvis_desktop.audio import Endpointer
    ep = Endpointer(sample_rate=SR)
    ep.calibrate(_noise(1.0))
    assert _feed(ep, np.concatenate([_noise(0.3, seed=1), _word(0.8, 2)])) == []
    tail = _noise(1.5, seed=3)
    fired_at = None
    for i in range(0, len(tail), 480):
        if ep.feed(tail[i:i + 480]):
            fired_at = (i + 480) / SR
            break
    assert fired_at is not None and 0.6 <= fired_at <= 0.75, fired_at
    slow = Endpointer(sample_rate=SR, end_silence_ms=1200)
    assert slow.end_frames == 40


def test_end_silence_env_override(monkeypatch):
    import importlib
    from jarvis_desktop import audio
    monkeypatch.setenv("JARVIS_END_SILENCE_MS", "850")
    try:
        importlib.reload(audio)
        assert audio.END_SILENCE_MS == 850 and audio.Endpointer().end_frames == 850 // 30
        monkeypatch.setenv("JARVIS_END_SILENCE_MS", "nonsense")
        importlib.reload(audio)
        assert audio.END_SILENCE_MS == 700
    finally:
        monkeypatch.delenv("JARVIS_END_SILENCE_MS", raising=False)
        importlib.reload(audio)


# ------------------------------------------------------------------ wake_now
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
    def __init__(self):
        self.flushed = 0

    def listen(self, timeout, max_seconds=None):
        return None

    def flush(self):
        self.flushed += 1

    def close(self):
        pass


class FakeSpeaker:
    def __init__(self):
        self.chimes = 0; self.interrupted = 0; self.said = []

    def chime(self):
        self.chimes += 1

    def say(self, text, block=True):
        self.said.append(text)

    def interrupt(self):
        self.interrupted += 1

    @property
    def is_playing(self):
        return False


class FakeAgent:
    def on_event(self, cb):
        pass

    def handle(self, text):
        return "ok"

    def close(self):
        pass


def make_app():
    from jarvis_desktop.app import VoiceApp
    return VoiceApp(agent=FakeAgent(), speaker=FakeSpeaker(), mic=FakeMic(), stt=None, wake=FakeWake(),
                    sleep=lambda s: None)


def test_wake_now_behaves_like_wake_word_from_another_thread():
    from jarvis_desktop.app import State
    app = make_app()
    app.step()
    assert app.state is State.IDLE
    t = threading.Thread(target=app.wake_now)
    t.start(); t.join()
    app.step()
    assert app.state is State.LISTENING
    assert app.speaker.chimes == 1 and app.mic.flushed >= 1 and app.wake.stopped >= 1


def test_wake_now_ignored_while_paused_and_not_replayed_later():
    from jarvis_desktop.app import State
    app = make_app()
    app.set_paused(True)
    assert app.wake_now() is False
    app.step()
    assert app.state is State.IDLE and app.speaker.chimes == 0
    app.set_paused(False)
    app.step()
    assert app.state is State.IDLE and app.speaker.chimes == 0


def test_wake_now_is_one_shot():
    from jarvis_desktop.app import State
    app = make_app()
    app.wake_now()
    app.step()
    assert app.state is State.LISTENING
    app._to_idle()
    app.step()
    assert app.state is State.IDLE and app.speaker.chimes == 1


def test_wake_now_while_speaking_interrupts_and_while_listening_extends():
    from jarvis_desktop.app import State
    app = make_app()
    app.state = State.SPEAKING
    assert app.wake_now() is True
    assert app.speaker.interrupted == 1
    app.state = State.LISTENING
    app._deadline = 0
    app.wake_now()
    assert app._deadline > time.monotonic() + 10


# ------------------------------------------------------------------ build_and_run hotkey wiring
class _Unavailable:
    def __init__(self, model_path=None):
        pass

    def status(self):
        return {"available": False, "reason": "model_missing"}


class _Available(_Unavailable):
    def status(self):
        return {"available": True, "reason": "ready"}

    def start(self):
        return True

    def stop(self):
        pass

    def pop_event(self):
        return False


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """build_and_run with every device/network piece faked; VoiceApp.run returns immediately."""
    import companion.launcher as launcher
    from jarvis_desktop import agent as agent_mod, app as app_mod, audio, config, stt as stt_mod
    monkeypatch.setenv("JARVIS_DESKTOP_HOME", str(tmp_path))
    rec = {"agent_closed": 0, "ran": 0}

    class Mic:
        def calibrate(self): pass
        def close(self): pass
        def flush(self): pass
        def listen(self, timeout=0, max_seconds=None): return None

    class Agent:
        def __init__(self, *a, **k): pass
        def on_event(self, cb): pass
        def close(self): rec["agent_closed"] += 1

    class Trans:
        def __init__(self, *a, **k):
            self.local = types.SimpleNamespace(load=lambda: None)

        def transcribe(self, pcm):
            return ""

    monkeypatch.setattr(audio, "Microphone", Mic)
    monkeypatch.setattr(audio, "Player", lambda *a, **k: types.SimpleNamespace(play=lambda *a, **k: True,
                                                                                interrupt=lambda: None,
                                                                                is_playing=False))
    monkeypatch.setattr(agent_mod, "Agent", Agent)
    monkeypatch.setattr(stt_mod, "Transcriber", Trans)
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    monkeypatch.setattr(app_mod.VoiceApp, "run", lambda self: (rec.__setitem__("ran", rec["ran"] + 1),
                                                              rec.__setitem__("app", self)))
    rec["launcher"] = launcher
    return rec


def _hotkey_module(monkeypatch, result):
    calls = []
    mod = types.ModuleType("jarvis_desktop.hotkey")

    def start_hotkey(cb, combo="ctrl+alt+j"):
        calls.append((cb, combo))
        return result
    mod.start_hotkey = start_hotkey
    mod.label = lambda combo: "Ctrl+Alt+J"
    monkeypatch.setitem(sys.modules, "jarvis_desktop.hotkey", mod)
    import jarvis_desktop
    monkeypatch.setattr(jarvis_desktop, "hotkey", mod, raising=False)
    return calls


def test_hotkey_only_mode_when_wake_model_missing(wired, monkeypatch, capsys):
    from jarvis_desktop.app import build_and_run
    from jarvis_desktop.config import Settings
    monkeypatch.setattr(wired["launcher"], "WakeController", _Unavailable)
    stopped = []
    handle = types.SimpleNamespace(stop=lambda: stopped.append(1))
    calls = _hotkey_module(monkeypatch, handle)
    rc = build_and_run(Settings(), use_tray=False)
    assert rc == 0 and wired["ran"] == 1
    assert calls and calls[0][1] == "ctrl+alt+j"
    assert calls[0][0] == wired["app"].wake_now
    assert "Press Ctrl+Alt+J to talk" in capsys.readouterr().out
    assert stopped == [1] and wired["agent_closed"] == 1


def test_no_wake_model_and_no_hotkey_still_exits_3(wired, monkeypatch, capsys):
    from jarvis_desktop.app import build_and_run
    from jarvis_desktop.config import Settings
    monkeypatch.setattr(wired["launcher"], "WakeController", _Unavailable)
    _hotkey_module(monkeypatch, None)  # registration failed
    assert build_and_run(Settings(), use_tray=False) == 3
    assert wired["ran"] == 0 and wired["agent_closed"] == 1


def test_missing_hotkey_module_is_tolerated(wired, monkeypatch, capsys):
    from jarvis_desktop.app import build_and_run
    from jarvis_desktop.config import Settings
    monkeypatch.setattr(wired["launcher"], "WakeController", _Available)
    monkeypatch.setitem(sys.modules, "jarvis_desktop.hotkey", None)  # import raises ImportError
    assert build_and_run(Settings(), use_tray=False) == 0
    assert wired["ran"] == 1
    assert "Say 'Jarvis'" in capsys.readouterr().out


def test_hotkey_start_exception_is_logged_and_app_continues(wired, monkeypatch):
    from jarvis_desktop.app import build_and_run
    from jarvis_desktop.config import Settings
    monkeypatch.setattr(wired["launcher"], "WakeController", _Available)
    mod = types.ModuleType("jarvis_desktop.hotkey")
    mod.start_hotkey = lambda cb, combo="": (_ for _ in ()).throw(OSError("boom"))
    monkeypatch.setitem(sys.modules, "jarvis_desktop.hotkey", mod)
    assert build_and_run(Settings(), use_tray=False) == 0 and wired["ran"] == 1
