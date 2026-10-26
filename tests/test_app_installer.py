"""Real Windows installer checks for the Jarvis Desktop app (scripts/install_app.ps1).

The full install runs for real into a scratch folder (uv sync, UFO venv from the local checkout).
Set JARVIS_INSTALL_TEST_UFO to a UFO git checkout (default: the developer checkout below); the
UFO part is skipped when it does not exist. Set JARVIS_INSTALL_TEST_NETWORK=1 to also download
the real Vosk wake model.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows installer")
POWERSHELL = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
SCRATCH = Path(os.environ.get("JARVIS_INSTALL_TEST_DIR",
                              Path(os.environ.get("LOCALAPPDATA", Path.home())) / "hermes/cache/scratch/jarvis-install-test"))
UFO_SRC = Path(os.environ.get("JARVIS_INSTALL_TEST_UFO", Path.home() / "Desktop/ufo-platform/UFO"))


def ps(script, *args, timeout=3600, env=None):
    return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *map(str, args)],
                          text=True, capture_output=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)


def ps_cmd(command, timeout=600):
    return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                          text=True, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)


def common(body):
    return f". '{ROOT / 'installer/common.ps1'}'; $ErrorActionPreference='Stop'; {body}"


# ------------------------------------------------------------------ unit-level (fast)

def test_uv_bootstrap_refuses_mismatched_sha(tmp_path):
    fake = tmp_path / "uv.zip"
    with zipfile.ZipFile(fake, "w") as z:
        z.writestr("uv.exe", b"not really uv")
    (tmp_path / "uv.zip.sha256").write_text("0" * 64 + "  uv-x86_64-pc-windows-msvc.zip\n")
    tools = tmp_path / "tools"
    r = ps_cmd(common(f"Install-UvStandalone -ToolsDir '{tools}' -ZipUrl '{fake.as_uri()}' -ShaUrl '{(tmp_path / 'uv.zip.sha256').as_uri()}'"))
    assert r.returncode != 0
    assert "SHA-256 mismatch" in r.stdout + r.stderr
    assert not (tools / "uv/uv.exe").exists()


def test_uv_bootstrap_accepts_matching_sha(tmp_path):
    fake = tmp_path / "uv.zip"
    with zipfile.ZipFile(fake, "w") as z:
        z.writestr("uv-x86_64-pc-windows-msvc/uv.exe", b"fake uv binary")
        z.writestr("uv-x86_64-pc-windows-msvc/uvx.exe", b"fake uvx")
    sha = hashlib.sha256(fake.read_bytes()).hexdigest()
    (tmp_path / "uv.zip.sha256").write_text(f"{sha} *uv-x86_64-pc-windows-msvc.zip\n")
    tools = tmp_path / "tools"
    r = ps_cmd(common(f"Install-UvStandalone -ToolsDir '{tools}' -ZipUrl '{fake.as_uri()}' -ShaUrl '{(tmp_path / 'uv.zip.sha256').as_uri()}'"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tools / "uv/uv.exe").read_bytes() == b"fake uv binary"
    assert not (tools / "download").exists()


@pytest.mark.parametrize("bad", ["../evil.txt", "/abs.txt", "C:/drive.txt", "ok/../../evil.txt"])
def test_zip_extraction_rejects_unsafe_entries(tmp_path, bad):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("good.txt", "fine")
        z.writestr(bad, "evil")
    out = tmp_path / "out"
    r = ps_cmd(common(f"Expand-ZipSafely -ZipPath '{archive}' -Destination '{out}'"))
    assert r.returncode != 0
    assert "Unsafe path" in r.stdout + r.stderr
    assert not (tmp_path / "evil.txt").exists() and not out.exists()


def test_bundled_ufo_patches_match_manifest():
    manifest = json.loads((ROOT / "patches/ufo-manifest.json").read_text())
    assert len(manifest["commit"]) == 40
    for rel, info in manifest["files"].items():
        data = (ROOT / "patches/ufo-files" / rel).read_bytes()
        assert hashlib.sha256(data).hexdigest() == info["patched_sha256"], rel
    assert b"UFO_AUTO_APPROVE_SENSITIVE" in (ROOT / "patches/ufo-files/ufo/module/interactor.py").read_bytes()
    assert sorted(p.name for p in (ROOT / "patches").glob("ufo-*.patch"))


def test_wrappers_are_double_click_safe():
    install = (ROOT / "Install-Jarvis.cmd").read_text()
    assert 'cd /d "%~dp0"' in install
    assert '-NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\\install_app.ps1" %*' in install
    assert install.count("pause") >= 2
    for name in ("Install-Jarvis.cmd", "Uninstall-Jarvis.cmd", "Start-Jarvis.cmd", "scripts/install_app.ps1",
                 "installer/common.ps1", "installer/uninstall_app.ps1"):
        text = (ROOT / name).read_text().lower()
        for forbidden in ("runas", "invoke-expression", "| iex", "irm ", "iex(", "install.ps1 |", "setx path"):
            assert forbidden not in text, (name, forbidden)


def test_uninstall_refuses_folder_without_marker(tmp_path):
    victim = tmp_path / "not jarvis"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    r = ps(ROOT / "installer/uninstall_app.ps1", "-Destination", victim, "-Yes", "-KeepShortcuts", "-KeepCredential", timeout=120)
    assert r.returncode == 1
    assert (victim / "keep.txt").exists()


def test_installer_uses_its_own_uv_cache_and_bounded_network():
    # A uv already on the user's PATH may share a cache whose lock another uv process holds,
    # which hung a real .exe install for 30+ minutes with no output. The installer must use its
    # own cache and finite network timeouts/retries so it can never wait forever.
    text = (ROOT / "scripts/install_app.ps1").read_text()
    assert "$env:UV_CACHE_DIR" in text and "Join-Path $dest" in text.split("$env:UV_CACHE_DIR", 1)[1].splitlines()[0]
    assert "$env:UV_HTTP_TIMEOUT" in text


def test_release_zip_has_installer_and_no_secrets(tmp_path):
    out = tmp_path / "Jarvis-Setup.zip"
    r = ps(ROOT / "installer/build_release.ps1", "-Output", out, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    rels = [n.split("/", 1)[1] for n in names]
    for required in ("Install-Jarvis.cmd", "Uninstall-Jarvis.cmd", "Start-Jarvis.cmd", "README-FIRST.txt",
                     "scripts/install_app.ps1", "installer/common.ps1", "installer/Start-Jarvis.app.cmd",
                     "patches/ufo-manifest.json", "patches/ufo-files/ufo/module/interactor.py",
                     "pyproject.toml", "uv.lock", "jarvis/__init__.py", "companion/ufo.py"):
        assert required in rels, required
    for rel in rels:
        parts = rel.split("/")
        assert parts[-1] not in (".env", "ACCESS.txt", "settings.json", "agents.yaml"), rel
        assert not set(parts) & {"data", "workspace", "tests", ".venv", "__pycache__", ".git", "logs"}, rel


# ------------------------------------------------------------------ real install (slow)

@pytest.fixture(scope="module")
def installed():
    dest = SCRATCH / "Jarvis install"
    if dest.exists():
        def make_writable(func, path, _exc):  # git object files are read-only on Windows
            os.chmod(path, 0o700)
            func(path)
        for attempt in range(6):  # antivirus may briefly hold freshly written .pyd files
            try:
                shutil.rmtree(dest, onexc=make_writable)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                import time
                time.sleep(10)
    args = ["-Destination", dest, "-SkipModels", "-NoShortcuts", "-NoSetup"]
    if (UFO_SRC / ".git").is_dir():
        args += ["-UfoSource", UFO_SRC]
    else:
        args += ["-NoUfo"]
    r = ps(ROOT / "scripts/install_app.ps1", *args)
    (SCRATCH / "first-install.log").write_text(r.stdout + "\n--- stderr ---\n" + r.stderr, encoding="utf-8")
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-4000:]
    return dest, args, r


def test_real_install_app(installed):
    dest, _, _ = installed
    app = dest / "app"
    py = app / ".venv/Scripts/python.exe"
    assert py.is_file()
    for rel in ("jarvis/__init__.py", "companion/ufo.py", "jarvis_tools.py", "pyproject.toml", "uv.lock"):
        assert (app / rel).is_file(), rel
    assert not (app / ".env").exists() and not (app / "ACCESS.txt").exists() and not (app / "tests").exists()
    r = subprocess.run([str(py), "-c", "import jarvis, companion, vosk, sounddevice, openpyxl; print('ok')"],
                       cwd=app, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": ""})
    assert r.stdout.strip() == "ok", r.stderr
    if (ROOT / "jarvis_desktop/__init__.py").is_file():
        assert (app / "jarvis_desktop/__init__.py").is_file()


def test_real_install_ufo(installed):
    dest, args, _ = installed
    if "-NoUfo" in args:
        pytest.skip("no local UFO checkout")
    ufo = dest / "ufo"
    assert "UFO_AUTO_APPROVE_SENSITIVE" in (ufo / "ufo/module/interactor.py").read_text(encoding="utf-8")
    assert "action.action_string or action.function" in (ufo / "ufo/automator/action_execution.py").read_text(encoding="utf-8")
    agents = (ufo / "config/ufo/agents.yaml").read_text(encoding="utf-8")
    active = [line.strip() for line in agents.splitlines() if line.strip().startswith("API_")]
    assert active.count('API_BASE: "http://127.0.0.1:8780/v1"  # set by Jarvis installer') == 4
    assert active.count('API_KEY: "local-proxy"  # set by Jarvis installer') == 4
    assert active.count('API_TYPE: "openai"  # set by Jarvis installer') == 4
    assert sum(1 for a in active if a.startswith("API_MODEL:") and "set by Jarvis" in a) == 4
    assert "sk-" not in agents
    py = ufo / ".venv/Scripts/python.exe"
    r = subprocess.run([str(py), "-E", "-X", "utf8", "-c", "import ufo; print('ufo-ok')"], cwd=ufo, capture_output=True, text=True)
    assert "ufo-ok" in r.stdout, r.stderr[-2000:]
    head = subprocess.run(["git", "-C", str(ufo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert head == json.loads((ROOT / "patches/ufo-manifest.json").read_text())["commit"]


def test_real_install_launchers(installed):
    dest, _, _ = installed
    start = (dest / "Start-Jarvis.cmd").read_text()
    assert "-m jarvis_desktop" in start
    assert 'set "PYTHONPATH="' in start and "JARVIS_UFO_HOME" in start and "JARVIS_WAKE_MODEL_PATH" in start
    assert (dest / "Uninstall-Jarvis.cmd").is_file()
    assert (dest / "installer/uninstall_app.ps1").is_file()
    assert json.loads((dest / ".jarvis-install.json").read_text(encoding="utf-8-sig"))["product"] == "Jarvis Desktop"


def test_real_rerun_is_idempotent_and_keeps_settings(installed):
    dest, args, _ = installed
    settings = dest / "settings.json"
    payload = json.dumps({"base_url": "https://example.invalid/v1", "chat_model": "dummy-model", "autonomy": "careful"})
    settings.write_text(payload)
    data = dest / "data"
    data.mkdir(exist_ok=True)
    (data / "memory.txt").write_text("keep me")
    agents_before = (dest / "ufo/config/ufo/agents.yaml").read_bytes() if "-NoUfo" not in args else None
    r = ps(ROOT / "scripts/install_app.ps1", *[a for a in args if a != "-NoSetup"])
    (SCRATCH / "rerun-install.log").write_text(r.stdout + "\n--- stderr ---\n" + r.stderr, encoding="utf-8")
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-4000:]
    assert "upgrade" in r.stdout and "Existing settings kept" in r.stdout
    assert settings.read_text() == payload
    assert (data / "memory.txt").read_text() == "keep me"
    if agents_before is not None:
        assert "Patch already applied" in r.stdout
        assert (dest / "ufo/config/ufo/agents.yaml").read_bytes() == agents_before


def test_real_uninstall_removes_everything(installed, tmp_path):
    dest, _, _ = installed
    copy_target = tmp_path / "uninstall-check"
    # Uninstall a lightweight marker-only copy, so the main scratch install stays for inspection.
    copy_target.mkdir()
    shutil.copy(dest / ".jarvis-install.json", copy_target / ".jarvis-install.json")
    (copy_target / "settings.json").write_text("{}")
    r = ps(ROOT / "installer/uninstall_app.ps1", "-Destination", copy_target, "-Yes", "-KeepShortcuts", "-KeepCredential", timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not copy_target.exists()


@pytest.mark.skipif(os.environ.get("JARVIS_INSTALL_TEST_NETWORK") != "1", reason="set JARVIS_INSTALL_TEST_NETWORK=1")
def test_real_wake_model_download(installed):
    dest, args, _ = installed
    keep = [a for a in args if a not in ("-SkipModels",)]
    r = ps(ROOT / "scripts/install_app.ps1", *keep, "-NoUfo")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert (dest / "models/vosk-model-small-en-us-0.15/am/final.mdl").is_file()
    assert not (dest / "models/download").exists()


def test_real_double_click_install_then_uninstall(tmp_path):
    """Install-Jarvis.cmd from an unrelated cwd (no UFO), then the installed Uninstall-Jarvis.cmd: N keeps, Y removes."""
    dest = SCRATCH / "wrapper install"
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    run = lambda *a, inp: subprocess.run(["cmd.exe", "/d", "/c", "call", *map(str, a)], cwd=tmp_path, input=inp,
                                         capture_output=True, text=True, timeout=1800)
    r = run(ROOT / "Install-Jarvis.cmd", "-Destination", dest, "-NoUfo", "-SkipModels", "-NoShortcuts", "-NoSetup", inp="\n")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert "Press any key" in r.stdout and (dest / "app/.venv/Scripts/python.exe").is_file()
    r = run(dest / "Uninstall-Jarvis.cmd", "-KeepCredential", "-KeepShortcuts", inp="N\n\n")
    assert r.returncode == 2 and "Cancelled" in r.stdout and dest.exists()
    import glob
    temp_before = set(glob.glob(os.path.join(os.environ["TEMP"], "jarvis-uninstall-*")))
    r = run(dest / "Uninstall-Jarvis.cmd", "-KeepCredential", "-KeepShortcuts", inp="Y\n\n")
    assert r.returncode == 0, r.stdout[-2000:]
    assert "Jarvis has been removed" in r.stdout
    assert not dest.exists()
    assert set(glob.glob(os.path.join(os.environ["TEMP"], "jarvis-uninstall-*"))) <= temp_before
