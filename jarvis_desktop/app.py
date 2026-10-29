"""Voice state machine and process wiring for Jarvis Desktop.

IDLE (offline Vosk wake word) -> LISTENING -> THINKING (agent.handle in a worker) -> SPEAKING -> LISTENING
(follow-ups without re-wake for 20 s) -> IDLE.

Phrases: bare "stop" while speaking interrupts playback; "stop listening" / "go to sleep" -> IDLE;
"goodbye jarvis" ends the conversation. Proactive announcements are queued and spoken when idle or
between turns. While speaking, captured audio is discarded except a short barge-in "stop" check.
"""
from __future__ import annotations

import enum
import json
import logging
import queue
import re
import sys
import threading
import time

log = logging.getLogger("jarvis_desktop")

FOLLOW_UP_SECONDS = 20.0
FILLER = "On it."
ERROR_REPLY = "Sorry, something went wrong while I was working on that. Please try again."


class State(enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s\u0900-\u097F]", " ", (text or "").lower())).strip()


_SLEEP = {"stop listening", "go to sleep", "stop listening jarvis", "jarvis stop listening", "jarvis go to sleep",
          "go to sleep jarvis", "that s all", "thats all", "that is all", "stop listening completely", "so jao"}
_GOODBYE = {"goodbye jarvis", "good bye jarvis", "bye jarvis", "goodbye", "bye bye jarvis", "stop jarvis"}
_STOP = {"stop", "jarvis stop", "stop jarvis", "stop it", "stop talking", "be quiet", "quiet", "ruko", "bas",
         "chup", "रुको", "बस"}


def classify_command(text: str) -> str | None:
    n = _norm(text)
    if n in _GOODBYE:
        return "goodbye"
    if n in _SLEEP:
        return "sleep"
    if n in _STOP:
        return "stop"
    return None


