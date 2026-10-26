"""Microphone capture, energy endpointing and interruptible playback.

* Capture: sounddevice 16 kHz mono float32 (injectable ``read_frame`` for tests / no-mic machines).
* Endpointing: frame RMS vs an adaptive noise floor (minimum statistics), calibrated on ~1 s of room
  audio. An utterance ends after 900 ms below threshold, is capped at 30 s, and voiced spans shorter
  than 0.3 s (clicks, coughs) are discarded.
* Decoding: WAV via the stdlib, everything else (mp3/ogg/...) via PyAV, which faster-whisper already
  depends on, so no extra decoder dependency.
* Playback: Windows ``winmm.PlaySoundW`` on a temp WAV (async, stoppable, no extra dependency);
  sounddevice output elsewhere.
"""
from __future__ import annotations

import io
import os
import queue
import sys
import tempfile
import threading
import time
import wave
from collections import deque

import numpy as np

SAMPLE_RATE = 16000


def rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


class Endpointer:
    """Streaming utterance segmentation by RMS energy with an adaptive noise floor."""

    def __init__(self, sample_rate=SAMPLE_RATE, frame_ms=30, end_silence_ms=900, max_seconds=30.0,
                 min_speech_seconds=0.3, preroll_ms=300, tail_ms=300, ratio=3.0, min_threshold=0.002,
                 start_frames=3, idle_window_s=1.5, speech_window_s=3.0):
        self.sr = sample_rate
        self.frame = int(sample_rate * frame_ms / 1000)
        self.end_frames = max(1, int(end_silence_ms / frame_ms))
        self.max_frames = int(max_seconds * 1000 / frame_ms)
        self.min_voiced_frames = int(min_speech_seconds * 1000 / frame_ms)
        self.preroll = deque(maxlen=max(1, int(preroll_ms / frame_ms)))
        self.tail_frames = int(tail_ms / frame_ms)
        self.ratio = ratio
        self.min_threshold = min_threshold
        self.start_frames = start_frames
        self.idle_window = max(1, int(idle_window_s * 1000 / frame_ms))
        self.history = deque(maxlen=max(self.idle_window, int(speech_window_s * 1000 / frame_ms)))
        self.noise_floor = min_threshold / ratio
        self._pending = np.zeros(0, np.float32)
        self.reset()

    # -- state
    def reset(self):
        self._speech = []          # frames of the current utterance
        self._in_speech = False
        self._loud_run = 0
        self._silent_run = 0
        self._voiced_last = 0       # index (in frames) of last loud frame in _speech
        self._first_voiced = 0
        self.preroll.clear()
        self._pending = np.zeros(0, np.float32)

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def threshold(self) -> float:
        return max(self.noise_floor * self.ratio, self.min_threshold)

    def calibrate(self, samples: np.ndarray):
        samples = np.asarray(samples, np.float32).reshape(-1)
        levels = [rms(samples[i:i + self.frame]) for i in range(0, len(samples) - self.frame + 1, self.frame)]
        if levels:
            self.noise_floor = max(float(np.median(levels)), 1e-6)
            self.history.clear()
            self.history.extend(levels)

    def _update_floor(self, level):
        self.history.append(level)
        window = list(self.history) if self._in_speech else list(self.history)[-self.idle_window:]
        self.noise_floor = max(min(window), 1e-6)

    # -- streaming
    def feed(self, samples: np.ndarray) -> list[np.ndarray]:
        samples = np.asarray(samples, np.float32).reshape(-1)
        buf = np.concatenate([self._pending, samples]) if self._pending.size else samples
        out = []
        n = len(buf) // self.frame
        for i in range(n):
            utterance = self._feed_frame(buf[i * self.frame:(i + 1) * self.frame])
            if utterance is not None:
                out.append(utterance)
        self._pending = buf[n * self.frame:].copy()
        return out

    def _feed_frame(self, frame):
        level = rms(frame)
        loud = level > self.threshold
        self._update_floor(level)
        if not self._in_speech:
            self.preroll.append(frame)
            self._loud_run = self._loud_run + 1 if loud else 0
            if self._loud_run >= self.start_frames:
                self._in_speech = True
                self._speech = list(self.preroll)
                self._first_voiced = len(self._speech) - self._loud_run
                self._voiced_last = len(self._speech) - 1
                self._silent_run = 0
                self.preroll.clear()
            return None
        self._speech.append(frame)
        if loud:
            self._silent_run = 0
            self._voiced_last = len(self._speech) - 1
        else:
            self._silent_run += 1
        if self._silent_run >= self.end_frames or len(self._speech) >= self.max_frames:
            return self._finish()
        return None

    def _finish(self):
        voiced = self._voiced_last - self._first_voiced + 1
        end = min(len(self._speech), self._voiced_last + 1 + self.tail_frames)
        frames = self._speech[:end]
        self._in_speech = False
        self._speech = []
        self._loud_run = 0
        self._silent_run = 0
        if voiced < self.min_voiced_frames:
            return None
        return np.concatenate(frames).astype(np.float32)


