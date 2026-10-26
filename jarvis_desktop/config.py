"""Settings for Jarvis Desktop.

Non-secret settings live in %LOCALAPPDATA%/Jarvis/settings.json. The provider API key is stored ONLY in
Windows Credential Manager through ``keyring`` (service "Jarvis", user "provider_api_key").
JARVIS_LLM_API_KEY in the environment overrides it (headless tests / scripted installs).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

KEYRING_SERVICE = "Jarvis"
KEYRING_USER = "provider_api_key"
AUTONOMY_LEVELS = ("careful", "balanced", "autonomous")
AUTONOMY_HELP = {
    "careful": "Every desktop action is read back and needs your spoken yes.",
    "balanced": "Safe, reversible actions run at once; sending, buying, deleting and scheduling need a yes.",
    "autonomous": "Only purchases, deletions and sending messages need a spoken yes.",
}
STT_MODES = ("local", "provider")


def app_home() -> Path:
    override = os.getenv("JARVIS_DESKTOP_HOME")
    if override:
        return Path(override)
    base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Jarvis"


def settings_path() -> Path:
    return app_home() / "settings.json"


def models_dir() -> Path:
    return app_home() / "models"


@dataclass
class Settings:
    provider: str = ""  # openai | anthropic | azure | gemini | ollama | litellm | ... ("" = detect from URL/key)
    base_url: str = "https://api.openai.com/v1"
    chat_model: str = "gpt-4o-mini"
    tts_model: str = "tts-1"
    tts_voice: str = "alloy"
    stt_mode: str = "local"
    stt_model: str = "base"
    autonomy: str = "balanced"
    wake_word: str = "jarvis"
    wake_model_path: str = ""
    language: str = "en"
    start_with_windows: bool = False

    def normalized(self) -> "Settings":
        d = Settings()
        self.base_url = (str(self.base_url or d.base_url)).strip().rstrip("/")
        if self.autonomy not in AUTONOMY_LEVELS:
            self.autonomy = d.autonomy
        if self.stt_mode not in STT_MODES:
            self.stt_mode = d.stt_mode
        if self.stt_model not in ("tiny", "base", "small"):
            self.stt_model = d.stt_model
        self.start_with_windows = bool(self.start_with_windows)
        for f in ("provider", "chat_model", "tts_model", "tts_voice", "wake_word", "language", "wake_model_path"):
            value = getattr(self, f)
            setattr(self, f, str(value).strip() if value is not None else getattr(d, f))
        return self


def load_settings() -> Settings:
    path = settings_path()
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    known = {f.name for f in fields(Settings)}
    s = Settings(**{k: v for k, v in data.items() if k in known})
    return s.normalized()


def save_settings(settings: Settings) -> Path:
    settings.normalized()
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _keyring():
    import keyring
    return keyring


def key_source() -> str | None:
    if os.getenv("JARVIS_LLM_API_KEY"):
        return "environment"
    try:
        if _keyring().get_password(KEYRING_SERVICE, KEYRING_USER):
            return "credential_manager"
    except Exception:
        return None
    return None


def get_api_key() -> str | None:
    env = os.getenv("JARVIS_LLM_API_KEY")
    if env:
        return env
    try:
        return _keyring().get_password(KEYRING_SERVICE, KEYRING_USER) or None
    except Exception:
        return None


def set_api_key(value: str) -> None:
    if not value or not value.strip():
        raise ValueError("API key is empty")
    _keyring().set_password(KEYRING_SERVICE, KEYRING_USER, value.strip())


def delete_api_key() -> None:
    try:
        _keyring().delete_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:
        pass


def is_configured() -> bool:
    if not settings_path().is_file():
        return False
    return bool(get_api_key()) or load_settings().provider == "ollama"  # local Ollama needs no key


def wake_model_path(settings: Settings | None = None) -> str | None:
    """Vosk wake model: env JARVIS_WAKE_MODEL_PATH > settings > %LOCALAPPDATA%/Jarvis/models/vosk-*."""
    env = os.getenv("JARVIS_WAKE_MODEL_PATH")
    if env:
        return env
    if settings and settings.wake_model_path:
        return settings.wake_model_path
    root = models_dir()
    if root.is_dir():
        for candidate in sorted(root.glob("vosk-model*")):
            if (candidate / "am" / "final.mdl").is_file():
                return str(candidate)
    return None
