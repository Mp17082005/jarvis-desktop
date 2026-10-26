"""Local Windows companion. Importing this module never opens a microphone."""
from __future__ import annotations

import os
from pathlib import Path
import sys

# Direct execution must not shadow stdlib queue with companion/queue.py.
if __name__ == "__main__" and not __package__:
    sys.path[0] = str(Path(__file__).resolve().parents[1])


def build_launch_plan(root: Path, state: Path, port: int, environ=None) -> dict:
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("Port must be an integer between 1024 and 65535")
    env = dict(os.environ if environ is None else environ)
    for name in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(name, None)
    env.update(JARVIS_DESKTOP_ENABLED="true", JARVIS_DESKTOP_LOCAL_ONLY="true",
               JARVIS_DATA_DIR=str(state / "data"), JARVIS_WORKSPACE_DIR=str(state / "workspace"))
    return {"command": [sys.executable, "-m", "uvicorn", "jarvis.main:app", "--host", "127.0.0.1",
                        "--port", str(port), "--no-proxy-headers"],
            "env": env, "cwd": str(root), "url": f"http://127.0.0.1:{port}"}


class WakeController:
    """Backend-owned local wake listener; opt in via start(), never at import.

    status() checks prerequisites without recording. start() is explicit consent;
    stop() releases the mic before browser realtime starts. No PCM leaves this
    process. The backend must stop on logout, lost client heartbeat and shutdown.
    """

    def __init__(self, model_path=None):
        import queue
        import threading
        self.model_path = Path(model_path or os.getenv("JARVIS_WAKE_MODEL_PATH") or "__missing_model__")
        self._reason = "stopped"
        self._listening = False
        self._stream = None
        self._thread = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._audio = queue.Queue(maxsize=8)
        self._events = queue.Queue(maxsize=1)
        self._model = None
        self._last_wake = float("-inf")

    def _readiness(self):
        if not all((self.model_path / part).is_file() for part in ("am/final.mdl", "conf/model.conf")):
            return "model_missing"
        try:
            import sounddevice as sd
            import vosk  # noqa: F401
        except (ImportError, OSError):
            return "dependencies_missing"
        try:
            if sd.query_devices(kind="input")["max_input_channels"] < 1:
                return "microphone_unavailable"
        except Exception:
            return "microphone_unavailable"
        return "ready"

    def status(self):
        with self._lock:
            ready = "ready" if self._listening else self._readiness()
            reason = self._reason if ready == "ready" and self._reason not in ("stopped", "listening") else ready
            return {"available": ready == "ready" and reason not in ("start_failed", "recognition_failed"),
                    "listening": self._listening, "reason": "listening" if self._listening else reason,
                    "local_only": True}

    def start(self):
        import json
        import threading
        with self._lock:
            if self._listening:
                return True
            self.stop()
            self._reason = self._readiness()
            if self._reason != "ready":
                return False
            try:
                import sounddevice as sd
                from vosk import Model, KaldiRecognizer
                if self._model is None:
                    self._model = Model(str(self.model_path))
                recognizer = KaldiRecognizer(self._model, 16000, json.dumps(["jarvis", "[unk]"]))
                self._stop.clear()
                self._stream = sd.RawInputStream(samplerate=16000, blocksize=4000,
                                                dtype="int16", channels=1, callback=self._on_audio)
                self._stream.start()
                self._listening = True
                self._reason = "listening"
                self._thread = threading.Thread(target=self._recognize, args=(recognizer,), daemon=True,
                                                name="jarvis-local-wake")
                self._thread.start()
                return True
            except Exception:
                self.stop()
                self._reason = "start_failed"
                return False

    def _on_audio(self, indata, frames, timing, status):
        import queue
        if self._stop.is_set() or status:
            return
        try:
            self._audio.put_nowait(bytes(indata))
        except queue.Full:
            pass  # bounded queue: never accumulate an audio archive

    def _recognize(self, recognizer):
        import json
        import queue
        import time
        try:
            while not self._stop.is_set():
                try:
                    pcm = self._audio.get(timeout=.1)
                except queue.Empty:
                    continue
                if recognizer.AcceptWaveform(pcm):
                    text = json.loads(recognizer.Result()).get("text", "").strip().lower()
                    now = time.monotonic()
                    if text == "jarvis" and now - self._last_wake >= 3 and not self._stop.is_set():
                        self._last_wake = now
                        try:
                            self._events.put_nowait(now)
                        except queue.Full:
                            pass
        except Exception:
            self._reason = "recognition_failed"
            self._listening = False
            self._stop.set()
            if self._stream:
                try:
                    self._stream.close()
                except Exception:
                    pass

    def stop(self):
        import queue
        import threading
        with self._lock:
            self._stop.set()
            if self._stream is not None:
                try:
                    self._stream.stop()
                except Exception:
                    pass
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            if self._thread and self._thread is not threading.current_thread():
                self._thread.join(timeout=2)
            self._thread = None
            self._listening = False
            self._reason = "stopped"
            for pending in (self._audio, self._events):
                while True:
                    try:
                        pending.get_nowait()
                    except queue.Empty:
                        break

    def pop_event(self):
        import queue
        import time
        try:
            timestamp = self._events.get_nowait()
        except queue.Empty:
            return False
        return self._listening and time.monotonic() - timestamp <= 10


def find_browser(environ=None):
    env = os.environ if environ is None else environ
    for relative in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if env.get(key):
                candidate = Path(env[key]) / relative
                if candidate.is_file():
                    return candidate
    return None


def browser_command(browser, profile, url):
    return [str(browser), f"--user-data-dir={profile}", f"--app={url}",
            "--no-first-run", "--no-default-browser-check"]


