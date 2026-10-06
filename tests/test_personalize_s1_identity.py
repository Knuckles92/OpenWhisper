"""Which app has focus, per platform, and the Windows UI Automation text read."""

import contextlib
import ctypes
import os
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

from services import desktop_session, hyprland
from services.focus_context import AppIdentity, TextContext, _linux, _mac


# --- Windows identity ----------------------------------------------------------


class FakeWin32:
    def __init__(self, hwnd=0x100, pid=500, exe="chrome.exe", title="", uwp=(None, 0), images=None):
        self.user32 = SimpleNamespace(GetForegroundWindow=lambda: hwnd)
        self.pid = pid
        self.exe = exe
        self.titles = []
        self.title_text = title
        self.uwp = uwp
        self.images = images or {}

    def window_pid(self, hwnd):
        return self.pid

    def image_name(self, pid):
        return self.images.get(pid, self.exe)

    def class_name(self, hwnd):
        return ""

    def title(self, hwnd):
        self.titles.append(hwnd)
        return self.title_text

    def uwp_app_window(self, frame, frame_pid):
        return self.uwp


@pytest.fixture
def win(monkeypatch):
    from services.focus_context import _win

    def install(fake):
        monkeypatch.setattr(_win, "_win32", lambda: fake)
        return fake

    return _win, install


def test_windows_names_the_foreground_app_and_a_browser_site(win):
    module, install = win
    fake = install(FakeWin32(title="Inbox (2) - me@example.com - Gmail - Google Chrome"))

    identity = module.foreground_identity()

    assert identity == AppIdentity("chrome.exe", "Chrome", pid=500, window="0x100",
                                   title_hint="Gmail", platform="windows")
    assert fake.titles == [0x100]


def test_windows_reads_no_title_outside_browsers(win):
    module, install = win
    fake = install(FakeWin32(exe="winword.exe", title="Secret plans - Word"))

    identity = module.foreground_identity()

    assert (identity.app_id, identity.name, identity.title_hint) == ("winword.exe", "Word", "")
    assert fake.titles == []


def test_windows_resolves_store_apps_behind_their_frame(win):
    module, install = win
    install(FakeWin32(exe="applicationframehost.exe", uwp=(0x200, 777),
                      images={777: "whatsapp.root.exe"}))

    identity = module.foreground_identity()

    assert (identity.app_id, identity.name, identity.pid) == ("whatsapp.root.exe", "WhatsApp", 777)


def test_windows_marks_openwhisper_itself_without_reading_more(win):
    module, install = win
    fake = install(FakeWin32(pid=os.getpid(), exe="python.exe"))

    identity = module.foreground_identity()

    assert identity.is_self and identity.app_id == "openwhisper"
    assert fake.titles == []


@pytest.mark.parametrize("fake", [
    FakeWin32(hwnd=0),
    FakeWin32(pid=0),
    FakeWin32(exe=""),
])
def test_windows_unknown_foreground_is_none(win, fake):
    module, install = win
    install(fake)
    assert module.foreground_identity() is None


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 foreground window")
def test_windows_real_identity_is_quick_and_never_raises():
    from services.focus_context._win import foreground_identity

    foreground_identity()
    started = time.perf_counter()
    for _ in range(20):
        identity = foreground_identity()
    assert (time.perf_counter() - started) / 20 < 0.02
    assert identity is None or isinstance(identity, AppIdentity)


# --- macOS identity --------------------------------------------------------------


def _fake_appkit(app):
    workspace = SimpleNamespace(frontmostApplication=lambda: app)
    appkit = types.ModuleType("AppKit")
    appkit.NSWorkspace = SimpleNamespace(sharedWorkspace=lambda: workspace)
    objc = types.ModuleType("objc")
    objc.autorelease_pool = contextlib.nullcontext
    return appkit, objc


def _mac_app(bundle, name, pid):
    return SimpleNamespace(bundleIdentifier=lambda: bundle, localizedName=lambda: name,
                           processIdentifier=lambda: pid)


@pytest.mark.parametrize("bundle,name,app_id,display", [
    ("com.microsoft.Outlook", "Microsoft Outlook", "com.microsoft.outlook", "Outlook"),
    ("com.example.Notebook", "Notebook Pro", "com.example.notebook", "Notebook Pro"),
    (None, "Mystery", "mystery", "Mystery"),
])
def test_macos_names_the_frontmost_app(monkeypatch, bundle, name, app_id, display):
    appkit, objc = _fake_appkit(_mac_app(bundle, name, 4242))
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "objc", objc)

    identity = _mac.frontmost_identity()

    assert identity == AppIdentity(app_id, display, pid=4242, platform="macos")


def test_macos_marks_itself_and_handles_no_app(monkeypatch):
    appkit, objc = _fake_appkit(_mac_app("org.python.python", "Python", os.getpid()))
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "objc", objc)
    assert _mac.frontmost_identity().is_self

    appkit, objc = _fake_appkit(None)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    assert _mac.frontmost_identity() is None


