"""Live end-to-end latency benchmark for the Jarvis desktop voice pipeline (not a pytest test).

Real clip ('What time is it right now?' synthesized by the provider TTS, falling back to offline SAPI)
-> 16 kHz + room noise -> Endpointer fed in 100 ms mic blocks with a silence tail
-> Transcriber with the real local faster-whisper model (preloaded, as the app warms it up at start)
-> real Agent.handle against the real provider
-> Speaker with a fake player that records when play() is first called.

Prints one JSON object per run plus a median summary. Never prints the API key.

    env -u PYTHONPATH -u PYTHONHOME .venv/Scripts/python.exe -E tests/bench_latency.py [--runs 3] [--stt-model base]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
# JARVIS_BENCH_SRC: import jarvis_desktop from another checkout (e.g. the pre-change code) for A/B runs
sys.path.insert(0, os.getenv("JARVIS_BENCH_SRC") or str(ROOT))

PHRASE = "What time is it right now?"
LONG_PHRASE = "Tell me three quick facts about the Moon."
SR = 16000
BLOCK = 1600  # 100 ms mic blocks, as Microphone delivers them


def load_env():
    env = {}
    path = ROOT / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class NullJobs:
    announce = None
    def start(self): pass
    def close(self): pass


class NullScheduler:
    fire = announce = None
    def start(self): pass
    def close(self): pass


class RecordingPlayer:
    """Fake output device: records when play() is first called; clips 'play' for their real duration."""

    def __init__(self):
        self.t_first = None
        self.calls = []
        self._end = 0.0

    def play(self, data, block=True):
        from jarvis_desktop.audio import decode_audio
        now = time.perf_counter()
        if self.t_first is None:
            self.t_first = now
        try:
            x, sr = decode_audio(data)
            dur = len(x) / sr
        except Exception:
            dur = 0.0
        self.calls.append((now, dur))
        self._end = time.monotonic() + dur
        if block:
            while self.is_playing:
                time.sleep(0.01)
        return True

    @property
    def is_playing(self):
        return time.monotonic() < self._end

    def interrupt(self):
        self._end = 0.0


def make_clip(settings, key, phrase=PHRASE):
    from jarvis_desktop.audio import decode_audio
    from jarvis_desktop.tts import Speaker
    sp = Speaker(settings, lambda: key, player=RecordingPlayer())
    audio = sp.synthesize(phrase)
    x, _ = decode_audio(audio, SR)
    nz = np.flatnonzero(np.abs(x) > 0.01)  # trim the synthesizer's own leading/trailing silence
    if nz.size:
        x = x[max(0, nz[0] - 800): nz[-1] + 800]
    peak = float(np.max(np.abs(x))) or 1.0
    return (x / peak * 0.5).astype(np.float32), sp.last_engine


def room_noise(seconds, seed):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(SR * seconds)) * 0.003).astype(np.float32)


def endpoint(clip):
    """Feed calibration noise, the clip, then silence until the endpointer emits the utterance.
    Returns (utterance, endpoint_ms = audio time after speech end until emission + processing wall time)."""
    from jarvis_desktop.audio import Endpointer
    ep = Endpointer(sample_rate=SR)
    ep.calibrate(room_noise(1.0, 1))
    signal = np.concatenate([room_noise(0.3, 2), clip + room_noise(len(clip) / SR, 3)[:len(clip)]])
    for i in range(0, len(signal), BLOCK):
        out = ep.feed(signal[i:i + BLOCK])
        if out:
            raise RuntimeError("endpointer cut the phrase before it ended")
    tail = room_noise(3.0, 4)
    t0 = time.perf_counter()
    for i in range(0, len(tail), BLOCK):
        out = ep.feed(tail[i:i + BLOCK])
        if out:
            silence_ms = (i + BLOCK) / SR * 1000
            return out[0], silence_ms + (time.perf_counter() - t0) * 1000, silence_ms
    raise RuntimeError("endpointer never ended the utterance")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--stt-model", default=None, help="override settings.stt_model (tiny/base/small)")
    ap.add_argument("--chat-model", default="claude-sonnet-5")
    ap.add_argument("--label", default="")
    ap.add_argument("--phrase", default=PHRASE, help=f"spoken request (e.g. {LONG_PHRASE!r} for a multi-sentence reply)")
    ap.add_argument("--ab", action="store_true",
                    help="also time the old path (whole reply synthesized before playback) in the same run")
    args = ap.parse_args(argv)

    env = load_env()
    key = env.get("JARVIS_LLM_API_KEY") or os.getenv("JARVIS_LLM_API_KEY")
    base_url = env.get("JARVIS_LLM_BASE_URL") or os.getenv("JARVIS_LLM_BASE_URL")
    if not key or not base_url:
        print("JARVIS_LLM_API_KEY / JARVIS_LLM_BASE_URL missing from .env", file=sys.stderr)
        return 2
    home = Path(os.getenv("JARVIS_BENCH_HOME") or Path(tempfile.gettempdir()) / "jarvis-bench-home")
    from jarvis_desktop.config import models_dir
    whisper_root = str(models_dir() / "whisper")  # the app's real model cache (downloaded once)
    os.environ["JARVIS_DESKTOP_HOME"] = str(home)  # agent data stays out of the user's real app dir

    from jarvis_desktop.agent import Agent
    from jarvis_desktop.config import Settings
    from jarvis_desktop.stt import Transcriber
    from jarvis_desktop.tts import Speaker

    settings = Settings(provider="litellm", base_url=base_url.rstrip("/"), chat_model=args.chat_model,
                        tts_model="tts-mai-voice-2", stt_mode="local", language="en")
    if args.stt_model:
        settings.stt_model = args.stt_model
    clip, clip_engine = make_clip(settings, key, args.phrase)

    stt = Transcriber(settings, lambda: key)
    stt.local.download_root = whisper_root
    t = time.perf_counter()
    stt.local.load()
    load_ms = (time.perf_counter() - t) * 1000
    stt.transcribe(clip)  # warm-up, as the app does at start

    data = tempfile.mkdtemp(prefix="jarvis-bench-data-")
    cfg = {"provider": "litellm", "base_url": settings.base_url, "api_key": key, "chat_model": args.chat_model,
           "autonomy": "balanced", "language": "en", "data_dir": data, "workspace": str(Path(data) / "ws"),
           "ufo_home": None, "ufo_model": args.chat_model}
    agent = Agent(cfg, speak_async=None, confirm=None, jobs=NullJobs(), scheduler=NullScheduler())
    runs = []
    try:
        for n in range(args.runs):
            if isinstance(getattr(agent.memory, "turns", None), list):
                agent.memory.turns.clear()  # each run is a fresh one-turn conversation
            utt, endpoint_ms, silence_ms = endpoint(clip)
            t = time.perf_counter()
            text = stt.transcribe(utt)
            stt_ms = (time.perf_counter() - t) * 1000
            player = RecordingPlayer()
            speaker = Speaker(settings, lambda: key, player=player)
            t = time.perf_counter()
            if hasattr(speaker, "warm_up"):
                speaker.warm_up()  # as VoiceApp does on entering THINKING
            reply = agent.handle(text)
            llm_ms = (time.perf_counter() - t) * 1000
            t = time.perf_counter()
            speaker.say(reply, block=False)
            while player.t_first is None and time.perf_counter() - t < 60:
                time.sleep(0.005)
            tts_first_ms = ((player.t_first or time.perf_counter()) - t) * 1000
            speaker.interrupt()
            if hasattr(speaker, "close"):
                speaker.close()
            whole_ms = None
            if args.ab:  # old behaviour: one request for the whole reply, playback only after it returns
                from jarvis_desktop.tts import sanitize_for_speech
                old = Speaker(settings, lambda: key, player=RecordingPlayer())  # fresh connection, like before
                t = time.perf_counter()
                getattr(old, "_synthesize_uncached", old.synthesize)(sanitize_for_speech(reply))
                whole_ms = round((time.perf_counter() - t) * 1000)
            run = {"run": n + 1, "transcript": text, "reply_chars": len(reply),
                   "endpoint_ms": round(endpoint_ms), "endpoint_silence_ms": round(silence_ms),
                   "stt_ms": round(stt_ms), "llm_ms": round(llm_ms), "tts_first_audio_ms": round(tts_first_ms),
                   "tts_engine": getattr(speaker, "first_engine", None) or speaker.last_engine}
            run["time_to_first_audio_ms"] = run["endpoint_ms"] + run["stt_ms"] + run["llm_ms"] + run["tts_first_audio_ms"]
            if whole_ms is not None:
                run["tts_whole_reply_ms"] = whole_ms
                run["old_path_time_to_first_audio_ms"] = 900 + run["stt_ms"] + run["llm_ms"] + whole_ms
            print(json.dumps(run))
            sys.stdout.flush()
            runs.append(run)
    finally:
        agent.close()
    keys = ["endpoint_ms", "stt_ms", "llm_ms", "tts_first_audio_ms", "time_to_first_audio_ms"]
    if args.ab:
        keys += ["tts_whole_reply_ms", "old_path_time_to_first_audio_ms"]
    summary = {"label": args.label, "stt_model": getattr(stt.local, "model_name", None) or stt.local.model_size,
               "phrase": args.phrase,
               "clip_engine": clip_engine, "clip_seconds": round(len(clip) / SR, 2), "stt_load_ms": round(load_ms),
               "runs": len(runs), "median": {k: round(statistics.median(r[k] for r in runs)) for k in keys}}
    print(json.dumps({"summary": summary}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
