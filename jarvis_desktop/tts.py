"""Text-to-speech: provider /audio/speech, falling back to offline Windows System.Speech.

The offline path shells out to Windows PowerShell (System.Speech.Synthesis.SpeechSynthesizer ->
SetOutputToWaveFile), so no pywin32/comtypes dependency is needed. Text is passed through a temp
file, never interpolated into the script.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unicodedata

import httpx

_URL = re.compile(r"(https?://|www\.)\S+", re.I)
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_CODE_BLOCK = re.compile(r"```.*?```", re.S)


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    if 0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x1F900 <= cp <= 0x1F9FF:
        return True
    if cp in (0xFE0F, 0x200D, 0x20E3) or 0xE0020 <= cp <= 0xE007F or 0x2B00 <= cp <= 0x2BFF:
        return True
    return unicodedata.category(ch) == "So"


def sanitize_for_speech(text: str) -> str:
    """Strip markdown, emoji, URLs and JSON so the voice reads naturally."""
    text = (text or "").strip()
    if not text:
        return ""
    if text[:1] in "{[" and text[-1:] in "}]":
        try:
            json.loads(text)
            return ""
        except ValueError:
            pass
    text = _CODE_BLOCK.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)          # headings
        line = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", line)   # list bullets
        line = re.sub(r"^\s*>\s?", "", line)                    # quotes
        line = line.replace("|", " ")
        line = line.strip()
        if line and not re.fullmatch(r"[-=_*\s:]+", line):
            if lines and not re.search(r"[.!?।:;,]$", lines[-1]):
                lines[-1] += "."
            lines.append(line)
    text = " ".join(lines)
    text = re.sub(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1", r"\2", text)
    text = re.sub(r"[*#~^<>{}\[\]\\]", " ", text)
    text = "".join(ch for ch in text if not _is_emoji(ch))
    text = re.sub(r"\s+([.,!?।])", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\.{2,}", ".", text)
    return text


class TTSError(RuntimeError):
    pass


def provider_speech(base_url: str, api_key: str, model: str, voice: str, text: str, transport=None,
                    timeout=60.0) -> bytes:
    """POST {base}/audio/speech -> audio bytes. Retries once without ``voice`` if the provider rejects it."""
    url = base_url.rstrip("/") + "/audio/speech"
    headers = {"Authorization": f"Bearer {api_key}"}
    voices = [v for v in dict.fromkeys([voice, "alloy"]) if v]  # some gateways require a voice, some reject unknown ones
    bodies = [{"model": model, "voice": v, "input": text} for v in voices] + [{"model": model, "input": text}]
    last = None
    with httpx.Client(transport=transport, timeout=timeout) as client:
        for body in bodies:
            r = client.post(url, headers=headers, json=body)
            if r.status_code == 200 and r.content and not r.headers.get("content-type", "").startswith(
                    ("application/json", "text/")):
                return r.content
            last = r.status_code
            if r.status_code not in (400, 422):
                break
    raise TTSError(f"provider speech failed (HTTP {last})")


_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$text = [System.IO.File]::ReadAllText($args[0], [System.Text.Encoding]::UTF8)
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  if ($args[2]) { foreach ($v in $s.GetInstalledVoices()) { if ($v.VoiceInfo.Culture.Name -like ($args[2] + '*')) { $s.SelectVoice($v.VoiceInfo.Name); break } } }
  $s.SetOutputToWaveFile($args[1])
  $s.Speak($text)
} finally { $s.Dispose() }
"""


def sapi_speech(text: str, language: str = "", timeout: float = 60.0) -> bytes:
    """Offline Windows System.Speech -> WAV bytes."""
    if sys.platform != "win32":
        raise TTSError("offline speech is only available on Windows")
    tmpdir = tempfile.mkdtemp(prefix="jarvis-sapi-")
    txt, wav, ps1 = (os.path.join(tmpdir, n) for n in ("in.txt", "out.wav", "say.ps1"))
    try:
        with open(txt, "w", encoding="utf-8") as f:
            f.write(text)
        with open(ps1, "w", encoding="utf-8") as f:
            f.write(_PS_SCRIPT)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", ps1, txt, wav, language or ""], capture_output=True, timeout=timeout,
                              creationflags=flags)
        if proc.returncode != 0 or not os.path.isfile(wav):
            raise TTSError("offline speech failed")
        with open(wav, "rb") as f:
            return f.read()
    finally:
        for p in (txt, wav, ps1):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


class Speaker:
    """say(text) -> sanitize -> provider TTS (fallback SAPI) -> Player. Thread-safe, interruptible."""

    def __init__(self, settings, api_key_getter, player=None, transport=None, offline_only=False):
        from .audio import Player
        self.settings = settings
        self.api_key_getter = api_key_getter
        self.player = player or Player()
        self.transport = transport
        self.offline_only = offline_only
        self.last_engine = None
        self._lock = threading.Lock()

    def synthesize(self, text: str) -> bytes:
        key = None if self.offline_only else self.api_key_getter()
        if key and self.settings.tts_model:
            from .providers import Provider, ProviderError, detect_provider
            kind = getattr(self.settings, "provider", "") or detect_provider(self.settings.base_url, key)
            if kind in ("openai", "litellm"):
                try:  # generic path: tolerant of gateways' voice quirks
                    audio = provider_speech(self.settings.base_url, key, self.settings.tts_model,
                                            self.settings.tts_voice, text, transport=self.transport)
                    self.last_engine = "provider"
                    return audio
                except Exception:
                    pass
            else:
                p = Provider(kind, self.settings.base_url, key, transport=self.transport, timeout=60)
                try:
                    audio = p.speech(self.settings.tts_model, self.settings.tts_voice, text)
                    self.last_engine = "provider"
                    return audio
                except (ProviderError, httpx.HTTPError):
                    pass
                finally:
                    p.close()
        audio = sapi_speech(text, language=self.settings.language if self.settings.language != "en" else "")
        self.last_engine = "offline"
        return audio

    def say(self, text: str, block: bool = True) -> bool:
        clean = sanitize_for_speech(text)
        if not clean:
            return False
        with self._lock:
            try:
                audio = self.synthesize(clean)
            except Exception:
                return False
        return self.player.play(audio, block=block)

    def interrupt(self):
        self.player.interrupt()

    @property
    def is_playing(self) -> bool:
        return self.player.is_playing
