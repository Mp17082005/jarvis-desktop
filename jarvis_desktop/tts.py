"""Text-to-speech: provider /audio/speech, falling back to offline Windows System.Speech.

Replies are spoken sentence-pipelined: the first (short) sentence is synthesized and starts playing at
once while the next chunk is synthesized in the background, so time-to-first-audio is one short TTS
request instead of the whole reply.

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
import time
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
                    timeout=60.0, client: httpx.Client | None = None) -> bytes:
    """POST {base}/audio/speech -> audio bytes. Retries once without ``voice`` if the provider rejects it.

    Pass a long-lived ``client`` to reuse its keep-alive connection (saves the TLS handshake per chunk)."""
    url = base_url.rstrip("/") + "/audio/speech"
    headers = {"Authorization": f"Bearer {api_key}"}
    voices = [v for v in dict.fromkeys([voice, "alloy"]) if v]  # some gateways require a voice, some reject unknown ones
    bodies = [{"model": model, "voice": v, "input": text} for v in voices] + [{"model": model, "input": text}]
    last = None
    own = client is None
    if own:
        client = httpx.Client(transport=transport, timeout=timeout)
    try:
        for body in bodies:
            r = client.post(url, headers=headers, json=body)
            if r.status_code == 200 and r.content and not r.headers.get("content-type", "").startswith(
                    ("application/json", "text/")):
                return r.content
            last = r.status_code
            if r.status_code not in (400, 422):
                break
    finally:
        if own:
            client.close()
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


FIRST_CHUNK_MAX = 120   # chars: keep the first request short so audio starts quickly
CHUNK_MAX = 250         # later chunks group sentences: fewer requests, still synthesized ahead of playback
CACHE_MAX_CHARS = 40    # short fixed phrases ("On it.", "Okay.", "Goodbye.") are synthesized once
CACHE_ENTRIES = 32
MIN_CHUNK = 12          # fragments shorter than this ("Sure.", "Ok.") are merged with a neighbour
_SENTENCE_END = re.compile(r"(?<=[.!?\u0964])\s+")


def _cap(sentence: str, limit: int) -> list[str]:
    """Split one over-long sentence at the last comma/semicolon (else space) before ``limit``."""
    out = []
    while len(sentence) > limit:
        window = sentence[:limit]
        cut = max(window.rfind(", "), window.rfind("; "), window.rfind(": "))
        cut = cut + 1 if cut >= MIN_CHUNK else window.rfind(" ")
        if cut < MIN_CHUNK:
            break
        out.append(sentence[:cut].strip())
        sentence = sentence[cut:].strip()
    out.append(sentence)
    return out


def split_for_speech(text: str) -> list[str]:
    """Sanitized text -> ordered speech chunks; ``" ".join(chunks)`` reproduces the text.

    Chunk 1 is the first sentence (capped at FIRST_CHUNK_MAX at a comma); later chunks group whole
    sentences up to CHUNK_MAX; fragments under MIN_CHUNK chars are merged with a neighbour."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    sentences = [x for x in _SENTENCE_END.split(text) if x]
    chunks: list[str] = []
    cur = ""
    for sentence in sentences:
        cur = f"{cur} {sentence}".strip() if cur else sentence
        if len(cur) < MIN_CHUNK:
            continue  # tiny fragment: keep accumulating
        if not chunks:
            parts = _cap(cur, FIRST_CHUNK_MAX)
            chunks.append(parts[0])
            cur = " ".join(parts[1:])
            continue
        if len(cur) >= CHUNK_MAX:
            parts = _cap(cur, CHUNK_MAX)
            chunks.extend(parts[:-1])
            cur = parts[-1]
            if len(cur) >= CHUNK_MAX * 0.6:
                chunks.append(cur)
                cur = ""
    if cur:
        if chunks and len(cur) < MIN_CHUNK:
            chunks[-1] = f"{chunks[-1]} {cur}"
        else:
            chunks.append(cur)
    return chunks


class _Session:
    """One say() call: cancel flag, first-audio result and completion."""

    def __init__(self):
        self.cancel = threading.Event()
        self.started = threading.Event()   # set when chunk 1 began playing, or nothing will play
        self.done = threading.Event()
        self.result = False


