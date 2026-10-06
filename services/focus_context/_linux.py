"""The active window on Linux: Hyprland's IPC, else X11, else unknown.

Both can block (``hyprctl`` is a subprocess with a 2 s timeout, X11 a socket
round trip), so the capture service calls this on its own thread. Other
Wayland desktops offer no portable way to ask, so the app stays unknown.
"""

from __future__ import annotations

import os
from typing import Optional

from services.focus_context import AppIdentity, catalog

#: Window classes of OpenWhisper's own windows (see services/hyprland.py).
_SELF_CLASSES = ("openwhisper", "openwhisper-ui-qa")


def active_identity() -> Optional[AppIdentity]:
    from services import hyprland

    if hyprland.available():
        return _hyprland_identity(hyprland.query("activewindow"))
    from services.desktop_session import is_wayland_session

    if is_wayland_session():
        return None
    return _x11_identity()


def _identity(app_class: str, pid, title: str, window: str, platform: str) -> Optional[AppIdentity]:
    app_id = (app_class or "").strip().lower()
    if not app_id:
        return None
    try:
        pid = int(pid) or None
    except (TypeError, ValueError):
        pid = None
    if app_id in _SELF_CLASSES or (pid is not None and pid == os.getpid()):
        return AppIdentity("openwhisper", "OpenWhisper", pid=pid, window=window,
                           is_self=True, platform=platform)
    return AppIdentity(
        app_id,
        catalog.app_name(app_id),
        pid=pid,
        window=window,
        title_hint=catalog.title_hint(app_id, title or ""),
        platform=platform,
    )


def _hyprland_identity(client) -> Optional[AppIdentity]:
    if not isinstance(client, dict):
        return None
    app_class = client.get("class") or client.get("initialClass") or ""
    return _identity(
        str(app_class), client.get("pid"), str(client.get("title") or ""),
        str(client.get("address") or ""), "hyprland",
    )


def _x11_identity() -> Optional[AppIdentity]:
    from Xlib import X
    from Xlib import display as xdisplay

    connection = xdisplay.Display()
    try:
        root = connection.screen().root
        active = root.get_full_property(
            connection.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
        window_id = int(active.value[0]) if active is not None and len(active.value) else 0
        if not window_id:
            return None
        window = connection.create_resource_object("window", window_id)
        wm_class = window.get_wm_class() or ()
        # WM_CLASS is (instance, class); the class names the application.
        app_class = wm_class[-1] if wm_class else ""
        pid_property = window.get_full_property(
            connection.intern_atom("_NET_WM_PID"), X.AnyPropertyType)
        pid = int(pid_property.value[0]) if pid_property is not None and len(pid_property.value) else None
        title = ""
        if catalog.is_browser(str(app_class).lower()):
            name = window.get_full_property(
                connection.intern_atom("_NET_WM_NAME"), connection.intern_atom("UTF8_STRING"))
            raw = name.value if name is not None else window.get_wm_name()
            title = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
        return _identity(str(app_class), pid, title, hex(window_id), "x11")
    finally:
        connection.close()
