"""First-run setup: a one-step Tk window (paste key -> Connect), plus a non-interactive path for scripts.

The window's logic lives in pure functions (``suggest``, ``fetch_models``, ``connect``) so it is tested
without a display; the Tk code only wires widgets to them.

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


_URL_PROVIDERS_NOTE = "Paste the address from your provider's portal."
_NOT_CHAT = re.compile(r"(embed|whisper|tts|dall-e|moderation|davinci|babbage|transcribe|image|realtime|audio|"
                       r"rerank|guard|speech)", re.I)


def suggest(key: str = "", base_url: str = "", provider: str | None = None) -> dict:
    """Everything the setup window can infer from a pasted key (and optional URL / explicit provider).

    Returns dict(kind, base_url, model, tts_model, needs_key, needs_url, detected, label, notes).
    ``detected`` is True only when the provider was recognised (explicit choice, URL host or key prefix),
    so the window does not overwrite a manual choice with a mere fallback guess.
    """
    from .providers import _KEY_PREFIX, PROVIDERS, detect_provider
    key = (key or "").strip()
    base_url = (base_url or "").strip().rstrip("/")
    explicit = (provider or "").strip().lower()
    kind = explicit or detect_provider(base_url, key)
    if kind not in PROVIDERS:
        raise ValueError(f"Unknown provider '{kind}'. Choose one of: {', '.join(PROVIDERS)}.")
    info = PROVIDERS[kind]
    needs_url = not info.default_base or "YOUR-" in info.default_base
    detected = bool(explicit or base_url or any(key.startswith(p) for p, _ in _KEY_PREFIX) or key.startswith("sk-"))
    return {"kind": kind, "label": info.label, "notes": info.notes,
            "base_url": base_url or ("" if needs_url else info.default_base),
            "model": info.default_model, "tts_model": info.tts_model,
            "needs_key": info.needs_key, "needs_url": needs_url, "detected": detected}


def fetch_models(kind: str, base_url: str, api_key: str, transport=None, timeout: float = 8.0) -> list[str]:
    """Chat model ids for the model dropdown, default first. Never raises; falls back to the default."""
    from .providers import PROVIDERS, Provider
    info = PROVIDERS.get(kind)
    default = info.default_model if info else ""
    fallback = [default] if default else []
    if info is None or (info.needs_key and not api_key):
        return fallback
    if (not info.default_base or "YOUR-" in info.default_base) and not (base_url or "").strip():
        return fallback
    p = Provider(kind=kind, base_url=base_url, api_key=api_key, transport=transport, timeout=timeout)
    try:
        ids = p.list_models()
    except Exception:
        return fallback
    finally:
        p.close()
    out = list(dict.fromkeys(m for m in ids if m and not _NOT_CHAT.search(m)))[:300]
    if not out:
        return fallback
    if default in out:
        out.remove(default)
        out.insert(0, default)
    return out


def connect(kind: str, base_url: str, api_key: str, model: str, autonomy: str = "balanced", tts_model: str | None = None,
            tts_voice: str | None = None, verify: bool = True, verify_fn=None) -> tuple[bool, str]:
    """The window's one button: check the inputs, run the real chat+tool test, and save on success.

    A blank key means "keep the saved key". With verify=False ("Save anyway") nothing is sent.
    Nothing is saved when the test fails. The key never appears in the returned message.
    """
    from .providers import PROVIDERS
    info = PROVIDERS.get(kind)
    if info is None:
        return False, f"Unknown provider type '{kind}'."
    typed = (api_key or "").strip()
    key = typed or (config.get_api_key() or "")
    base, model = (base_url or "").strip().rstrip("/"), (model or "").strip()
    if info.needs_key and not key:
        return False, "Paste your API key first."
    if not base or "YOUR-" in base:
        return False, "Enter the provider URL (" + (info.notes or _URL_PROVIDERS_NOTE) + ")"
    if not model:
        return False, "Choose a model" + (" (for Azure: your deployment name)." if kind == "azure" else ".")
    msg = "Saved without testing."
    if verify:
        check = verify_fn or globals()["verify"]
        ok, msg, _ = check(kind, base, key if info.needs_key or key else "", model)
        msg = _redact(msg, key)
        if not ok:
            return False, msg
    apply_setup(base, typed or None, model, autonomy if autonomy in AUTONOMY_LEVELS else "balanced", provider=kind,
                tts_model=tts_model, tts_voice=tts_voice)
    return True, msg


class _WizardUI:  # GUI glue; logic is in suggest/fetch_models/connect
    def __init__(self, root):
        import queue
        import threading
        import tkinter as tk
        from tkinter import ttk

        from . import hotkey as hk
        from .providers import PROVIDERS, detect_provider
        self.tk, self.ttk, self.threading, self.PROVIDERS = tk, ttk, threading, PROVIDERS
        self.root = root
        self.saved = False
        self._queue = queue.Queue()
        self._gen = 0
        self._fetch_job = None
        self._models_done = threading.Event(); self._models_done.set()
        self._connect_done = threading.Event(); self._connect_done.set()
        self._model_auto = True
        self._adv_open = False
        s = config.load_settings()
        self.settings = s
        have_key = bool(config.get_api_key())
        configured = config.settings_path().is_file()
        kind0 = s.provider or detect_provider(s.base_url, config.get_api_key() or "")
        kind0 = kind0 if kind0 in PROVIDERS else "openai"
        self.labels = {info.label: k for k, info in PROVIDERS.items()}
        self.provider_label = tk.StringVar(value=PROVIDERS[kind0].label)
        self.key = tk.StringVar(value="")
        self.base = tk.StringVar(value=s.base_url if configured or not suggest(provider=kind0)["needs_url"] else "")
        self.model = tk.StringVar(value=s.chat_model if configured else PROVIDERS[kind0].default_model)
        self.tts_model = tk.StringVar(value=s.tts_model if configured else PROVIDERS[kind0].tts_model)
        self.voice = tk.StringVar(value=s.tts_voice)
        self.autonomy = tk.StringVar(value=s.autonomy if s.autonomy in AUTONOMY_LEVELS else "balanced")
        self.note = tk.StringVar(value="")
        self.autonomy_help = tk.StringVar(value=AUTONOMY_HELP[self.autonomy.get()])
        self.status = tk.StringVar(value=("A key is already saved; leave the box empty to keep it." if have_key else
                                          "Your key is stored in Windows Credential Manager, never in a file."))
        self.hotkey_label = hk.label(getattr(s, "hotkey", hk.DEFAULT))

        root.title("Jarvis setup")
        root.resizable(False, False)
        frm = ttk.Frame(root, padding=16)
        frm.grid(sticky="nsew")
        self.frm = frm
        W = 44
        ttk.Label(frm, text="Connect Jarvis to your AI", font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(frm, text="Paste your API key and click Connect. Jarvis works out the rest.",
                  foreground="#555").grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self.key_row = ttk.Frame(frm)
        self.key_row.grid(row=2, column=0, columnspan=3, sticky="we")
        ttk.Label(self.key_row, text="Paste your API key", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        self.key_entry = ttk.Entry(self.key_row, textvariable=self.key, width=W + 12, show="\u2022")
        self.key_entry.grid(row=1, column=0, sticky="we", pady=(2, 8))
        self.first_focus = "key"

        ttk.Label(frm, text="Provider").grid(row=3, column=0, sticky="w", pady=3)
        self.provider_combo = ttk.Combobox(frm, textvariable=self.provider_label, values=list(self.labels),
                                           state="readonly", width=W)
        self.provider_combo.grid(row=3, column=1, columnspan=2, sticky="we", pady=3)
        self.provider_combo.bind("<<ComboboxSelected>>", lambda e: self.select_provider(self.kind()))

        self.url_row = ttk.Frame(frm)
        ttk.Label(self.url_row, text="Provider URL", width=12).grid(row=0, column=0, sticky="w")
        ttk.Entry(self.url_row, textvariable=self.base, width=W + 2).grid(row=0, column=1, sticky="we")
        self.url_row.grid(row=4, column=0, columnspan=3, sticky="we", pady=3)

        ttk.Label(frm, text="Model").grid(row=5, column=0, sticky="w", pady=3)
        self.model_combo = ttk.Combobox(frm, textvariable=self.model, values=[self.model.get()], width=W)
        self.model_combo.grid(row=5, column=1, columnspan=2, sticky="we", pady=3)
        self.model_combo.bind("<<ComboboxSelected>>", lambda e: setattr(self, "_model_auto", False))
        self.model_combo.bind("<Key>", lambda e: setattr(self, "_model_auto", False))
        ttk.Label(frm, textvariable=self.note, wraplength=440, foreground="#555").grid(
            row=6, column=0, columnspan=3, sticky="w")

        ttk.Label(frm, text="Ask before acting:").grid(row=7, column=0, sticky="w", pady=(10, 0))
        radios = ttk.Frame(frm)
        radios.grid(row=7, column=1, columnspan=2, sticky="w", pady=(10, 0))
        for j, level in enumerate(AUTONOMY_LEVELS):
            ttk.Radiobutton(radios, text=level.capitalize(), value=level, variable=self.autonomy,
                            command=lambda: self.autonomy_help.set(AUTONOMY_HELP[self.autonomy.get()])
                            ).grid(row=0, column=j, padx=(0, 10))
        ttk.Label(frm, textvariable=self.autonomy_help, wraplength=440, foreground="#555").grid(
            row=8, column=0, columnspan=3, sticky="w")

        self.adv_btn = ttk.Button(frm, text="Advanced \u25b8", command=self.toggle_advanced)
        self.adv_btn.grid(row=9, column=0, sticky="w", pady=(10, 0))
        self.adv = ttk.Frame(frm)
        self.adv_url_row = ttk.Frame(self.adv)
        ttk.Label(self.adv_url_row, text="Provider URL", width=30).grid(row=0, column=0, sticky="w")
        ttk.Entry(self.adv_url_row, textvariable=self.base, width=30).grid(row=0, column=1, sticky="we")
        self.adv_url_row.grid(row=0, column=0, columnspan=2, sticky="we", pady=2)
        for i, (text, var) in enumerate([("Voice model (blank = Windows voice)", self.tts_model),
                                         ("Voice name", self.voice)], start=1):
            ttk.Label(self.adv, text=text, width=30).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Entry(self.adv, textvariable=var, width=30).grid(row=i, column=1, sticky="we", pady=2)

        ttk.Label(frm, textvariable=self.status, wraplength=440, foreground="#333").grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(12, 6))
        btns = ttk.Frame(frm)
        btns.grid(row=12, column=0, columnspan=3, sticky="e")
        self.save_anyway_btn = ttk.Button(btns, text="Save anyway", command=self.do_save_anyway)
        ttk.Button(btns, text="Cancel", command=self.close).grid(row=0, column=1, padx=(6, 0))
        self.connect_btn = ttk.Button(btns, text="Connect", command=self.do_connect, default="active")
        self.connect_btn.grid(row=0, column=2, padx=(6, 0))
        root.bind("<Return>", lambda e: self.do_connect())
        root.protocol("WM_DELETE_WINDOW", self.close)

        self._apply_visibility()
        self.key.trace_add("write", lambda *a: self.on_key_change(debounce_ms=500))
        self.base.trace_add("write", lambda *a: self.on_url_change())
        self.key_entry.focus_set()
        root.after(50, self._poll)

    # ------------------------------------------------------------ state helpers
    def kind(self) -> str:
        return self.labels[self.provider_label.get()]

    def _shown(self, widget) -> bool:
        return bool(widget.grid_info())

    def key_visible(self):
        return self._shown(self.key_row)

    def url_visible(self):
        return self._shown(self.url_row)

    def advanced_visible(self):
        return self._shown(self.adv)

    def save_anyway_visible(self):
        return self._shown(self.save_anyway_btn)

    def _apply_visibility(self):
        info = suggest(provider=self.kind())
        (self.key_row.grid if info["needs_key"] else self.key_row.grid_remove)()
        if info["needs_url"]:
            self.url_row.grid(); self.adv_url_row.grid_remove()
        else:
            self.url_row.grid_remove(); self.adv_url_row.grid()
        self.note.set(info["notes"] or ("Runs on this PC; no key needed." if not info["needs_key"] else ""))
        self.save_anyway_btn.grid_remove()

    def toggle_advanced(self):
        self._adv_open = not self._adv_open
        if self._adv_open:
            self.adv.grid(row=10, column=0, columnspan=3, sticky="we", pady=(4, 0))
        else:
            self.adv.grid_remove()
        self.adv_btn.configure(text="Advanced \u25be" if self._adv_open else "Advanced \u25b8")

    def _post(self, fn):
        self._queue.put(fn)

    def _drain(self):
        while True:
            try:
                fn = self._queue.get_nowait()
            except Exception:
                return
            try:
                fn()
            except self.tk.TclError:
                return

    def _poll(self):
        self._drain()
        try:
            self.root.after(60, self._poll)
        except self.tk.TclError:
            pass

    def _pump_until(self, event, timeout=10.0):
        import time
        end = time.monotonic() + timeout
        while not event.is_set() and time.monotonic() < end:
            self.root.update(); time.sleep(0.01)
        self.root.update(); self._drain(); self.root.update()

    # ------------------------------------------------------------ provider / key
    def select_provider(self, kind, keep_url=False):
        info = suggest(self.key.get(), provider=kind)
        self.provider_label.set(info["label"])
        if not keep_url:
            self._set_base(info["base_url"])
        self.model.set(info["model"]); self._model_auto = True
        self.model_combo["values"] = [info["model"]] if info["model"] else []
        self.tts_model.set(info["tts_model"])
        self._apply_visibility()
        self.schedule_fetch(0 if not info["needs_key"] else 300)

    def _set_base(self, value):
        self._setting_base = True
        try:
            self.base.set(value)
        finally:
            self._setting_base = False

    def on_key_change(self, debounce_ms=0):
        info = suggest(self.key.get())
        if info["detected"] and info["kind"] != self.kind():
            self.select_provider(info["kind"])
        self.schedule_fetch(debounce_ms)

    def on_url_change(self):
        if getattr(self, "_setting_base", False):
            return
        from .providers import detect_provider
        url = self.base.get().strip()
        if url.startswith(("http://", "https://")):
            k = detect_provider(url, "")
            if k != "litellm" and k != self.kind():
                self.select_provider(k, keep_url=True)
                return
        self.schedule_fetch(600)

    def schedule_fetch(self, delay_ms=0):
        if self._fetch_job is not None:
            try:
                self.root.after_cancel(self._fetch_job)
            except self.tk.TclError:
                pass
        self._fetch_job = self.root.after(delay_ms, self._start_fetch)

    def _start_fetch(self):
        self._fetch_job = None
        self._gen += 1
        gen = self._gen
        kind, base = self.kind(), self.base.get().strip()
        key = self.key.get().strip() or config.get_api_key() or ""
        done = self.threading.Event()
        self._models_done = done

        def work():
            models = fetch_models(kind, base, key)

            def apply():
                if gen == self._gen:
                    self.model_combo["values"] = models
                    if models and (self._model_auto or not self.model.get().strip()):
                        self.model.set(models[0])
            self._post(apply)
            done.set()
        self.threading.Thread(target=work, daemon=True, name="jarvis-setup-models").start()

    def wait_models(self, timeout=10.0):
        if self._fetch_job is not None:
            self.root.after_cancel(self._fetch_job)
            self._start_fetch()
        self._pump_until(self._models_done, timeout)

    # ------------------------------------------------------------ connect / save
    def _values(self):
        return dict(kind=self.kind(), base_url=self.base.get(), api_key=self.key.get(), model=self.model.get(),
                    autonomy=self.autonomy.get(), tts_model=self.tts_model.get().strip(),
                    tts_voice=self.voice.get().strip())

    def do_connect(self):
        if not self._connect_done.is_set():
            return
        self.save_anyway_btn.grid_remove()
        self.status.set("Connecting: one short reply and one tool call...")
        self.connect_btn.state(["disabled"])
        values = self._values()
        done = self.threading.Event()
        self._connect_done = done

        def work():
            try:
                ok, msg = connect(**values)
            except Exception as e:  # keyring or disk errors
                ok, msg = False, _redact(f"Could not save settings ({type(e).__name__}).", values["api_key"])
            self._post(lambda: self._finish_connect(ok, msg))
            done.set()
        self.threading.Thread(target=work, daemon=True, name="jarvis-setup-connect").start()

    def _finish_connect(self, ok, msg):
        self.connect_btn.state(["!disabled"])
        if ok:
            self.saved = True
            self.status.set(f"\u2714 {msg} Saved. Say \"Jarvis\" or press {self.hotkey_label} to talk.")
            self.root.after(1500, self.close)
        else:
            self.status.set("\u2716 " + msg)
            self.save_anyway_btn.grid(row=0, column=0)

    def wait_connect(self, timeout=10.0):
        self._pump_until(self._connect_done, timeout)

    def do_save_anyway(self):
        ok, msg = connect(**self._values(), verify=False)
        if ok:
            self.saved = True
            self.close()
        else:
            self.status.set("\u2716 " + msg)

    def close(self):
        try:
            self.root.quit()
        except self.tk.TclError:
            pass


def run_wizard(_test_hook=None) -> bool:
    """Show the setup window. Returns True when settings were saved.

    ``_test_hook(ui)`` runs after the window is built (tests drive it headlessly).
    """
    import tkinter as tk
    root = tk.Tk()
    ui = _WizardUI(root)
    try:
        if _test_hook is not None:
            _test_hook(ui)
        root.mainloop()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
    return ui.saved