class VoiceApp:
    def __init__(self, agent, speaker, mic, stt, wake, clock=time.monotonic, sleep=time.sleep,
                 filler_after=1.5, follow_up_seconds=FOLLOW_UP_SECONDS, listen_slice=3.0, on_state=None):
        self.agent, self.speaker, self.mic, self.stt, self.wake = agent, speaker, mic, stt, wake
        self.clock, self.sleep = clock, sleep
        self.filler_after = filler_after
        self.follow_up_seconds = follow_up_seconds
        self.listen_slice = listen_slice
        self.on_state = on_state
        self.state = State.IDLE
        self.paused = False
        self.last_heard = ""
        self._wake_on = False
        self._deadline = 0.0
        self._pending_text = ""
        self._reply = ""
        self._announcements: queue.Queue = queue.Queue()
        self._quit = threading.Event()
        self._manual_wake = threading.Event()  # set by wake_now() from any thread (hotkey, tray)
        agent.on_event(self.announce)

    # ---- public controls (any thread)
    def announce(self, text: str):
        if text:
            self._announcements.put(text)

    def set_paused(self, paused: bool):
        self.paused = bool(paused)
        if paused:
            try:
                self.speaker.interrupt()
            except Exception:
                pass
        self._notify()

    def wake_now(self) -> bool:
        """Thread-safe 'as if the wake word fired' (global hotkey / tray). Ignored while paused.

        IDLE: the next step chimes, flushes the mic and starts LISTENING. SPEAKING: stops the reply so the
        user can talk. LISTENING: restarts the follow-up window. Returns False when ignored."""
        if self.paused or self._quit.is_set():
            return False
        if self.state is State.SPEAKING:
            try:
                self.speaker.interrupt()
            except Exception:
                pass
        elif self.state is State.LISTENING:
            self._deadline = self.clock() + self.follow_up_seconds
            return True
        self._manual_wake.set()
        return True

    def quit(self):
        self._quit.set()
        try:
            self.speaker.interrupt()
        except Exception:
            pass

    @property
    def status_text(self) -> str:
        if self.paused:
            return "Paused"
        return {State.IDLE: "Waiting for 'Jarvis'", State.LISTENING: "Listening",
                State.THINKING: "Thinking", State.SPEAKING: "Speaking"}[self.state]

    # ---- helpers
    def _set(self, state: State):
        if state is not self.state:
            log.info("state %s -> %s", self.state.value, state.value)
            self.state = state
            self._notify()

    def _notify(self):
        if self.on_state:
            try:
                self.on_state(self)
            except Exception:
                pass

    def _wake_start(self):
        if not self._wake_on:
            self._wake_on = bool(self.wake.start())

    def _wake_stop(self):
        self.wake.stop()
        self._wake_on = False

    def _drain_announcements(self) -> bool:
        spoke = False
        while True:
            try:
                text = self._announcements.get_nowait()
            except queue.Empty:
                return spoke
            self.speaker.say(text)
            spoke = True

    def _to_idle(self):
        self._manual_wake.clear()
        try:
            self.mic.close()
        except Exception:
            pass
        self._set(State.IDLE)

    # ---- the machine
    def step(self):
        handler = {State.IDLE: self._step_idle, State.LISTENING: self._step_listening,
                   State.THINKING: self._step_thinking, State.SPEAKING: self._step_speaking}[self.state]
        handler()

    def _step_idle(self):
        if self.paused:
            self._manual_wake.clear()  # a press while paused is ignored, never replayed on resume
            if self._wake_on:
                self._wake_stop()
            self.sleep(0.2)
            return
        if not self._announcements.empty():
            was_on = self._wake_on
            if was_on:
                self._wake_stop()  # never let our own voice trigger the wake word
            self._drain_announcements()
        manual = self._manual_wake.is_set()
        if not manual:
            self._wake_start()
        if manual or self.wake.pop_event():
            self._manual_wake.clear()
            self._wake_stop()
            chime = getattr(self.speaker, "chime", None)
            if chime:
                chime()
            self.mic.flush()
            self._deadline = self.clock() + self.follow_up_seconds
            self._set(State.LISTENING)
            return
        self.sleep(0.1)

    def _step_listening(self):
        if self.paused:
            self._to_idle()
            return
        if self._drain_announcements():
            self.mic.flush()
            self._deadline = self.clock() + self.follow_up_seconds
        remaining = self._deadline - self.clock()
        if remaining <= 0:
            self._to_idle()
            return
        pcm = self.mic.listen(timeout=min(remaining, self.listen_slice))
        if pcm is None:
            if self.clock() >= self._deadline:
                self._to_idle()
            return
        try:
            text = (self.stt.transcribe(pcm) or "").strip()
        except Exception:
            log.exception("transcription failed")
            text = ""
        if not text:
            return
        self.last_heard = text
        log.info("heard: %s", text)
        command = classify_command(text)
        if command in ("sleep", "goodbye"):
            self.speaker.say("Goodbye." if command == "goodbye" else "Okay.")
            self._to_idle()
            return
        if command == "stop":
            self._deadline = self.clock() + self.follow_up_seconds
            return
        self._pending_text = text
        self._set(State.THINKING)

    def _step_thinking(self):
        warm = getattr(self.speaker, "warm_up", None)
        if warm:  # open the TTS connection while the model thinks: the reply's first chunk skips TLS setup
            try:
                warm()
            except Exception:
                pass
        result = {}
        done = threading.Event()

        def work():
            try:
                result["reply"] = self.agent.handle(self._pending_text)
            except Exception:
                log.exception("agent failed")
                result["reply"] = ERROR_REPLY
            finally:
                done.set()

        threading.Thread(target=work, name="jarvis-agent-turn", daemon=True).start()
        if not done.wait(self.filler_after):
            self.speaker.say(FILLER)
        while not done.wait(0.1):
            if self._quit.is_set():
                return
        self._reply = result.get("reply") or ""
        self._set(State.SPEAKING)

    def _step_speaking(self):
        reply = self._reply
        self._reply = ""
        if reply:
            self.speaker.say(reply, block=False)
            self.mic.flush()
            while self.speaker.is_playing and not self._quit.is_set():
                pcm = self.mic.listen(timeout=0.5, max_seconds=3)
                if pcm is None:
                    continue
                try:
                    heard = self.stt.transcribe(pcm) or ""
                except Exception:
                    heard = ""
                if classify_command(heard) in ("stop", "goodbye", "sleep"):
                    self.speaker.interrupt()
                    break
                # anything else heard while speaking is (probably our own) echo: discarded
        self._drain_announcements()
        self.mic.flush()
        self._deadline = self.clock() + self.follow_up_seconds
        self._set(State.LISTENING)

    def run(self):
        while not self._quit.is_set():
            try:
                self.step()
            except Exception:
                log.exception("voice loop error")
                self.state = State.IDLE
                time.sleep(1)
        try:
            self._wake_stop()
            self.mic.close()
        except Exception:
            pass


