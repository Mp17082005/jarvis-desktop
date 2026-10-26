"""Windows companion launch/install tests; no physical audio is simulated as proof."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def launcher():
    path = ROOT / "companion" / "launcher.py"
    assert path.is_file(), "The companion launcher must exist"
    spec = importlib.util.spec_from_file_location("companion_launcher_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launch_plan_is_loopback_only_and_clears_python_injection(tmp_path):
    module = launcher()
    plan = module.build_launch_plan(ROOT, tmp_path, 8767, {
        "PATH": "safe", "PYTHONPATH": "foreign-packages", "PYTHONHOME": "foreign-python",
        "JARVIS_DESKTOP_ENABLED": "false", "JARVIS_DESKTOP_LOCAL_ONLY": "false",
    })
    assert plan["command"] == [sys.executable, "-m", "uvicorn", "jarvis.main:app", "--host", "127.0.0.1", "--port", "8767", "--no-proxy-headers"]
    assert plan["url"] == "http://127.0.0.1:8767"
    assert "PYTHONPATH" not in plan["env"] and "PYTHONHOME" not in plan["env"]
    assert plan["env"]["JARVIS_DESKTOP_ENABLED"] == "true"
    assert plan["env"]["JARVIS_DESKTOP_LOCAL_ONLY"] == "true"
    assert plan["env"]["JARVIS_DATA_DIR"] == str(tmp_path / "data")
    assert plan["env"]["PATH"] == "safe"
    with pytest.raises(ValueError):
        module.build_launch_plan(ROOT, tmp_path, 0, {})


def test_wake_without_model_never_claims_available_or_records(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_WAKE_MODEL_PATH", raising=False)
    wake = launcher().WakeController(model_path=tmp_path / "absent")
    assert wake.status()["available"] is False
    assert wake.status()["listening"] is False
    assert wake.status()["reason"] == "model_missing"
    assert wake.start() is False
    assert wake.pop_event() is False
    wake.stop()


def test_wake_reports_no_microphone_with_valid_model(tmp_path, monkeypatch):
    from types import SimpleNamespace
    (tmp_path / "am").mkdir()
    (tmp_path / "am" / "final.mdl").touch()
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "model.conf").touch()
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(query_devices=lambda **kw: {"max_input_channels": 0}))
    monkeypatch.setitem(sys.modules, "vosk", SimpleNamespace())
    wake = launcher().WakeController(tmp_path)
    assert wake.status()["reason"] == "microphone_unavailable"
    assert wake.start() is False
    assert wake.pop_event() is False


def test_wake_explicit_start_local_pcm_exact_jarvis_and_stop(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import time
    (tmp_path / "am").mkdir()
    (tmp_path / "am" / "final.mdl").touch()
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "model.conf").touch()
    streams = []
    class Stream:
        def __init__(self, **kw):
            self.kw = kw
            self.closed = False
            streams.append(self)
        def start(self):
            pass
        def stop(self):
            pass
        def close(self):
            self.closed = True
    class Recognizer:
        def __init__(self, model, rate, grammar):
            assert rate == 16000
            assert json.loads(grammar) == ["jarvis", "[unk]"]
        def AcceptWaveform(self, pcm):
            self.pcm = pcm
            return True
        def Result(self):
            return json.dumps({"text": self.pcm.decode()})
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(query_devices=lambda **kw: {"max_input_channels": 1}, RawInputStream=Stream))
    monkeypatch.setitem(sys.modules, "vosk", SimpleNamespace(Model=lambda path: object(), KaldiRecognizer=Recognizer))
    wake = launcher().WakeController(tmp_path)
    assert wake.status()["available"] is True
    assert not streams, "Readiness check must not record"
    assert wake.start() is True
    assert wake.start() is True and len(streams) == 1
    assert streams[0].kw["dtype"] == "int16"
    callback = streams[0].kw["callback"]
    callback(b"not jarvis", 0, None, False)
    time.sleep(.1)
    assert wake.pop_event() is False
    callback(b"jarvis", 0, None, False)
    deadline = time.monotonic() + 2
    woke = False
    while time.monotonic() < deadline and not woke:
        woke = wake.pop_event()
        time.sleep(.01)
    assert woke is True
    assert wake.pop_event() is False, "Wake events are single-consumption"
    wake.stop()
    assert streams[0].closed
    assert wake.status()["listening"] is False
    assert wake.pop_event() is False


def test_browser_command_is_isolated_without_permission_bypasses(tmp_path):
    module = launcher()
    chrome = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.touch()
    assert module.find_browser({"PROGRAMFILES": str(tmp_path)}) == chrome
    command = module.browser_command(chrome, tmp_path / "profile", "http://127.0.0.1:8767")
    assert command == [str(chrome), "--user-data-dir=" + str(tmp_path / "profile"), "--app=http://127.0.0.1:8767", "--no-first-run", "--no-default-browser-check"]
    assert module.find_browser({}) is None


def test_doctor_cli_returns_machine_readable_checks_without_secrets(tmp_path):
    import os
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["JARVIS_LLM_API_KEY"] = "must-not-appear"
    result = subprocess.run([sys.executable, str(ROOT / "companion" / "launcher.py"), "--doctor", "--state-dir", str(tmp_path)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["bind"] == "127.0.0.1"
    assert "microphone" in report and "wake" in report and "browser" in report
    assert "must-not-appear" not in result.stdout
    assert report["wake"]["listening"] is False


def test_source_package_excludes_credentials_runtime_and_tests(tmp_path):
    import zipfile
    source = tmp_path / "source"
    source.mkdir()
    for name in ["pyproject.toml", "uv.lock", "jarvis_tools.py", ".env.example", ".env", "ACCESS.txt", "data/private.py", "tests/private.py", "jarvis/main.py", "jarvis/__pycache__/private.py", "companion/launcher.py", "static/index.html", "static/private.env", "scripts/start_companion.cmd", "scripts/install_companion.ps1", "docs/DESKTOP-COMPANION.md"]:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    archive = tmp_path / "companion.zip"
    launcher().package_source(source, archive)
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        assert "jarvis-companion/companion/launcher.py" in names
        assert "jarvis-companion/uv.lock" in names
        assert "jarvis-companion/.env.example" in names
        assert not any("private" in p or p.endswith("/.env") or p.endswith("ACCESS.txt") for p in names)


def test_launcher_runs_real_server_and_cleans_up(tmp_path):
    import os
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, str(ROOT / "companion" / "launcher.py"), "--no-browser", "--port", str(port), "--state-dir", str(tmp_path), "--run-seconds", "1"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Ready: http://127.0.0.1:{port}" in result.stdout
    with socket.socket() as sock:
        assert sock.connect_ex(("127.0.0.1", port)) != 0


def test_installer_source_only_no_secret_copy_and_shortcuts_opt_in(tmp_path):
    import os
    if sys.platform != "win32":
        pytest.skip("PowerShell installer integration requires Windows")
    script = ROOT / "scripts" / "install_companion.ps1"
    assert script.is_file(), "Installer must exist"
    destination = tmp_path / "installed"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Destination", str(destination), "-SourceOnly"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (destination / "companion" / "launcher.py").is_file()
    assert (destination / "scripts" / "start_companion.cmd").is_file()
    assert not (destination / ".env").exists()
    assert not (destination / "ACCESS.txt").exists()
    assert not (destination / "data").exists()
    assert not (destination / "tests").exists()
    assert "No shortcuts created" in result.stdout


def test_launcher_refuses_occupied_port_without_killing_owner(tmp_path):
    import os
    import socket
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        result = subprocess.run([sys.executable, "-m", "companion.launcher", "--no-browser", "--port", str(sock.getsockname()[1]), "--state-dir", str(tmp_path)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 1
        assert "occupied" in result.stderr
        assert sock.fileno() >= 0


def test_doctor_loads_model_configuration_without_printing_credentials(tmp_path, monkeypatch):
    module = launcher()
    (tmp_path / ".env").write_text('JARVIS_WAKE_MODEL_PATH=some-configured-model\nJARVIS_LLM_API_KEY=private-token\n')
    monkeypatch.delenv("JARVIS_WAKE_MODEL_PATH", raising=False)
    observed = []
    class Wake:
        def __init__(self, model_path=None):
            observed.append(model_path)
        def status(self):
            return {"available": False, "listening": False, "reason": "model_missing"}
    monkeypatch.setattr(module, "WakeController", Wake)
    report = module.doctor(tmp_path)
    assert observed == ["some-configured-model"]
    assert "private-token" not in json.dumps(report)