class Microphone:
    """Half-duplex utterance capture. ``read_frame(timeout)`` -> float32 block or None (injectable)."""

    def __init__(self, read_frame=None, sample_rate=SAMPLE_RATE, calibrate_seconds=1.0, device=None,
                 endpointer=None):
        self.sr = sample_rate
        self.calibrate_seconds = calibrate_seconds
        self.device = device
        self.endpointer = endpointer or Endpointer(sample_rate=sample_rate)
        self._read_frame = read_frame
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._stream = None
        self._calibrated = False
        self._lock = threading.RLock()

    # -- device
    def _ensure_stream(self):
        if self._read_frame is not None or self._stream is not None:
            return
        import sounddevice as sd
        self._stream = sd.InputStream(samplerate=self.sr, channels=1, dtype="float32", blocksize=int(self.sr * 0.1),
                                      device=self.device, callback=self._on_audio)
        self._stream.start()

    def _on_audio(self, indata, frames, timing, status):
        try:
            self._queue.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass  # bounded: never accumulate an audio archive

    def _read(self, timeout):
        if self._read_frame is not None:
            return self._read_frame(timeout)
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            self.flush()  # keep the calibrated noise floor: no 1 s re-calibration after every wake

    def flush(self):
        """Discard everything captured so far (e.g. our own voice while speaking)."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self.endpointer.reset()

    def calibrate(self):
        self._ensure_stream()
        need = int(self.sr * self.calibrate_seconds)
        got = []
        deadline = time.monotonic() + self.calibrate_seconds + 2
        while sum(len(g) for g in got) < need and time.monotonic() < deadline:
            block = self._read(0.2)
            if block is not None:
                got.append(np.asarray(block, np.float32).reshape(-1))
        if got:
            self.endpointer.calibrate(np.concatenate(got))
        self._calibrated = True

    def listen(self, timeout: float = 8.0, max_seconds: float | None = None):
        """Return one utterance (float32 16 kHz) or None if nobody started speaking within ``timeout``."""
        with self._lock:
            self._ensure_stream()
            if not self._calibrated:
                self.calibrate()
            ep = self.endpointer
            saved_max = ep.max_frames
            if max_seconds:
                ep.max_frames = int(max_seconds * self.sr / ep.frame)
            try:
                return self._listen(ep, timeout)
            finally:
                ep.max_frames = saved_max

    def _listen(self, ep, timeout):
        wall_deadline = time.monotonic() + timeout
        audio_budget = int(timeout * self.sr)
        hard_deadline = wall_deadline + ep.max_frames * ep.frame / self.sr + 2  # dead stream guard
        consumed = 0
        while True:
            if time.monotonic() > hard_deadline:
                ep.reset()
                return None
            if not ep.in_speech and (time.monotonic() > wall_deadline or consumed > audio_budget):
                return None
            block = self._read(0.2)
            if block is None:
                if self._read_frame is not None and time.monotonic() > wall_deadline:
                    return None
                continue
            block = np.asarray(block, np.float32).reshape(-1)
            consumed += len(block)
            done = ep.feed(block)
            if done:
                return done[0]


# ------------------------------------------------------------------ decoding

def _wav_to_float(data: bytes):
    with wave.open(io.BytesIO(data)) as w:
        ch, width, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif width == 1:
        x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128.0
    elif width == 4:
        x = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported wav sample width {width}")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def _av_to_float(data: bytes, target_rate):
    import av
    out = []
    with av.open(io.BytesIO(data)) as container:
        stream = container.streams.audio[0]
        rate = target_rate or stream.rate or SAMPLE_RATE
        resampler = av.AudioResampler(format="flt", layout="mono", rate=rate)
        for frame in container.decode(stream):
            for r in resampler.resample(frame):
                out.append(r.to_ndarray().reshape(-1))
        for r in resampler.resample(None):
            out.append(r.to_ndarray().reshape(-1))
    x = np.concatenate(out).astype(np.float32) if out else np.zeros(0, np.float32)
    return x, rate


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or x.size == 0:
        return x.astype(np.float32)
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def decode_audio(data: bytes, target_rate: int | None = None):
    """bytes (wav/mp3/ogg/...) -> (mono float32, sample_rate)."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        try:
            x, sr = _wav_to_float(data)
            if target_rate:
                x, sr = resample(x, sr, target_rate), target_rate
            return x, sr
        except (wave.Error, ValueError):
            pass  # e.g. float WAV: let PyAV handle it
    return _av_to_float(data, target_rate)