# ------------------------------------------------------------------------------- wiring

def run_text(agent, speaker, text: str) -> str:
    reply = agent.handle(text)
    print(reply)
    sys.stdout.flush()
    try:
        speaker.say(reply)
    except Exception:
        log.exception("speaking failed")
    return reply


def agent_config(settings, api_key) -> dict:
    from .config import app_home
    home = app_home()
    ufo = home / "ufo"
    return {"provider": settings.provider, "base_url": settings.base_url, "api_key": api_key, "chat_model": settings.chat_model,
            "autonomy": settings.autonomy, "language": settings.language, "data_dir": str(home / "data"),
            "workspace": str(home / "workspace"), "ufo_home": str(ufo) if (ufo / "ufo" / "__main__.py").is_file() else None,
            # UFO's own config names a placeholder model; send UFO's calls to the user's chat model.
            "ufo_model": settings.chat_model}


def doctor_report(sd_module=None, http_transport=None) -> dict:
    import importlib.util
    import platform

    import httpx

    from . import config

    settings = config.load_settings()
    rep = {"platform": platform.platform(), "python": platform.python_version(),
           "settings_file": str(config.settings_path()), "configured": config.settings_path().is_file()}
    rep["packages"] = {n: importlib.util.find_spec(n) is not None
                       for n in ("sounddevice", "vosk", "faster_whisper", "keyring", "pystray", "PIL", "av")}
    mic = {"available": False, "devices": [], "detail": "No microphone (input device) found."}
    speaker = {"available": False, "detail": "No speaker (output device) found."}
    try:
        sd = sd_module or __import__("sounddevice")
        devices = list(sd.query_devices())
        inputs = [d["name"] for d in devices if d.get("max_input_channels", 0) > 0]
        outputs = [d["name"] for d in devices if d.get("max_output_channels", 0) > 0]
        if inputs:
            mic = {"available": True, "devices": inputs, "detail": f"{len(inputs)} input device(s)."}
        if outputs:
            speaker = {"available": True, "devices": outputs, "detail": f"{len(outputs)} output device(s)."}
    except Exception as exc:
        mic["detail"] = f"No microphone: audio system unavailable ({type(exc).__name__})."
    rep["microphone"], rep["speaker"] = mic, speaker
    model = config.wake_model_path(settings)
    wake = {"model_path": model, "model_present": bool(model) and
            all(__import__("pathlib").Path(model, p).is_file() for p in ("am/final.mdl", "conf/model.conf"))}
    rep["wake"] = wake
    rep["stt"] = {"mode": settings.stt_mode, "local_model": settings.stt_model}
    key = config.get_api_key()
    rep["api_key"] = {"present": bool(key), "source": config.key_source()}
    from .providers import Provider, ProviderError, detect_provider
    kind = settings.provider or detect_provider(settings.base_url, key or "")
    provider = {"type": kind, "base_url": settings.base_url, "model": settings.chat_model, "reachable": False}
    p = Provider(kind, settings.base_url, key or "", transport=http_transport, timeout=20)
    try:
        p.chat(settings.chat_model, [{"role": "user", "content": "Reply OK"}], max_tokens=8)
        provider.update(reachable=True, authorized=True)
    except ProviderError as exc:
        provider.update(reachable=True, authorized=exc.status not in (401, 403), status=exc.status)
    except Exception as exc:
        provider["error"] = type(exc).__name__
    finally:
        p.close()
    rep["provider"] = provider
    return rep


