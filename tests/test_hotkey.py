"""Global hotkey (Win32 RegisterHotKey on its own message-loop thread)."""
import sys
import threading
import time

import pytest

from jarvis_desktop import hotkey

ALT, CTRL, SHIFT, WIN = 0x1, 0x2, 0x4, 0x8


@pytest.mark.parametrize("combo,mods,vk", [
    ("ctrl+alt+j", CTRL | ALT, ord("J")),
    ("Ctrl + Alt + J", CTRL | ALT, ord("J")),
    ("win+shift+space", WIN | SHIFT, 0x20),
    ("control+shift+5", CTRL | SHIFT, ord("5")),
    ("alt+f12", ALT, 0x7B),
    ("ctrl+alt+f24", CTRL | ALT, 0x87),
    ("windows+enter", WIN, 0x0D),
    ("f9", 0, 0x78),
])
def test_parse_combo(combo, mods, vk):
    assert hotkey.parse_combo(combo) == (mods, vk)


@pytest.mark.parametrize("bad", ["", "ctrl+alt", "ctrl+alt+j+k", "ctrl+banana", "j", "ctrl++", "shift+f25", None])
def test_parse_combo_rejects(bad):
    with pytest.raises(ValueError):
        hotkey.parse_combo(bad)


def test_label():
    assert hotkey.label("ctrl+alt+j") == "Ctrl+Alt+J"
    assert hotkey.label("win+shift+space") == "Win+Shift+Space"
    assert hotkey.label("nonsense+") == "Ctrl+Alt+J"


def test_normalize():
    assert hotkey.normalize(" Control+ALT+j ") == "ctrl+alt+j"
    assert hotkey.normalize("bad+") is None


def test_start_returns_none_for_bad_combo_or_non_windows(monkeypatch):
    assert hotkey.start_hotkey(lambda: None, "ctrl+banana") is None
    monkeypatch.setattr(hotkey.sys, "platform", "linux")
    assert hotkey.start_hotkey(lambda: None, "ctrl+alt+j") is None


win_only = pytest.mark.skipif(sys.platform != "win32", reason="Win32 hotkeys")
COMBO = "ctrl+alt+shift+f23"  # unusual: nobody has this


@win_only
def test_real_registration_press_stop_and_reregister():
    fired = threading.Event()
    threads = []

    def cb():
        threads.append(threading.current_thread().name)
        fired.set()

    h = hotkey.start_hotkey(cb, COMBO)
    assert h is not None and h.thread_id
    try:
        # A second registration of the same combo must fail cleanly (combo in use).
        assert hotkey.start_hotkey(lambda: None, COMBO) is None
        assert h.simulate_press()  # posts WM_HOTKEY with the registered id to the hotkey thread
        assert fired.wait(3)
        assert threads and threads[0] != h.thread_name  # callback never blocks the message loop
    finally:
        h.stop()
    assert not h.alive
    h.stop()  # idempotent
    h2 = hotkey.start_hotkey(lambda: None, COMBO)
    assert h2 is not None
    h2.stop()


@win_only
def test_callback_exception_does_not_kill_loop():
    hits = []

    def cb():
        hits.append(1)
        raise RuntimeError("boom")

    h = hotkey.start_hotkey(cb, COMBO)
    assert h is not None
    try:
        h.simulate_press()
        deadline = time.time() + 3
        while len(hits) < 1 and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.1)
        h.simulate_press()
        deadline = time.time() + 3
        while len(hits) < 2 and time.time() < deadline:
            time.sleep(0.02)
        assert len(hits) == 2 and h.alive
    finally:
        h.stop()