def test_macos_imports_appkit_in_the_background_first(monkeypatch):
    monkeypatch.delitem(sys.modules, "AppKit", raising=False)
    monkeypatch.setattr(_mac, "_import_started", False)
    imported = threading.Event()
    monkeypatch.setattr(_mac, "_import_appkit", imported.set)

    assert _mac.frontmost_identity() is None
    assert imported.wait(5)
    assert _mac.frontmost_identity() is None


class FakeAX(types.ModuleType):
    def __init__(self, text, start, length, pid=4242, role="AXTextArea"):
        super().__init__("HIServices")
        self.text, self.start, self.length = text, start, length
        self.pid, self.role = pid, role
        for name in ("kAXFocusedUIElementAttribute", "kAXRoleAttribute", "kAXSubroleAttribute",
                     "kAXSelectedTextRangeAttribute", "kAXNumberOfCharactersAttribute",
                     "kAXValueCFRangeType", "kAXStringForRangeParameterizedAttribute"):
            setattr(self, name, name)

    def AXUIElementCreateSystemWide(self):
        return "system"

    def AXUIElementSetMessagingTimeout(self, element, seconds):
        self.timeout = seconds

    def AXIsProcessTrusted(self):
        return True

    def AXUIElementCopyAttributeValue(self, element, name, _out):
        values = {
            "kAXFocusedUIElementAttribute": "field",
            "kAXRoleAttribute": self.role,
            "kAXSubroleAttribute": None,
            "kAXSelectedTextRangeAttribute": "span",
            "kAXNumberOfCharactersAttribute": len(self.text),
        }
        return 0, values.get(name)

    def AXUIElementGetPid(self, element, _out):
        return 0, self.pid

    def AXValueGetValue(self, value, kind, _out):
        return True, SimpleNamespace(location=self.start, length=self.length)

    def AXValueCreate(self, kind, span):
        return span

    def AXUIElementCopyParameterizedAttributeValue(self, element, name, span, _out):
        location, length = span
        return 0, self.text[location:location + length]


def test_macos_text_stays_off_until_tested_on_a_mac(monkeypatch):
    sample = "Lunch with Siobhan tomorrow"
    monkeypatch.setitem(sys.modules, "HIServices", FakeAX(sample, 11, 7))
    reader = _mac.AxTextReader()
    identity = AppIdentity("com.apple.mail", "Mail", pid=4242)

    assert _mac._AX_TEXT_ENABLED is False
    assert reader.read(identity, include_text=True, include_selection=True) is None


def test_macos_text_reader_logic_with_a_fake_accessibility_api(monkeypatch):
    sample = "Lunch with Siobhan tomorrow"
    fake = FakeAX(sample, 11, 7)
    monkeypatch.setitem(sys.modules, "HIServices", fake)
    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(autorelease_pool=contextlib.nullcontext))
    monkeypatch.setattr(_mac, "_AX_TEXT_ENABLED", True)
    monkeypatch.setattr(_mac, "_secure_input_enabled", lambda: False)
    reader = _mac.AxTextReader()
    identity = AppIdentity("com.apple.mail", "Mail", pid=4242)

    assert reader.read(identity, include_text=True, include_selection=True) == TextContext(
        "Lunch with ", "Siobhan", " tomorrow", caret_known=True, selection_known=True, source="ax")
    assert fake.timeout == 0.3
    fake.role = "AXSecureTextField"
    assert reader.read(identity, include_text=True, include_selection=True) == TextContext(
        source="ax", blocked=True)
    fake.role, fake.pid = "AXTextArea", 1
    assert reader.read(identity, include_text=True, include_selection=True) is None
    monkeypatch.setattr(_mac, "_secure_input_enabled", lambda: True)
    fake.pid = 4242
    assert reader.read(identity, include_text=True, include_selection=True) is None


# --- Linux identity ----------------------------------------------------------------


def test_hyprland_names_the_active_window(monkeypatch):
    clients = iter([
        {"class": "firefox", "title": "Inbox - Gmail — Mozilla Firefox", "pid": 321,
         "address": "0x55"},
        {"class": "", "initialClass": "Slack", "title": "general", "pid": "322"},
        {"class": "openwhisper", "pid": 9},
        {"class": "kitty", "pid": None},
        "not a client",
    ])
    calls = []
    monkeypatch.setattr(hyprland, "available", lambda: True)
    monkeypatch.setattr(hyprland, "query", lambda name: calls.append(name) or next(clients))

    assert _linux.active_identity() == AppIdentity(
        "firefox", "Firefox", pid=321, window="0x55", title_hint="Gmail", platform="hyprland")
    slack = _linux.active_identity()
    assert (slack.app_id, slack.name, slack.pid) == ("slack", "Slack", 322)
    assert _linux.active_identity().is_self
    assert _linux.active_identity().pid is None
    assert _linux.active_identity() is None
    assert set(calls) == {"activewindow"}


