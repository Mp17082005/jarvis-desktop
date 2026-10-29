"""Global hotkey for "talk now" (default Ctrl+Alt+J) with no extra dependency.

Win32 ``RegisterHotKey`` with a NULL window posts WM_HOTKEY to the registering thread's message queue,
so each hotkey gets a dedicated thread that registers it and runs a ``GetMessageW`` loop. The user's
callback runs on a short-lived worker thread so a slow callback never stalls the message loop.
``stop()`` posts WM_QUIT to that thread, which unregisters the hotkey before exiting.
"""
from __future__ import annotations

import itertools
import logging
import sys
import threading

log = logging.getLogger("jarvis.hotkey")

DEFAULT = "ctrl+alt+j"
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT, PM_NOREMOVE = 0x0312, 0x0012, 0x0000

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
         "win": MOD_WIN, "windows": MOD_WIN, "super": MOD_WIN}
_MOD_NAME = {MOD_CONTROL: "ctrl", MOD_ALT: "alt", MOD_SHIFT: "shift", MOD_WIN: "win"}
_MOD_ORDER = (MOD_CONTROL, MOD_WIN, MOD_ALT, MOD_SHIFT)
_NAMED = {"space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
          "backspace": 0x08, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21,
          "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "pause": 0x13}
_NAMED.update({f"f{i}": 0x6F + i for i in range(1, 25)})
_ids = itertools.count(0x4A00)  # app-defined ids must be < 0xC000


def _split(combo) -> list[str]:
    if not isinstance(combo, str) or not combo.strip():
        raise ValueError("empty hotkey")
    parts = [p.strip().lower() for p in combo.split("+")]
    if any(not p for p in parts):
        raise ValueError(f"malformed hotkey {combo!r}")
    return parts


def parse_combo(combo: str) -> tuple[int, int]:
    """'ctrl+alt+j' -> (modifier flags, virtual-key code). Raises ValueError when invalid."""
    parts = _split(combo)
    mods, keys = 0, []
    for p in parts:
        if p in _MODS:
            mods |= _MODS[p]
        else:
            keys.append(p)
    if len(keys) != 1:
        raise ValueError(f"hotkey {combo!r} needs exactly one non-modifier key")
    key = keys[0]
    if len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key.upper())
    elif key in _NAMED:
        vk = _NAMED[key]
    else:
        raise ValueError(f"unknown key {key!r} in hotkey {combo!r}")
    if not mods and not key.startswith("f"):
        raise ValueError(f"hotkey {combo!r} needs a modifier (ctrl, alt, shift or win)")
    return mods, vk


def normalize(combo) -> str | None:
    """Canonical lowercase form ('ctrl+alt+j'), or None when the combo is invalid."""
    try:
        mods, _ = parse_combo(combo)
    except ValueError:
        return None
    key = next(p for p in _split(combo) if p not in _MODS)
    return "+".join([_MOD_NAME[m] for m in _MOD_ORDER if mods & m] + [key])


def label(combo) -> str:
    """Human label for menus: 'Ctrl+Alt+J'. Invalid combos show the default."""
    norm = normalize(combo) or DEFAULT
    return "+".join(p.upper() if len(p) <= 3 and p.startswith("f") or len(p) == 1 else p.capitalize()
                    for p in norm.split("+"))


class HotkeyHandle:
    def __init__(self, callback, mods: int, vk: int, combo: str):
        self.callback, self.mods, self.vk, self.combo = callback, mods, vk, combo
        self.id = next(_ids)
        self.thread_id = 0
        self.thread_name = f"jarvis-hotkey-{self.id:x}"
        self._ready = threading.Event()
        self._ok = False
        self._error = ""
        self._stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True, name=self.thread_name)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    # ------------------------------------------------------------- thread
    def _run(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                        wintypes.UINT]
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)  # create this thread's queue
        self.thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, self.id, self.mods | MOD_NOREPEAT, self.vk):
            self._error = f"Windows error {ctypes.get_last_error()}"
            self._ready.set()
            return
        self._ok = True
        self._ready.set()
        try:
            while True:
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r == 0 or r == -1:  # WM_QUIT or error
                    break
                if msg.message == WM_HOTKEY and msg.wParam == self.id and not self._stopped:
                    threading.Thread(target=self._fire, daemon=True, name="jarvis-hotkey-callback").start()
        finally:
            user32.UnregisterHotKey(None, self.id)

    def _fire(self):
        try:
            self.callback()
        except Exception:
            log.exception("hotkey callback failed")

    # ------------------------------------------------------------- control
    def _post(self, message: int, wparam: int = 0) -> bool:
        if not self.thread_id or not self.alive:
            return False
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        return bool(user32.PostThreadMessageW(self.thread_id, message, wparam, 0))

    def simulate_press(self) -> bool:
        """Deliver WM_HOTKEY as if the keys were pressed (tests and diagnostics)."""
        return self._post(WM_HOTKEY, self.id)

    def stop(self, timeout: float = 2.0) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._post(WM_QUIT)
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)


def start_hotkey(callback, combo: str = DEFAULT):
    """Register a global hotkey that calls ``callback()``. Returns a handle with ``stop()``, or None
    (and logs why) when the combo is invalid, already taken by another program, or not on Windows."""
    try:
        mods, vk = parse_combo(combo)
    except ValueError as e:
        log.warning("Hotkey not registered: %s", e)
        return None
    if sys.platform != "win32":
        log.info("Global hotkey is only available on Windows.")
        return None
    handle = HotkeyHandle(callback, mods, vk, normalize(combo) or combo)
    handle._thread.start()
    if not handle._ready.wait(5) or not handle._ok:
        log.warning("Hotkey %s could not be registered (%s); another program may already use it.",
                    label(combo), handle._error or "timeout")
        handle.stop()
        return None
    log.info("Hotkey %s registered.", label(combo))
    return handle
