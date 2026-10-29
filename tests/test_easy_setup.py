"""One-step setup: paste a key -> provider/URL/model detected -> Connect (verify + save). No network."""
import json
import sys
import types

import httpx
import pytest


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DESKTOP_HOME", str(tmp_path / "Jarvis"))
    monkeypatch.delenv("JARVIS_LLM_API_KEY", raising=False)
    from jarvis_desktop import config
    kr = FakeKeyring()
    monkeypatch.setattr(config, "_keyring", lambda: kr)
    return tmp_path / "Jarvis", kr


# ---------------------------------------------------------------- suggest()
@pytest.mark.parametrize("key,kind,base,model,tts", [
    ("sk-proj-abc123", "openai", "https://api.openai.com/v1", "gpt-4o-mini", "tts-1"),
    ("sk-ant-api03-xyz", "anthropic", "https://api.anthropic.com", "claude-sonnet-4-5", ""),
    ("sk-or-v1-abc", "openrouter", "https://openrouter.ai/api/v1", "openai/gpt-4o-mini", ""),
    ("gsk_abc", "groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", ""),
    ("AIzaSyAbc", "gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash", ""),
    ("xai-abc", "xai", "https://api.x.ai/v1", "grok-3-mini", ""),
])
def test_suggest_from_key_prefix_fills_everything(key, kind, base, model, tts):
    from jarvis_desktop.setup_wizard import suggest
    s = suggest(key)
    assert (s["kind"], s["base_url"], s["model"], s["tts_model"]) == (kind, base, model, tts)
    assert s["needs_key"] is True and s["needs_url"] is False
    assert s["detected"] is True


def test_suggest_strips_whitespace_from_pasted_key():
    from jarvis_desktop.setup_wizard import suggest
    assert suggest("  sk-ant-abc\n")["kind"] == "anthropic"


def test_suggest_unknown_key_defaults_to_openai_but_not_confident():
    from jarvis_desktop.setup_wizard import suggest
    s = suggest("0123456789abcdef0123456789abcdef")
    assert s["kind"] == "openai" and s["detected"] is False
    assert suggest("")["detected"] is False


def test_suggest_url_wins_over_key_and_is_kept():
    from jarvis_desktop.setup_wizard import suggest
    s = suggest("abc", "https://myres.openai.azure.com/openai/v1/")
    assert s["kind"] == "azure" and s["base_url"] == "https://myres.openai.azure.com/openai/v1"
    assert s["needs_url"] is True and s["model"] == ""  # deployment name must be typed


@pytest.mark.parametrize("kind", ["azure", "litellm"])
def test_suggest_explicit_url_providers_need_url_and_no_placeholder(kind):
    from jarvis_desktop.setup_wizard import suggest
    s = suggest("k", provider=kind)
    assert s["kind"] == kind and s["needs_url"] is True
    assert s["base_url"] == ""  # never the YOUR-RESOURCE placeholder


def test_suggest_ollama_needs_no_key_and_no_url():
    from jarvis_desktop.setup_wizard import suggest
    s = suggest("", provider="ollama")
    assert s["needs_key"] is False and s["needs_url"] is False
    assert s["base_url"] == "http://localhost:11434/v1" and s["model"] == "llama3.1"


def test_suggest_explicit_provider_wins_over_key_and_unknown_raises():
    from jarvis_desktop.setup_wizard import suggest
    assert suggest("sk-ant-x", provider="OpenRouter")["kind"] == "openrouter"
    with pytest.raises(ValueError):
        suggest("x", provider="nope")


# ---------------------------------------------------------------- model list
def _models(payload, status=200, seen=None):
    def handler(request):
        if seen is not None:
            seen.append((request.url.path, dict(request.headers)))
        return httpx.Response(status, json=payload)
    return httpx.MockTransport(handler)


def test_provider_list_models_reads_openai_shape_and_strips_gemini_prefix():
    from jarvis_desktop.providers import Provider
    seen = []
    p = Provider("gemini", "", "AIzaK", transport=_models({"data": [{"id": "models/gemini-2.5-flash"}, {"id": "x"}]}, seen=seen))
    assert p.list_models() == ["gemini-2.5-flash", "x"]
    assert seen[0][0] == "/v1beta/openai/models"