class Speaker:
    """say(text) -> sanitize -> sentence chunks -> provider TTS (fallback SAPI) -> Player.

    Chunk N+1 is synthesized while chunk N plays. Thread-safe and interruptible: interrupt() stops the
    current clip and cancels every chunk not yet played."""

    def __init__(self, settings, api_key_getter, player=None, transport=None, offline_only=False):
        from .audio import Player
        self.settings = settings
        self.api_key_getter = api_key_getter
        self.player = player or Player()
        self.transport = transport
        self.offline_only = offline_only
        self.last_engine = None
        self.first_engine = None   # engine that produced the first chunk of the latest say()
        self._local = threading.local()      # per-thread: the say() session a synthesis belongs to
        self._lock = threading.Lock()        # guards _session
        self._play_lock = threading.Lock()   # cancel-check + play start are atomic w.r.t. interrupt()
        self._session: _Session | None = None
        self._http: httpx.Client | None = None
        self._http_lock = threading.Lock()
        self._cache: dict = {}

    # ---- provider connection
    def _client(self) -> httpx.Client:
        with self._http_lock:
            if self._http is None:
                self._http = httpx.Client(transport=self.transport, timeout=60.0,
                                          limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=120))
            return self._http

    def _uses_generic_endpoint(self, key) -> bool:
        if not key or not self.settings.tts_model:
            return False
        from .providers import detect_provider
        kind = getattr(self.settings, "provider", "") or detect_provider(self.settings.base_url, key)
        return kind in ("openai", "litellm")

    def warm_up(self):
        """Open (or refresh) the keep-alive TLS connection to the speech endpoint in the background, so the
        reply's first TTS request skips the handshake. Sends no credentials. Returns the thread or None."""
        if self.offline_only:
            return None
        try:
            if not self._uses_generic_endpoint(self.api_key_getter()):
                return None
        except Exception:
            return None
        url = httpx.URL(self.settings.base_url)
        origin = f"{url.scheme}://{url.netloc.decode()}/"

        def ping():
            try:
                self._client().head(origin, timeout=5.0)
            except Exception:
                pass
        t = threading.Thread(target=ping, daemon=True, name="jarvis-tts-warmup")
        t.start()
        return t

    def close(self):
        with self._http_lock:
            client, self._http = self._http, None
        if client is not None:
            client.close()

    # ---- synthesis
    def synthesize(self, text: str) -> bytes:
        if len(text) > CACHE_MAX_CHARS:
            return self._synthesize_uncached(text)
        key = (text, self.offline_only, self.settings.base_url, self.settings.tts_model, self.settings.tts_voice,
               self.settings.language)
        hit = self._cache.get(key)
        if hit is not None:
            self.last_engine = hit[1]
            return hit[0]
        audio = self._synthesize_uncached(text)
        if len(self._cache) >= CACHE_ENTRIES:
            self._cache.pop(next(iter(self._cache)), None)
        self._cache[key] = (audio, self.last_engine)
        return audio

    def _synthesize_uncached(self, text: str) -> bytes:
        key = None if self.offline_only else self.api_key_getter()
        if key and self.settings.tts_model:
            from .providers import Provider, ProviderError, detect_provider
            kind = getattr(self.settings, "provider", "") or detect_provider(self.settings.base_url, key)
            if kind in ("openai", "litellm"):
                try:  # generic path: tolerant of gateways' voice quirks
                    audio = provider_speech(self.settings.base_url, key, self.settings.tts_model,
                                            self.settings.tts_voice, text, client=self._client())
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
        session = getattr(self._local, "session", None)
        if session is not None and session.cancel.is_set():
            raise TTSError("speech cancelled")  # interrupted mid-request: never spawn the offline voice for it
        audio = sapi_speech(text, language=self.settings.language if self.settings.language != "en" else "")
        self.last_engine = "offline"
        return audio

    def say(self, text: str, block: bool = True) -> bool:
        """Speak ``text``. Returns True once audio was accepted by the player (False if nothing could be
        synthesized). block=False returns as soon as the first chunk is playing; ``is_playing`` stays True
        until the last chunk has finished or interrupt() is called."""
        chunks = split_for_speech(sanitize_for_speech(text))
        if not chunks:
            return False
        session = _Session()
        with self._lock:
            old, self._session = self._session, session
        if old is not None:
            self._cancel(old)
        worker = threading.Thread(target=self._run, args=(session, chunks), daemon=True, name="jarvis-speak")
        worker.start()
        session.started.wait()
        if block:
            session.done.wait()
        return session.result

    def _synth_async(self, text, session):
        box = {}
        ready = threading.Event()

        def work():
            self._local.session = session
            try:
                if not session.cancel.is_set():
                    box["audio"] = self.synthesize(text)
                    box["engine"] = self.last_engine
            except Exception:
                box["audio"] = None
            finally:
                ready.set()
        threading.Thread(target=work, daemon=True, name="jarvis-tts-prefetch").start()
        return box, ready

    def _wait(self, event, session) -> bool:
        while not event.wait(0.02):
            if session.cancel.is_set():
                return False
        return not session.cancel.is_set()

    def _run(self, session: _Session, chunks):
        try:
            pending = self._synth_async(chunks[0], session)
            for i in range(len(chunks)):
                box, ready = pending
                if not self._wait(ready, session):
                    return
                if i + 1 < len(chunks):
                    pending = self._synth_async(chunks[i + 1], session)  # synthesize ahead while this one plays
                audio = box.get("audio")
                if not audio:
                    continue  # one failed chunk is skipped, the rest still speak
                with self._play_lock:
                    if session.cancel.is_set():
                        return
                    try:
                        ok = bool(self.player.play(audio, block=False))
                    except Exception:
                        ok = False
                if ok and not session.started.is_set():
                    session.result = True
                    self.first_engine = box.get("engine")
                session.started.set()
                if ok:
                    while getattr(self.player, "is_playing", False) and not session.cancel.is_set():
                        time.sleep(0.01)
        finally:
            session.done.set()
            session.started.set()
            with self._lock:
                if self._session is session:
                    self._session = None

    def _cancel(self, session: _Session):
        session.cancel.set()
        with self._play_lock:
            try:
                self.player.interrupt()
            except Exception:
                pass

    def interrupt(self):
        with self._lock:
            session, self._session = self._session, None
        if session is not None:
            self._cancel(session)
        else:
            with self._play_lock:
                self.player.interrupt()

    @property
    def is_playing(self) -> bool:
        session = self._session
        if session is not None and not session.done.is_set() and not session.cancel.is_set():
            return True
        return bool(getattr(self.player, "is_playing", False))