def build_and_run(settings, use_tray=True, offline_tts=False) -> int:
    from companion.launcher import WakeController

    from . import config
    from .agent import Agent
    from .audio import Microphone, Player, beep
    from .confirm import SpokenConfirm
    from .stt import Transcriber
    from .tts import Speaker

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    player = Player()
    speaker = Speaker(settings, config.get_api_key, player=player, offline_only=offline_tts)
    speaker.chime = lambda: player.play(beep(), block=True)
    mic = Microphone()
    stt = Transcriber(settings, config.get_api_key)
    model = config.wake_model_path(settings)
    wake = WakeController(model_path=model)
    status = wake.status()
    wake_ok = bool(status["available"])
    if not wake_ok:
        print(f"Wake word unavailable ({status['reason']}). Run: python -m jarvis_desktop --doctor", file=sys.stderr)
        wake = _NoWake()
    confirm = SpokenConfirm(speak=lambda q: speaker.say(q, block=True),
                            listen=lambda timeout: mic.listen(timeout=timeout, max_seconds=8),
                            transcribe=stt.transcribe, before_listen=mic.flush)
    holder = {}
    agent = Agent(agent_config(settings, config.get_api_key()),
                  speak_async=lambda text: holder["app"].announce(text) if "app" in holder else None,
                  confirm=confirm)
    try:
        threading.Thread(target=stt.local.load, daemon=True, name="jarvis-stt-warmup").start()
        mic.calibrate()
        mic.close()
    except Exception:
        log.warning("microphone calibration failed; continuing")
    app = VoiceApp(agent=agent, speaker=speaker, mic=mic, stt=stt, wake=wake)
    holder["app"] = app
    combo = getattr(settings, "hotkey", "") or "ctrl+alt+j"
    hotkey, hotkey_label = _start_hotkey(app, combo)
    if not wake_ok and hotkey is None:
        agent.close()
        return 3
    tray = None
    try:
        if use_tray:
            from .tray import start_tray
            tray = start_tray(app)
        if wake_ok:
            ready = "Jarvis is ready. Say 'Jarvis' to start."
            if hotkey is not None:
                ready += f" Or press {hotkey_label} to talk."
        else:
            ready = f"Jarvis is ready (hotkey only). Press {hotkey_label} to talk."
        print(ready + ("" if tray else " (Ctrl+C to quit)"))
        sys.stdout.flush()
        app.run()
    except KeyboardInterrupt:
        app.quit()
    finally:
        agent.close()
        for stop in (getattr(hotkey, "stop", None), getattr(tray, "stop", None), getattr(speaker, "close", None)):
            if stop is not None:
                try:
                    stop()
                except Exception:
                    pass
    return 0


class _NoWake:
    """Stand-in wake detector for hotkey-only mode (no Vosk model / no wake microphone)."""

    def start(self):
        return False

    def stop(self):
        pass

    def pop_event(self):
        return False

    def status(self):
        return {"available": False, "listening": False, "reason": "hotkey_only", "local_only": True}


def _start_hotkey(app, combo):
    """Register the global push-to-talk hotkey -> app.wake_now. Never fatal: (handle or None, label)."""
    label = "Ctrl+Alt+J"
    try:
        import importlib
        hotkey_mod = importlib.import_module(".hotkey", __package__)
        try:
            label = hotkey_mod.label(combo)
        except Exception:
            pass
        handle = hotkey_mod.start_hotkey(app.wake_now, combo)
    except ImportError:
        log.info("hotkey module unavailable; wake word only")
        return None, label
    except Exception:
        log.exception("hotkey could not be started; continuing without it")
        return None, label
    return handle, label


def print_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))