def test_provider_list_models_anthropic_uses_its_headers():
    from jarvis_desktop.providers import Provider
    seen = []
    p = Provider("anthropic", "", "sk-ant-k", transport=_models({"data": [{"id": "claude-sonnet-4-5"}]}, seen=seen))
    assert p.list_models() == ["claude-sonnet-4-5"]
    path, headers = seen[0]
    assert path == "/v1/models" and headers["x-api-key"] == "sk-ant-k"


def test_provider_list_models_errors_raise_provider_error():
    from jarvis_desktop.providers import Provider, ProviderError
    with pytest.raises(ProviderError):
        Provider("openai", "", "k", transport=_models({"error": "no"}, status=401)).list_models()


def test_fetch_models_filters_non_chat_and_puts_default_first():
    from jarvis_desktop.setup_wizard import fetch_models
    data = {"data": [{"id": i} for i in ["whisper-1", "gpt-4o", "text-embedding-3-small", "tts-1", "dall-e-3",
                                          "gpt-4o-mini", "omni-moderation-latest", "gpt-4o"]]}
    out = fetch_models("openai", "", "sk-k", transport=_models(data))
    assert out == ["gpt-4o-mini", "gpt-4o"]


def test_fetch_models_falls_back_to_default_and_never_raises():
    from jarvis_desktop.setup_wizard import fetch_models
    assert fetch_models("openai", "", "sk-k", transport=_models({}, status=500)) == ["gpt-4o-mini"]
    boom = httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))
    assert fetch_models("groq", "", "gsk_k", transport=boom) == ["llama-3.3-70b-versatile"]
    assert fetch_models("azure", "", "k", transport=boom) == []  # no default deployment to offer


# ---------------------------------------------------------------- connect()
def test_connect_verifies_then_saves_in_one_step(home):
    from jarvis_desktop import config
    from jarvis_desktop.setup_wizard import connect
    root, kr = home
    calls = []

    def fake_verify(kind, base, key, model):
        calls.append((kind, base, key, model))
        return True, "Connected to OpenAI.", {}

    ok, msg = connect("openai", "https://api.openai.com/v1", "sk-new", "gpt-4o", autonomy="careful",
                      tts_model="tts-1", tts_voice="nova", verify_fn=fake_verify)
    assert ok and "Connected" in msg
    assert calls == [("openai", "https://api.openai.com/v1", "sk-new", "gpt-4o")]
    s = config.load_settings()
    assert (s.provider, s.chat_model, s.autonomy, s.tts_voice) == ("openai", "gpt-4o", "careful", "nova")
    assert kr.store[("Jarvis", "provider_api_key")] == "sk-new"
    assert "sk-new" not in (root / "settings.json").read_text()


def test_connect_failure_saves_nothing_and_never_echoes_key(home):
    from jarvis_desktop import config
    from jarvis_desktop.setup_wizard import connect
    root, kr = home
    ok, msg = connect("openai", "https://api.openai.com/v1", "sk-secret", "gpt-4o",
                      verify_fn=lambda *a: (False, "bad sk-secret", {}))
    assert not ok and "sk-secret" not in msg
    assert not (root / "settings.json").exists() and not kr.store


def test_connect_uses_saved_key_when_field_blank(home):
    from jarvis_desktop.setup_wizard import connect
    root, kr = home
    kr.store[("Jarvis", "provider_api_key")] = "sk-old"
    seen = []
    ok, _ = connect("openai", "https://api.openai.com/v1", "", "m",
                    verify_fn=lambda k, b, key, m: (seen.append(key), (True, "ok", {}))[1])
    assert ok and seen == ["sk-old"] and kr.store[("Jarvis", "provider_api_key")] == "sk-old"


def test_connect_validates_missing_url_or_model_without_calling_provider(home):
    from jarvis_desktop.setup_wizard import connect
    never = lambda *a: pytest.fail("verify must not run")
    ok, msg = connect("azure", "", "k", "dep", verify_fn=never)
    assert not ok and "URL" in msg
    ok, msg = connect("openai", "https://api.openai.com/v1", "k", "", verify_fn=never)
    assert not ok and "model" in msg.lower()
    ok, msg = connect("openai", "https://api.openai.com/v1", "", "m", verify_fn=never)
    assert not ok and "key" in msg.lower()


