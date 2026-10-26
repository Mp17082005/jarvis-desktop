"""First-run setup: Tk window, plus a non-interactive path for scripted installs.

The API key is written only to Windows Credential Manager (keyring) and is never echoed, logged or
placed in error messages.
"""
from __future__ import annotations

import os
import re

import httpx

from . import config
from .config import AUTONOMY_HELP, AUTONOMY_LEVELS, Settings
from .providers import verify


def _redact(text: str, key: str) -> str:
    text = text or ""
    if key:
        text = text.replace(key, "[key]")
    return re.sub(r"(sk|key|Bearer)[-_ ]?[A-Za-z0-9_\-]{8,}", "[redacted]", text)


def test_connection(base_url: str, api_key: str, model: str, transport=None, timeout=20.0):
    """GET {base}/models, then a 1-token chat completion. Returns (ok, plain message). Never echoes the key."""
    base = (base_url or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        return False, "The provider URL must start with https:// (or http:// for a local server)."
    if not api_key:
        return False, "Enter your API key first."
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False) as client:
            r = client.get(base + "/models", headers=headers)
            if r.status_code in (401, 403):
                return False, f"The provider rejected the API key (HTTP {r.status_code})."
            models_note = "" if r.status_code == 200 else f" (model list unavailable, HTTP {r.status_code})"
            body = {"model": model, "messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 1}
            c = client.post(base + "/chat/completions", headers=headers, json=body)
            if c.status_code != 200:
                hint = {401: "the API key was rejected", 403: "the key is not allowed to use this model",
                        404: "the model or URL was not found", 429: "rate limited or out of credit"}.get(
                    c.status_code, "the provider returned an error")
                return False, _redact(f"Chat test failed (HTTP {c.status_code}): {hint}.", api_key)
            c.json()["choices"]
    except httpx.TimeoutException:
        return False, "The provider did not answer in time. Check the URL and your internet connection."
    except httpx.HTTPError as exc:
        return False, _redact(f"Could not reach the provider ({type(exc).__name__}).", api_key)
    except (ValueError, KeyError, TypeError):
        return False, "The provider answered, but not like an OpenAI-compatible API."
    return True, f"Connected. Model '{model}' answered{models_note}."


def apply_setup(base_url: str, api_key: str | None, model: str, autonomy: str = "balanced", **extra) -> Settings:
    s = config.load_settings()
    s.base_url, s.chat_model, s.autonomy = base_url, model, autonomy
    for k, v in extra.items():
        if v is not None and hasattr(s, k):
            setattr(s, k, v)
    if api_key:
        config.set_api_key(api_key)
    config.save_settings(s)
    return s


def resolve_provider(provider: str | None, base_url: str | None, key: str | None):
    """(kind, base_url) from what the user gave: explicit type wins, else detect from URL/key."""
    from .providers import PROVIDERS, detect_provider
    kind = (provider or "").strip().lower() or detect_provider(base_url or "", key or "")
    if kind not in PROVIDERS:
        raise ValueError(f"Unknown provider '{kind}'. Choose one of: {', '.join(PROVIDERS)}.")
    base = (base_url or "").strip() or PROVIDERS[kind].default_base
    return kind, base


def non_interactive(args) -> int:
    from .providers import PROVIDERS
    key = os.getenv("JARVIS_LLM_API_KEY") or ""
    try:
        kind, base = resolve_provider(getattr(args, "provider", None), args.base_url, key)
    except ValueError as e:
        print(e)
        return 2
    info = PROVIDERS[kind]
    if info.needs_key and not key:
        print("Set JARVIS_LLM_API_KEY in the environment for a non-interactive setup.")
        return 2
    if not base:
        print("--base-url is required for this provider.")
        return 2
    model = args.model or info.default_model
    if not model:
        print("--model is required for this provider.")
        return 2
    autonomy = args.autonomy or "balanced"
    if autonomy not in AUTONOMY_LEVELS:
        print("--autonomy must be careful, balanced or autonomous.")
        return 2
    if args.test:
        ok, msg, _ = verify(kind, base, key, model)
        print(msg)
        if not ok:
            return 1
    tts_model = args.tts_model if args.tts_model is not None else info.tts_model
    apply_setup(base, key or None, model, autonomy, provider=kind, tts_model=tts_model, tts_voice=args.tts_voice,
                stt_mode=args.stt_mode, language=args.language)
    where = " and stored the API key in Windows Credential Manager" if key else ""
    print(f"Saved settings to {config.settings_path()}{where}.")
    return 0


def run_wizard() -> bool:  # pragma: no cover - GUI
    """Show the Tk setup window. Returns True when the user saved."""
    import threading
    import tkinter as tk
    from tkinter import ttk

    s = config.load_settings()
    saved = {"ok": False}
    root = tk.Tk()
    root.title("Jarvis setup")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=16)
    frm.grid()
    ttk.Label(frm, text="Connect Jarvis to your AI provider", font=("Segoe UI", 12, "bold")).grid(
        row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

    from .providers import PROVIDERS, detect_provider
    labels = {info.label: kind for kind, info in PROVIDERS.items()}
    current_kind = s.provider or detect_provider(s.base_url, config.get_api_key() or "")
    provider_label = tk.StringVar(value=PROVIDERS.get(current_kind, PROVIDERS["openai"]).label)
    base = tk.StringVar(value=s.base_url)
    key = tk.StringVar(value="")
    model = tk.StringVar(value=s.chat_model)
    tts_model = tk.StringVar(value=s.tts_model)
    voice = tk.StringVar(value=s.tts_voice)
    autonomy = tk.StringVar(value=s.autonomy)
    note = tk.StringVar(value=PROVIDERS.get(current_kind, PROVIDERS["openai"]).notes)
    status = tk.StringVar(value="Your key is stored in Windows Credential Manager, never in a file."
                          if not config.get_api_key() else "A key is already saved. Leave blank to keep it.")

    ttk.Label(frm, text="Provider").grid(row=1, column=0, sticky="w", pady=3)
    combo = ttk.Combobox(frm, textvariable=provider_label, values=list(labels), state="readonly", width=44)
    combo.grid(row=1, column=1, columnspan=2, sticky="we", pady=3)

    def on_provider(_event=None):
        info = PROVIDERS[labels[provider_label.get()]]
        base.set(info.default_base)
        if info.default_model:
            model.set(info.default_model)
        tts_model.set(info.tts_model)
        note.set(info.notes or ("No key needed." if not info.needs_key else ""))
    combo.bind("<<ComboboxSelected>>", on_provider)

    rows = [("Provider URL", base, {}), ("API key", key, {"show": "\u2022"}), ("Model", model, {}),
            ("Voice model (blank = Windows voice)", tts_model, {}), ("Voice name", voice, {})]
    for i, (label, var, kw) in enumerate(rows, start=2):
        ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=var, width=46, **kw).grid(row=i, column=1, columnspan=2, sticky="we", pady=3)
    ttk.Label(frm, textvariable=note, wraplength=420, foreground="#555").grid(row=len(rows) + 2, column=0, columnspan=3, sticky="w")

    r = len(rows) + 3
    ttk.Label(frm, text="How much should Jarvis ask before acting?").grid(row=r, column=0, columnspan=3,
                                                                          sticky="w", pady=(12, 2))
    for j, level in enumerate(AUTONOMY_LEVELS, start=1):
        ttk.Radiobutton(frm, text=f"{level.capitalize()}: {AUTONOMY_HELP[level]}", value=level,
                        variable=autonomy).grid(row=r + j, column=0, columnspan=3, sticky="w")
    r += len(AUTONOMY_LEVELS) + 1
    ttk.Label(frm, textvariable=status, wraplength=420, foreground="#333").grid(row=r, column=0, columnspan=3,
                                                                                sticky="w", pady=(12, 6))

    def current_key():
        return key.get().strip() or config.get_api_key() or ""

    def kind():
        return labels[provider_label.get()]

    def do_test():
        status.set("Testing: one short reply and one tool call...")
        test_btn.state(["disabled"])

        def work():
            ok, msg, _ = verify(kind(), base.get().strip(), current_key(), model.get().strip())
            root.after(0, lambda: (status.set(("\u2714 " if ok else "\u2716 ") + msg), test_btn.state(["!disabled"])))
        threading.Thread(target=work, daemon=True).start()

    def do_save():
        if PROVIDERS[kind()].needs_key and not current_key():
            status.set("Enter your API key.")
            return
        if not model.get().strip():
            status.set("Enter a model name.")
            return
        if not base.get().strip():
            status.set("Enter the provider URL.")
            return
        apply_setup(base.get().strip(), key.get().strip() or None, model.get().strip(), autonomy.get(), provider=kind(),
                    tts_model=tts_model.get().strip(), tts_voice=voice.get().strip())
        saved["ok"] = True
        root.destroy()

    test_btn = ttk.Button(frm, text="Test connection", command=do_test)
    test_btn.grid(row=r + 1, column=0, sticky="w")
    ttk.Button(frm, text="Cancel", command=root.destroy).grid(row=r + 1, column=1, sticky="e")
    ttk.Button(frm, text="Save", command=do_save).grid(row=r + 1, column=2, sticky="e")
    root.mainloop()
    return saved["ok"]