def test_other_wayland_desktops_are_unknown(monkeypatch):
    monkeypatch.setattr(hyprland, "available", lambda: False)
    monkeypatch.setattr(desktop_session, "is_wayland_session", lambda: True)
    assert _linux.active_identity() is None


def _fake_xlib(monkeypatch, *, active=0x42, wm_class=("Navigator", "firefox"), pid=777,
               title=b"Chat | Microsoft Teams - Mozilla Firefox"):
    closed = []

    def prop(values):
        return SimpleNamespace(value=values) if values is not None else None

    class Window:
        def get_wm_class(self):
            return wm_class

        def get_full_property(self, atom, _kind):
            return {"_NET_WM_PID": prop([pid] if pid else None),
                    "_NET_WM_NAME": prop(title)}.get(atom)

        def get_wm_name(self):
            return "fallback"

    class Display:
        def screen(self):
            root = SimpleNamespace(get_full_property=lambda atom, _kind: prop([active]))
            return SimpleNamespace(root=root)

        def intern_atom(self, name):
            return name

        def create_resource_object(self, kind, window_id):
            assert (kind, window_id) == ("window", active)
            return Window()

        def close(self):
            closed.append(True)

    xlib = types.ModuleType("Xlib")
    xlib.X = SimpleNamespace(AnyPropertyType=0)
    xlib.display = SimpleNamespace(Display=Display)
    monkeypatch.setitem(sys.modules, "Xlib", xlib)
    monkeypatch.setattr(hyprland, "available", lambda: False)
    monkeypatch.setattr(desktop_session, "is_wayland_session", lambda: False)
    return closed


def test_x11_names_the_active_window(monkeypatch):
    closed = _fake_xlib(monkeypatch)

    assert _linux.active_identity() == AppIdentity(
        "firefox", "Firefox", pid=777, window="0x42", title_hint="Teams", platform="x11")
    assert closed == [True]


def test_x11_without_an_active_window_is_unknown(monkeypatch):
    closed = _fake_xlib(monkeypatch, active=0)
    assert _linux.active_identity() is None
    assert closed == [True]


# --- Windows UI Automation (real) --------------------------------------------------

SAMPLE = "Meeting notes. Lunch with Siobhan about the Q3 roadmap tomorrow."


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UI Automation")
def test_ui_automation_reads_a_hidden_edit_control():
    """Our own hidden EDIT on its own message loop: deterministic, focus-free."""
    from ctypes import byref, wintypes

    from services.focus_context._win_uia import UiaTextReader

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
        wintypes.HINSTANCE, wintypes.LPVOID]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                    wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.MsgWaitForMultipleObjects.argtypes = [
        wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, wintypes.DWORD]
    ready, stop = threading.Event(), threading.Event()
    window = {}

    def pump():
        # Never shown: a hidden popup parent with a child EDIT, off screen.
        parent = user32.CreateWindowExW(0x80, "STATIC", "ow-test", 0x80000000,
                                        -32000, -32000, 400, 200, None, None, None, None)
        edit = user32.CreateWindowExW(0, "EDIT", SAMPLE, 0x40000000 | 0x10000000 | 0x0004,
                                      0, 0, 400, 200, parent, None, None, None)
        start = SAMPLE.index("Siobhan")
        user32.SendMessageW(edit, 0x00B1, start, start + len("Siobhan"))  # EM_SETSEL
        window["edit"] = edit
        ready.set()
        message = wintypes.MSG()
        try:
            while not stop.is_set():
                while user32.PeekMessageW(byref(message), None, 0, 0, 1):
                    user32.TranslateMessage(byref(message))
                    user32.DispatchMessageW(byref(message))
                user32.MsgWaitForMultipleObjects(0, None, False, 20, 0x04FF)
        finally:
            user32.DestroyWindow(parent)

    results = {}

    def read():
        reader = UiaTextReader()
        try:
            results["full"] = reader._read_window(window["edit"], include_text=True,
                                                  include_selection=True)
            results["selection"] = reader._read_window(window["edit"], include_text=False,
                                                       include_selection=True)
        except Exception as exc:
            results["error"] = repr(exc)
        finally:
            reader.close()

    pumping = threading.Thread(target=pump, daemon=True)
    pumping.start()
    try:
        assert ready.wait(5), "the test window never appeared"
        reading = threading.Thread(target=read, daemon=True)
        reading.start()
        # UI Automation's own timeouts end a stuck call well inside this.
        reading.join(10)
        assert not reading.is_alive(), "UI Automation did not answer"
    finally:
        stop.set()
        pumping.join(5)

    assert "error" not in results, results.get("error")
    assert results["full"] == TextContext(
        "Meeting notes. Lunch with ", "Siobhan", " about the Q3 roadmap tomorrow.",
        caret_known=True, selection_known=True, source="uia")
    assert results["selection"] == TextContext(selected="Siobhan", selection_known=True,
                                               source="uia")