def test_save_anyway_saves_without_verifying(home):
    from jarvis_desktop import config
    from jarvis_desktop.setup_wizard import connect
    ok, _ = connect("litellm", "https://gw.example/v1", "k", "m", verify=False,
                    verify_fn=lambda *a: pytest.fail("no verify"))
    assert ok and config.load_settings().base_url == "https://gw.example/v1"


# ---------------------------------------------------------------- settings
def test_settings_defaults_hotkey_and_autostart(home):
    from jarvis_desktop.config import Settings, load_settings
    s = Settings()
    assert s.hotkey == "ctrl+alt+j" and s.start_with_windows is True
    assert load_settings().hotkey == "ctrl+alt+j"


def test_old_settings_file_loads_and_bad_hotkey_falls_back(home):
    from jarvis_desktop import config
    root, _ = home
    root.mkdir(parents=True)
    (root / "settings.json").write_text(json.dumps({"chat_model": "m", "start_with_windows": 0, "hotkey": "ctrl+nope"}))
    s = config.load_settings()
    assert s.chat_model == "m" and s.start_with_windows is False and s.hotkey == "ctrl+alt+j"
    (root / "settings.json").write_text(json.dumps({"hotkey": " Win+Shift+Space "}))
    assert config.load_settings().hotkey == "win+shift+space"


# ---------------------------------------------------------------- GUI (headless build)
@pytest.fixture
def tk_headless(monkeypatch):
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk(); r.destroy()
    except tk.TclError:
        pytest.skip("no Tk display")
    monkeypatch.setattr(tk.Tk, "mainloop", lambda self: (self.update(), self.destroy()))
    return tk


def test_wizard_builds_headless_and_returns_false(home, tk_headless):
    from jarvis_desktop.setup_wizard import run_wizard
    assert run_wizard() is False


def test_wizard_paste_key_autofills_and_ollama_hides_key(home, tk_headless, monkeypatch):
    from jarvis_desktop import setup_wizard
    monkeypatch.setattr(setup_wizard, "fetch_models", lambda *a, **k: ["claude-sonnet-4-5", "claude-opus-4-1"])
    seen = {}

    def hook(ui):
        assert ui.first_focus == "key"
        ui.key.set("sk-ant-api03-abc")
        ui.on_key_change()
        seen["kind"], seen["base"], seen["model"] = ui.kind(), ui.base.get(), ui.model.get()
        seen["url_shown"] = ui.url_visible()
        ui.wait_models()
        seen["models"] = list(ui.model_combo["values"])
        ui.select_provider("ollama")
        seen["key_shown"] = ui.key_visible()
        seen["ollama_base"] = ui.base.get()
        ui.select_provider("azure")
        seen["azure_url_shown"], seen["azure_base"] = ui.url_visible(), ui.base.get()
        seen["advanced_hidden"] = not ui.advanced_visible()
        seen["autonomy"] = ui.autonomy.get()
        seen["save_anyway_hidden"] = not ui.save_anyway_visible()

    assert setup_wizard.run_wizard(_test_hook=hook) is False
    assert (seen["kind"], seen["base"], seen["model"]) == ("anthropic", "https://api.anthropic.com", "claude-sonnet-4-5")
    assert seen["url_shown"] is False and "claude-opus-4-1" in seen["models"]
    assert seen["key_shown"] is False and seen["ollama_base"] == "http://localhost:11434/v1"
    assert seen["azure_url_shown"] is True and seen["azure_base"] == ""
    assert seen["advanced_hidden"] and seen["autonomy"] == "balanced" and seen["save_anyway_hidden"]