def doctor(root):
    import importlib.util
    import platform
    packages = {name: importlib.util.find_spec(name) is not None for name in ("uvicorn", "vosk", "sounddevice")}
    microphone = {"available": False, "reason": "dependencies_missing"}
    if packages["sounddevice"]:
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            inputs = [d for d in devices if d["max_input_channels"] > 0]
            microphone = {"available": bool(inputs), "input_devices": len(inputs),
                          "reason": "device_present_not_recording_test" if inputs else "microphone_unavailable"}
        except Exception:
            microphone = {"available": False, "reason": "microphone_unavailable"}
    browser = find_browser()
    from dotenv import dotenv_values
    config = dotenv_values(root / ".env")
    model_path = os.getenv("JARVIS_WAKE_MODEL_PATH") or config.get("JARVIS_WAKE_MODEL_PATH")
    if not model_path and (root / "models" / "vosk-model-small-en-us-0.15").is_dir():
        model_path = str(root / "models" / "vosk-model-small-en-us-0.15")
    return {"platform": platform.system(), "python": platform.python_version(), "bind": "127.0.0.1",
            "browser": str(browser) if browser else None, "packages": packages,
            "configuration_file_present": (root / ".env").is_file(),
            "microphone": microphone, "wake": WakeController(model_path=model_path).status()}


def package_source(root, destination):
    """Source ZIP, not a compiled executable; positive file allowlist."""
    import zipfile
    root = Path(root).resolve()
    destination = Path(destination).resolve()
    files = [root / name for name in ("Install-Jarvis.cmd", "Start-Jarvis.cmd", "README-FIRST.txt",
             "pyproject.toml", "uv.lock", "jarvis_tools.py", ".env.example",
             "scripts/install_companion.ps1", "scripts/start_companion.cmd", "docs/DESKTOP-COMPANION.md", "docs/DEVICE-PAIRING.md")]
    for directory, extensions in (("jarvis", {".py"}), ("companion", {".py"}),
                                  ("static", {".html", ".js", ".css", ".svg", ".png", ".ico", ".woff2"})):
        files.extend(path for path in (root / directory).rglob("*")
                     if path.suffix.lower() in extensions and not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(root).parts))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
                archive.write(path, "jarvis-companion/" + path.relative_to(root).as_posix())
    return destination


def main(argv=None):
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Jarvis Windows local companion")
    parser.add_argument("--doctor", action="store_true", help="Print readiness; never record or expose secret values")
    parser.add_argument("--package", type=Path, help="Write a source-only install ZIP, excluding secrets and runtime data")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--state-dir", type=Path, default=Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "JarvisCompanion" / "state")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--run-seconds", type=float, default=None, help="Bounded local smoke run; stop child on exit")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    if args.package:
        print(package_source(root, args.package))
        return 0
    if args.doctor:
        print(json.dumps(doctor(root), indent=2))
        return 0
    import socket
    import subprocess
    import time
    import urllib.request
    import urllib.error
    if sys.platform != "win32":
        parser.error("This companion launcher supports Windows only")
    if args.run_seconds is not None and not 0 < args.run_seconds <= 3600:
        parser.error("--run-seconds must be between 0 and 3600")
    try:
        plan = build_launch_plan(root, args.state_dir.resolve(), args.port)
    except ValueError as exc:
        parser.error(str(exc))
    # Do not attach to or open an unrelated process already on this port.
    with socket.socket() as check:
        try:
            check.bind(("127.0.0.1", args.port))
        except OSError:
            print("Companion port is occupied; stop that instance or choose --port.", file=sys.stderr)
            return 1
    browser = find_browser()
    if not args.no_browser and not browser:
        print("Install Chrome or Edge, or launch with --no-browser.", file=sys.stderr)
        return 1
    args.state_dir.mkdir(parents=True, exist_ok=True)
    for name in ("data", "workspace"):
        (args.state_dir / name).mkdir(exist_ok=True)
    # First-run config remains local to the installation. Never print tokens.
    from dotenv import load_dotenv
    load_dotenv(root / ".env", override=False)
    plan = build_launch_plan(root, args.state_dir.resolve(), args.port)
    if not plan["env"].get("JARVIS_WAKE_MODEL_PATH"):
        model = root / "models" / "vosk-model-small-en-us-0.15"
        if model.is_dir():
            plan["env"]["JARVIS_WAKE_MODEL_PATH"] = str(model)
    child = subprocess.Popen(plan["command"], cwd=plan["cwd"], env=plan["env"],
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        deadline = time.monotonic() + 30
        ready = False
        while time.monotonic() < deadline:
            if child.poll() is not None:
                return child.returncode or 1
            try:
                with opener.open(plan["url"] + "/", timeout=1) as response:
                    ready = response.status == 200
            except (OSError, urllib.error.URLError):
                pass
            if ready:
                break
            time.sleep(.2)
        if not ready or child.poll() is not None:
            print("Companion startup failed; inspect the local console.", file=sys.stderr)
            return 1
        print("Ready: " + plan["url"], flush=True)
        if not args.no_browser:
            subprocess.Popen(browser_command(browser, args.state_dir.resolve() / "browser-profile", plan["url"]))
        if args.run_seconds is not None:
            time.sleep(args.run_seconds)
            return 0 if child.poll() is None else (child.returncode or 1)
        print("Keep this console open. Ctrl+C stops the companion.", flush=True)
        return child.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        if child.poll() is None:
            import signal
            try:
                child.send_signal(signal.CTRL_BREAK_EVENT)
                child.wait(timeout=8)
            except (OSError, subprocess.TimeoutExpired):
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()



if __name__ == "__main__":
    raise SystemExit(main())