def decode_to_float(data: bytes, target_rate: int = SAMPLE_RATE) -> np.ndarray:
    return decode_audio(data, target_rate)[0]


def to_wav_bytes(x: np.ndarray, sample_rate: int) -> bytes:
    pcm = (np.clip(np.asarray(x, np.float32), -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def beep(freq=880.0, seconds=0.12, rate=SAMPLE_RATE) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    env = np.minimum(1, np.minimum(t, seconds - t) * 40)
    return to_wav_bytes(0.25 * env * np.sin(2 * np.pi * freq * t), rate)


# ------------------------------------------------------------------ playback

class WinmmBackend:
    """Windows PlaySoundW on a WAV file: async, stoppable, stdlib-only (ctypes)."""
    SND_ASYNC, SND_NODEFAULT, SND_FILENAME = 0x0001, 0x0002, 0x00020000

    def __init__(self):
        import ctypes
        self._play = ctypes.windll.winmm.PlaySoundW
        self._play.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint]
        self._play.restype = ctypes.c_int

    def play_file(self, path) -> bool:
        return bool(self._play(str(path), None, self.SND_FILENAME | self.SND_ASYNC | self.SND_NODEFAULT))

    def stop(self):
        self._play(None, None, 0)


class SounddeviceBackend:
    def play_file(self, path) -> bool:
        import sounddevice as sd
        x, sr = decode_audio(open(path, "rb").read())
        sd.play(x, sr)
        return True

    def stop(self):
        import sounddevice as sd
        sd.stop()


def default_backend():
    if sys.platform == "win32":
        return WinmmBackend()
    return SounddeviceBackend()


def output_device_available(sd_module=None) -> bool:
    try:
        sd = sd_module or __import__("sounddevice")
        return sd.query_devices(kind="output")["max_output_channels"] > 0
    except Exception:
        return False


class Player:
    """Plays audio bytes; ``interrupt()`` stops at once; ``is_playing`` reflects the current clip."""

    def __init__(self, backend=None):
        self._backend = backend
        self._lock = threading.RLock()
        self._end = 0.0
        self._file = None
        self._generation = 0
        self.last_ok = None  # did the OS accept the clip? (False on machines without an audio device)

    @property
    def backend(self):
        if self._backend is None:
            self._backend = default_backend()
        return self._backend

    def play(self, data: bytes, block: bool = True) -> bool:
        x, sr = decode_audio(data)
        if x.size == 0:
            return False
        wav = to_wav_bytes(x, sr)
        fd, path = tempfile.mkstemp(prefix="jarvis-say-", suffix=".wav")
        with os.fdopen(fd, "wb") as f:
            f.write(wav)
        with self._lock:
            self.interrupt()
            self._generation += 1
            gen = self._generation
            self._file = path
            ok = self.backend.play_file(path)
            self.last_ok = ok
            self._end = time.monotonic() + len(x) / sr + 0.05 if ok else 0.0
        if block:
            while self.is_playing and gen == self._generation:
                time.sleep(0.05)
        return ok

    @property
    def is_playing(self) -> bool:
        return time.monotonic() < self._end

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.is_playing and (deadline is None or time.monotonic() < deadline):
            time.sleep(0.05)

    def interrupt(self):
        with self._lock:
            if self._end:
                try:
                    self.backend.stop()
                except Exception:
                    pass
            self._end = 0.0
            old, self._file = self._file, None
        if old:
            threading.Timer(1.0, _unlink_quietly, args=(old,)).start()


def _unlink_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass
