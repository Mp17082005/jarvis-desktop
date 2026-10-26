"""Bounded, synchronous UFO subprocess adapter.

Durable identity, owner isolation, approval and polling stay in JobQueue/DesktopBridge;
this module never hands out global handles. One run at a time, no automatic retry.
"""
import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

_BASE_ENV = ('SystemRoot', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC', 'PATHEXT', 'PATH', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'HOMEDRIVE', 'HOMEPATH', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData', 'USERNAME', 'COMPUTERNAME', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE')
_VERDICTS = {
    'yes': (True, 'completed', 'UFO self-evaluated the task as complete.'),
    'no': (False, 'not_completed', 'UFO evaluated the task as not completed.'),
    'unsure': (False, 'uncertain', 'UFO could not tell whether the task completed.'),
}


class _Job:
    """Windows job object: terminating it kills the whole descendant tree."""
    def __init__(self):
        self.handle = None
        if os.name != 'nt':
            return
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            return

        class Basic(ctypes.Structure):
            _fields_ = [('a', ctypes.c_int64), ('b', ctypes.c_int64), ('LimitFlags', wintypes.DWORD), ('c', ctypes.c_size_t), ('d', ctypes.c_size_t), ('e', wintypes.DWORD), ('f', ctypes.c_size_t), ('g', wintypes.DWORD), ('h', wintypes.DWORD)]

        class Counters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in 'abcdef']

        class Extended(ctypes.Structure):
            _fields_ = [('Basic', Basic), ('Io', Counters), ('p', ctypes.c_size_t), ('j', ctypes.c_size_t), ('pk', ctypes.c_size_t), ('jk', ctypes.c_size_t)]

        info = Extended()
        info.Basic.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel.CloseHandle(handle)
            return
        self.handle = handle

    def adopt(self, process):
        if not self.handle:
            return False
        kernel = ctypes.windll.kernel32
        kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        return bool(kernel.AssignProcessToJobObject(self.handle, int(process._handle)))

    def kill(self):
        if self.handle:
            ctypes.windll.kernel32.TerminateJobObject(ctypes.c_void_p(self.handle), 1)

    def close(self):
        if self.handle:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None


