"""Real Windows entrypoint checks; all installation targets are temporary."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows entrypoints")


def run_cmd(wrapper, cwd, *args, env=None):
    return subprocess.run(
        ["cmd.exe", "/d", "/c", "call", str(wrapper), *map(str, args)],
        cwd=cwd, env=env, input="\n", text=True, capture_output=True, timeout=120,
    )


def run_ps(script, cwd, *args, env=None):
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *map(str, args)],
        cwd=cwd, env=env, input="\n", text=True, capture_output=True, timeout=120,
    )


def test_legacy_companion_installer_works_from_unrelated_directory(tmp_path):
    cwd = tmp_path / "unrelated working directory"
    cwd.mkdir()
    target = tmp_path / "installed companion with spaces"
    target.mkdir()
    original = b"LOCAL_CONFIGURATION_SENTINEL=keep-private\r\n"
    (target / ".env").write_bytes(original)
    result = run_ps(ROOT / "scripts/install_companion.ps1", cwd, "-SourceOnly", "-Destination", target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SourceOnly: dependencies not installed" in result.stdout
    assert (target / "scripts/install_companion.ps1").is_file()
    assert (target / ".env").read_bytes() == original
    assert (target / ".env.example").read_bytes() == (ROOT / ".env.example").read_bytes()
    assert "LOCAL_CONFIGURATION_SENTINEL" not in result.stdout + result.stderr
    assert not (cwd / "JarvisCompanion").exists()


def test_start_prefers_installed_directory_and_reports_missing_config(tmp_path):
    wrapper = ROOT / "Start-Jarvis.cmd"
    assert wrapper.is_file(), "Extracted source needs a double-click launcher"
    installed = tmp_path / "local app data" / "JarvisCompanion"
    (installed / "scripts").mkdir(parents=True)
    # This script must not execute before the configuration check.
    (installed / "scripts/start_companion.cmd").write_text("@echo WRONG_START\nexit /b 77\n")
    env = dict(os.environ, LOCALAPPDATA=str(installed.parent))
    result = run_cmd(wrapper, tmp_path, env=env)
    assert result.returncode == 1
    assert str(installed / ".env") in result.stdout
    assert ".env.example" in result.stdout
    assert "WRONG_START" not in result.stdout
    assert "Press any key" in result.stdout


def test_start_forwards_arguments_and_exit_code_to_installed_launcher(tmp_path):
    wrapper = ROOT / "Start-Jarvis.cmd"
    assert wrapper.is_file()
    installed = tmp_path / "local app data" / "JarvisCompanion"
    (installed / "scripts").mkdir(parents=True)
    (installed / ".venv/Scripts").mkdir(parents=True)
    (installed / ".venv/Scripts/python.exe").touch()
    (installed / ".env").write_text("PRIVATE_SENTINEL=never_print_me\n")
    (installed / "scripts/start_companion.cmd").write_text(
        '@echo off\necho SELECTED_INSTALLED\necho cwd=%CD%\necho args=%*\nexit /b 23\n'
    )
    result = run_cmd(wrapper, tmp_path, "--doctor", env=dict(os.environ, LOCALAPPDATA=str(installed.parent)))
    assert result.returncode == 23, result.stdout + result.stderr
    assert "SELECTED_INSTALLED" in result.stdout
    assert f"cwd={installed}" in result.stdout
    assert "args=--doctor" in result.stdout
    assert "PRIVATE_SENTINEL" not in result.stdout


def test_packaged_source_has_double_click_entrypoints_and_plain_readme(tmp_path):
    import zipfile
    from companion.launcher import package_source
    archive = package_source(ROOT, tmp_path / "source.zip")
    with zipfile.ZipFile(archive) as zipped:
        names = set(zipped.namelist())
        for name in ("Install-Jarvis.cmd", "Start-Jarvis.cmd", "README-FIRST.txt"):
            assert f"jarvis-companion/{name}" in names, name
        readme = zipped.read("jarvis-companion/README-FIRST.txt").decode("utf-8")
        assert "Extract" in readme and "double-click Install-Jarvis.cmd" in readme
        assert "uv" in readme and "not a signed" in readme
        assert not any(name.endswith(("/.env", "/ACCESS.txt")) for name in names)
        extracted = tmp_path / "extracted source with spaces"
        zipped.extractall(extracted)
    source = extracted / "jarvis-companion"
    destination = tmp_path / "installed from zip"
    # The legacy companion package installs through its own script; Install-Jarvis.cmd now targets the desktop app.
    result = run_ps(source / "scripts/install_companion.ps1", tmp_path, "-SourceOnly", "-Destination", destination)
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("Install-Jarvis.cmd", "Start-Jarvis.cmd", "README-FIRST.txt"):
        assert (destination / name).is_file(), name
    assert not (destination / ".env").exists()


def test_missing_uv_is_bootstrapped_from_hash_verified_release_not_a_script():
    installer = (ROOT / "scripts/install_app.ps1").read_text()
    common = (ROOT / "installer/common.ps1").read_text()
    assert "Install-UvStandalone" in installer
    assert "uv-x86_64-pc-windows-msvc.zip" in common and ".sha256" in common
    assert common.index("Get-Sha256 $zipFile") < common.index("Expand-ZipSafely -ZipPath $zipFile")
    for text in (installer.lower(), common.lower()):
        for forbidden in ("install.ps1", "invoke-expression", "| iex", "irm "):
            assert forbidden not in text


def test_start_falls_back_to_extracted_checkout_without_local_install(tmp_path):
    import shutil
    source = tmp_path / "checkout with spaces"
    (source / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "Start-Jarvis.cmd", source / "Start-Jarvis.cmd")
    (source / "scripts/start_companion.cmd").write_text("@echo MUST_NOT_START\n")
    result = run_cmd(source / "Start-Jarvis.cmd", tmp_path, env=dict(os.environ, LOCALAPPDATA=str(tmp_path / "empty app data")))
    assert result.returncode == 1
    assert str(source / ".env") in result.stdout
    assert "MUST_NOT_START" not in result.stdout


def test_install_missing_extracted_files_has_friendly_error(tmp_path):
    import shutil
    wrapper = tmp_path / "Install-Jarvis.cmd"
    shutil.copyfile(ROOT / "Install-Jarvis.cmd", wrapper)
    result = run_cmd(wrapper, tmp_path)
    assert result.returncode == 1
    assert "Extract the entire ZIP first" in result.stdout
    assert "Press any key" in result.stdout


def test_double_click_wrappers_do_not_elevate_or_fetch_remote_scripts():
    install = (ROOT / "Install-Jarvis.cmd").read_text()
    assert 'cd /d "%~dp0"' in install
    assert '-NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\\install_app.ps1" %*' in install
    for name in ("Install-Jarvis.cmd", "Start-Jarvis.cmd"):
        content = (ROOT / name).read_text().lower()
        for forbidden in ("runas", "invoke-webrequest", "irm ", "iex ", "-autostart", "-startmenu"):
            assert forbidden not in content
