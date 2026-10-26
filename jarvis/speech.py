"""Lazy CPU speech recognition. PyAV decodes in-process; no shell/ffmpeg."""
import io
import threading
import time
from fastapi import HTTPException


class Speech:
    def __init__(self):
        self.model = None
        self.lock = threading.Lock()

    @staticmethod
    def available():
        try:
            import faster_whisper
            import av
            return True
        except (ImportError, OSError):
            return False

    def transcribe(self, data):
        if not self.lock.acquire(blocking=False):
            raise HTTPException(429, "Speech recognition is busy")
        try:
            try:
                import av
                import numpy as np
                from faster_whisper import WhisperModel
            except (ImportError, OSError):
                raise HTTPException(503, "Speech dependencies unavailable; install the voice extra and Windows VC++ runtime") from None
            samples, count = [], 0
            started = time.monotonic()
            try:
                with av.open(io.BytesIO(data)) as container:
                    if not container.streams.audio:
                        raise ValueError("No audio")
                    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
                    for frame in container.decode(audio=0):
                        if time.monotonic() - started > 20:
                            raise HTTPException(422, "Audio decoding time limit exceeded")
                        for converted in resampler.resample(frame):
                            sample = converted.to_ndarray().reshape(-1)
                            count += sample.size
                            if count > 16000 * 120:
                                raise HTTPException(413, "Audio must be at most 120 seconds")
                            samples.append(sample)
                    for converted in resampler.resample(None):
                        sample = converted.to_ndarray().reshape(-1)
                        count += sample.size
                        samples.append(sample)
                    if not samples or count > 16000 * 120:
                        raise HTTPException(422, "Invalid or oversized audio")
                audio = np.concatenate(samples).astype(np.float32) / 32768.0
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(422, "Cannot decode this audio file") from None
            try:
                if self.model is None:
                    self.model = WhisperModel("base", device="cpu", compute_type="int8", cpu_threads=2, num_workers=1)
                segments, _ = self.model.transcribe(audio, beam_size=1, vad_filter=True, condition_on_previous_text=False)
                text = " ".join(segment.text.strip() for segment in segments).strip()
                return text[:8000]
            except Exception:
                raise HTTPException(503, "Speech model unavailable; use text or retry after model download") from None
        finally:
            self.lock.release()
