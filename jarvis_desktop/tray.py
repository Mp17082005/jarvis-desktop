"""Optional system-tray icon (pystray). Without pystray Jarvis runs console-only."""
from __future__ import annotations

import threading


def _icon_image(color):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=color)
    d.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 230))
    return img


def start_tray(app):
    """Start the tray icon in a background thread; return the icon, or None if pystray is unavailable."""
    try:
        import pystray
    except Exception:
        return None
    colors = {"Paused": (128, 128, 128, 255)}

    def title():
        return f"Jarvis: {app.status_text}"

    def toggle(icon, item):
        app.set_paused(not app.paused)

    def settings(icon, item):
        def run():
            from .setup_wizard import run_wizard
            run_wizard()
        threading.Thread(target=run, daemon=True).start()

    def quit_(icon, item):
        app.quit()
        icon.stop()

    items = []
    if callable(getattr(app, "wake_now", None)):
        from .hotkey import DEFAULT, label
        combo = getattr(getattr(app, "settings", None), "hotkey", None) or DEFAULT

        def talk(icon, item):
            app.wake_now()
        # default=True: a left-click on the tray icon starts listening right away.
        items.append(pystray.MenuItem(f"Talk now ({label(combo)})", talk, default=True))
    menu = pystray.Menu(
        *items,
        pystray.MenuItem(lambda item: app.status_text, None, enabled=False),
        pystray.MenuItem(lambda item: "Resume listening" if app.paused else "Pause listening", toggle),
        pystray.MenuItem("Settings", settings),
        pystray.MenuItem("Quit", quit_),
    )
    try:
        icon = pystray.Icon("jarvis", _icon_image((40, 120, 220, 255)), title(), menu)
    except Exception:
        return None
    previous = app.on_state

    def on_state(a):
        if previous:
            previous(a)
        try:
            icon.title = title()
            icon.icon = _icon_image(colors.get(a.status_text, (40, 120, 220, 255) if a.state.value == "idle"
                                               else (30, 170, 90, 255)))
            icon.update_menu()
        except Exception:
            pass

    app.on_state = on_state
    threading.Thread(target=icon.run, daemon=True, name="jarvis-tray").start()
    return icon