def test_wizard_connect_one_click_saves_and_closes(home, tk_headless, monkeypatch):
    from jarvis_desktop import config, setup_wizard
    monkeypatch.setattr(setup_wizard, "fetch_models", lambda *a, **k: ["gpt-4o-mini"])
    monkeypatch.setattr(setup_wizard, "verify", lambda kind, base, key, model: (True, "Connected.", {}))
    state = {}

    def hook(ui):
        ui.key.set("sk-proj-abc")
        ui.on_key_change()
        ui.do_connect()
        ui.wait_connect()
        state["status"] = ui.status.get()

    assert setup_wizard.run_wizard(_test_hook=hook) is True
    assert config.load_settings().provider == "openai" and "Connected" in state["status"]


def test_wizard_connect_failure_offers_save_anyway(home, tk_headless, monkeypatch):
    from jarvis_desktop import setup_wizard
    monkeypatch.setattr(setup_wizard, "fetch_models", lambda *a, **k: ["gpt-4o-mini"])
    monkeypatch.setattr(setup_wizard, "verify", lambda kind, base, key, model: (False, "rejected", {}))
    state = {}

    def hook(ui):
        ui.key.set("sk-proj-abc")
        ui.on_key_change()
        ui.do_connect()
        ui.wait_connect()
        state["shown"] = ui.save_anyway_visible()
        state["status"] = ui.status.get()

    assert setup_wizard.run_wizard(_test_hook=hook) is False
    assert state["shown"] and "rejected" in state["status"]


# ---------------------------------------------------------------- tray
class _FakePystray(types.ModuleType):
    def __init__(self):
        super().__init__("pystray")
        fake = self

        class MenuItem:
            def __init__(self, text, action, enabled=True, default=False, **kw):
                self.text, self.action, self.enabled, self.default = text, action, enabled, default

        class Menu:
            def __init__(self, *items):
                self.items = items
                fake.menu = self

        class Icon:
            def __init__(self, name, image, title, menu):
                self.menu = menu

            def run(self):
                pass

            def stop(self):
                pass

            def update_menu(self):
                pass

        self.MenuItem, self.Menu, self.Icon = MenuItem, Menu, Icon


def test_tray_first_item_is_default_talk_now(monkeypatch):
    fake = _FakePystray()
    monkeypatch.setitem(sys.modules, "pystray", fake)
    from jarvis_desktop.tray import start_tray

    class App:
        status_text, paused, on_state = "Waiting", False, None
        settings = types.SimpleNamespace(hotkey="ctrl+alt+j")
        woke = 0

        def wake_now(self):
            App.woke += 1

    icon = start_tray(App())
    assert icon is not None
    first = fake.menu.items[0]
    assert first.text == "Talk now (Ctrl+Alt+J)" and first.default is True
    first.action(icon, first)
    assert App.woke == 1
    texts = [i.text if isinstance(i.text, str) else i.text(i) for i in fake.menu.items]
    assert "Settings" in texts and "Quit" in texts and any("listening" in t for t in texts)


def test_tray_without_wake_now_keeps_old_menu(monkeypatch):
    fake = _FakePystray()
    monkeypatch.setitem(sys.modules, "pystray", fake)
    from jarvis_desktop.tray import start_tray

    class App:
        status_text, paused, on_state = "Waiting", False, None

    assert start_tray(App()) is not None
    assert not any(isinstance(i.text, str) and i.text.startswith("Talk now") for i in fake.menu.items)


# ---------------------------------------------------------------- installer text
def test_installer_defaults_autostart_and_start_now_checked():
    from pathlib import Path
    iss = (Path(__file__).resolve().parents[1] / "installer/jarvis.iss").read_text(encoding="utf-8")
    task = next(l for l in iss.splitlines() if l.startswith('Name: "autostart"'))
    assert "unchecked" not in task and "sign in" in task
    run = next(l for l in iss.splitlines() if "Start Jarvis now" in l)
    assert "unchecked" not in run and "skipifsilent" in run and "postinstall" in run


def test_install_script_creates_desktop_shortcut_and_uninstall_removes_it():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    install = (root / "scripts/install_app.ps1").read_text(encoding="utf-8")
    assert "GetFolderPath('Desktop')" in install and "GetFolderPath('Programs')" in install
    uninstall = (root / "installer/uninstall_app.ps1").read_text(encoding="utf-8")
    assert "GetFolderPath('Desktop')" in uninstall
