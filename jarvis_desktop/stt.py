"""Speech-to-text: local faster-whisper (default) or provider /audio/transcriptions.

Local models are cached in %LOCALAPPDATA%/Jarvis/models and run on CPU with int8.
In provider mode a failed request falls back to the local model.
"""
from __future__ import annotations

import threading

import httpx
import numpy as np

from .audio import SAMPLE_RATE, to_wav_bytes


class LocalWhisper:
    def __init__(self, model_size="base", download_root=None, language=None):
        self.model_size = model_size if model_size in ("tiny", "base", "small") else "base"
        self.download_root = download_root
        self.language = language
        self._model = None
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                root = self.download_root
                if root is None:
                    from .config import models_dir
                    root = str(models_dir() / "whisper")
                self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8", download_root=root)
        return self._model

    def transcribe(self, pcm: np.ndarray) -> str:
        if pcm is None or len(pcm) < SAMPLE_RATE * 0.2:
            return ""
        model = self.load()
        lang = self.language if self.language and self.language not in ("auto", "") else None
        segments, _ = model.transcribe(np.asarray(pcm, np.float32), language=lang, beam_size=1,
                                       vad_filter=False, condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()


class ProviderSTT:
    def __init__(self, base_url, api_key_getter, model="whisper-1", language=None, transport=None, timeout=60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key_getter = api_key_getter
        self.model = model
        self.language = language
        self.transport = transport
        self.timeout = timeout

    def transcribe(self, pcm: np.ndarray) -> str:
        wav = to_wav_bytes(pcm, SAMPLE_RATE)
        data = {"model": self.model}
        if self.language and self.language not in ("auto", ""):
            data["language"] = self.language
        with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
            r = client.post(self.base_url + "/audio/transcriptions",
                            headers={"Authorization": f"Bearer {self.api_key_getter()}"},
                            files={"file": ("speech.wav", wav, "audio/wav")}, data=data)
        r.raise_for_status()
        return (r.json().get("text") or "").strip()


class Transcriber:
    """Facade chosen by settings.stt_mode."""

    def __init__(self, settings, api_key_getter, local=None, provider=None):
        self.settings = settings
        self.local = local or LocalWhisper(settings.stt_model, language=settings.language or None)
        self.provider = provider or (ProviderSTT(settings.base_url, api_key_getter, language=settings.language)
                                     if settings.stt_mode == "provider" else None)

    def transcribe(self, pcm) -> str:
        if self.provider is not None:
            try:
                return self.provider.transcribe(pcm)
            except Exception:
                pass
        return self.local.transcribe(pcm)