class UFORunner:
    def __init__(self, home=None, python=None, enabled=None, timeout=None, retain=None, env_allowlist=()):
        self.enabled = (os.getenv('JARVIS_UFO_ENABLED', '').lower() == 'true') if enabled is None else bool(enabled)
        self.home = os.getenv('JARVIS_UFO_HOME', '') if home is None else home
        self.python = python or (str(Path(self.home) / '.venv' / 'Scripts' / 'python.exe') if self.home else '')
        self.timeout = max(1, min(int(timeout if timeout is not None else os.getenv('JARVIS_UFO_TIMEOUT_SECONDS', '600')), 1800))
        self.retain = max(1, int(retain if retain is not None else os.getenv('JARVIS_UFO_LOG_RETAIN', '20')))
        self.env_allowlist = tuple(env_allowlist)
        self._cancel = threading.Event()
        self._busy = threading.Lock()

    def _ready(self):
        if not (self.enabled and self.home and self.python and os.name == 'nt'):
            return False
        home = Path(self.home)
        return home.is_absolute() and (home / 'ufo' / '__main__.py').is_file() and Path(self.python).is_absolute() and Path(self.python).is_file()

    def capabilities(self):
        # Configuration only; never claims arbitrary tasks will succeed.
        return {'enabled': self.enabled, 'configured': bool(self.home), 'available': bool(self._ready()), 'tested': False, 'scope': 'installed_machine', 'requires_approval': True}

    def cancel(self):
        self._cancel.set()

    @staticmethod
    def _valid(args):
        if not isinstance(args, dict) or set(args) != {'request'} or not isinstance(args['request'], str):
            return False
        text = args['request']
        return bool(text.strip()) and len(text) <= 2000 and not any(ord(c) < 32 and c not in '\n\t' for c in text)

    def _environment(self):
        keep = {k.casefold() for k in _BASE_ENV + self.env_allowlist}
        env = {k: v for k, v in os.environ.items() if k.casefold() in keep}
        env.update({'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', 'PYTHONNOUSERSITE': '1'})
        # ufo_task only reaches this runner after Jarvis's exact-argument approval, so the
        # approved task's save/close steps proceed rather than being denied headlessly.
        env['UFO_AUTO_APPROVE_SENSITIVE'] = '1'
        return env

    def _prune(self, logs):
        runs = sorted((p for p in logs.glob('jarvis-*') if p.is_dir() and not p.is_symlink()), key=lambda p: p.stat().st_mtime)
        for old in runs[:-self.retain]:
            shutil.rmtree(old, ignore_errors=True)

    @staticmethod
    def _evaluation(log):
        try:
            lines = (log / 'evaluation.log').read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get('type') == 'evaluation_result' and item.get('complete') in _VERDICTS:
                return item['complete']
        return None

    def run(self, args, cancel_check=None):
        if not self._valid(args):
            return {'ok': False, 'error': 'invalid_arguments'}
        if not self._ready():
            return {'ok': False, 'error': 'ufo_unavailable', 'message': 'UFO is disabled or not configured on this machine.'}
        if not self._busy.acquire(blocking=False):
            return {'ok': False, 'error': 'ufo_busy', 'message': 'A UFO task is already running. Do not retry automatically.'}
        job = _Job()
        try:
            self._cancel.clear()
            home = Path(self.home)
            task = f'jarvis-{int(time.time())}-{uuid.uuid4().hex[:8]}'
            logs = home / 'logs'
            logs.mkdir(exist_ok=True)
            self._prune(logs)
            argv = [self.python, '-m', 'ufo', f'--task={task}', '--mode=normal', f'--request={args["request"]}', '--log-level=OFF']
            try:
                process = subprocess.Popen(argv, shell=False, cwd=str(home), env=self._environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except OSError:
                return {'ok': False, 'error': 'launch_failed', 'outcome': 'failed', 'message': 'UFO could not be started. Not retried.'}
            if not job.adopt(process):
                process.kill()
                process.wait()
                return {'ok': False, 'error': 'launch_failed', 'outcome': 'failed', 'message': 'UFO process containment failed. Not retried.'}
            deadline = time.monotonic() + self.timeout
            stop = None
            while process.poll() is None:
                try:
                    cancelled = bool(cancel_check and cancel_check())
                except Exception:
                    cancelled = True  # a broken cancel channel fails closed
                if cancelled or self._cancel.is_set():
                    stop = 'cancelled'
                elif time.monotonic() >= deadline:
                    stop = 'timeout'
                if stop:
                    job.kill()
                    process.wait(10)
                    break
                time.sleep(0.1)
            if stop == 'cancelled':
                return {'ok': False, 'error': 'cancelled', 'outcome': 'cancelled', 'message': 'UFO was stopped; actions already taken may not be undone and the outcome is uncertain. Not retried.'}
            if stop == 'timeout':
                return {'ok': False, 'error': 'timeout', 'outcome': 'timeout', 'message': 'UFO exceeded its time limit and was stopped; partial side effects may exist. Not retried.'}
            if process.returncode != 0:
                return {'ok': False, 'error': 'ufo_failed', 'outcome': 'failed', 'message': 'UFO exited with an error; side effects may have happened. Not retried.'}
            verdict = self._evaluation(logs / task)
            if verdict is None:
                return {'ok': False, 'error': 'ufo_uncertain', 'outcome': 'uncertain', 'message': 'UFO exited but produced no readable evaluation; success is unverified.'}
            ok, outcome, message = _VERDICTS[verdict]
            return {'ok': ok, 'outcome': outcome, 'message': message}
        finally:
            try:
                self._prune(Path(self.home) / 'logs')
            except OSError:
                pass
            job.kill()
            job.close()
            self._busy.release()
