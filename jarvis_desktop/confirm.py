"""Spoken confirmation: speak the exact question, listen once, accept only a clear yes.

Half-duplex by construction: the question is played to completion (blocking) and the microphone is
flushed before listening, so Jarvis's own voice can never answer its own question.
"""
from __future__ import annotations

import re
import unicodedata

LISTEN_SECONDS = 8.0

_YES_PHRASES = {
    "yes", "yeah", "yep", "yup", "ya", "sure", "ok", "okay", "o k", "alright", "all right", "confirm", "confirmed",
    "go ahead", "do it", "proceed", "affirmative", "correct", "go", "please do", "yes please", "yes do it",
    "yes go ahead", "go for it", "sure go ahead", "yeah go ahead", "okay go ahead", "ok go ahead", "ok do it",
    "okay do it", "yes confirm", "yes proceed", "absolutely", "of course", "definitely",
    # Hindi / Hinglish
    "haan", "haa", "ha", "han", "haan ji", "ji haan", "ji ha", "ji", "theek hai", "thik hai", "theek he",
    "haan kar do", "kar do", "karo", "haan karo", "chalo", "haan chalo", "bilkul", "haan bilkul",
    "हाँ", "हां", "हा", "जी हाँ", "जी हां", "हाँ जी", "हां जी", "ठीक है", "कर दो", "हाँ कर दो", "हां कर दो", "बिल्कुल",
}
_FILLERS = {"please", "jarvis", "now", "ji", "sir", "yes", "yeah", "ok", "okay", "sure", "haan", "हाँ", "हां"}
_NEGATIONS = {"no", "not", "dont", "don't", "nope", "never", "cancel", "stop", "wait", "hold", "nahi", "nahin",
              "na", "mat", "ruko", "nai", "नहीं", "नही", "मत", "रुको", "but", "maybe", "later"}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", (text or "").lower())
    text = text.replace("’", "'")
    text = re.sub(r"[^\w\s'\u0900-\u097F]", " ", text)
    text = text.replace("'", "")
    return re.sub(r"\s+", " ", text).strip()


def is_clear_yes(text: str) -> bool:
    norm = _normalize(text)
    if not norm:
        return False
    words = norm.split()
    if any(w in _NEGATIONS or w.replace("'", "") in _NEGATIONS for w in words) or "dont" in words:
        return False
    if norm in _YES_PHRASES:
        return True
    # allow polite padding around a yes phrase: "yes please", "okay jarvis go ahead", "haan ji kar do"
    if len(words) > 5:
        return False
    stripped = [w for w in words if w not in ("please", "jarvis", "sir", "now")]
    candidate = " ".join(stripped)
    if candidate in _YES_PHRASES:
        return True
    # e.g. "sure, go ahead": every word is either filler or part of a known yes phrase
    joined = candidate
    for phrase in sorted(_YES_PHRASES, key=len, reverse=True):
        joined = re.sub(rf"(^| ){re.escape(phrase)}(?= |$)", " ", joined)
    return joined.strip() == "" or all(w in _FILLERS for w in joined.split())


class SpokenConfirm:
    """``confirm(question) -> bool`` as required by agent.Agent."""

    def __init__(self, speak, listen, transcribe, before_listen=None, listen_seconds=LISTEN_SECONDS):
        self.speak = speak                  # blocking: returns after playback finished
        self.listen = listen                # listen(timeout) -> pcm | None
        self.transcribe = transcribe        # transcribe(pcm) -> str
        self.before_listen = before_listen  # e.g. mic.flush, to drop our own voice
        self.listen_seconds = listen_seconds
        self.last_heard = ""

    def __call__(self, question: str) -> bool:
        self.last_heard = ""
        try:
            self.speak(question)
            if self.before_listen:
                self.before_listen()
            pcm = self.listen(self.listen_seconds)
            if pcm is None:
                return False
            self.last_heard = self.transcribe(pcm) or ""
        except Exception:
            return False
        return is_clear_yes(self.last_heard)
