"""Classic console windows are terminals whatever program owns them."""

import os
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from config import config
from services import app_styles, cleanup_prompts, synthetic_keys
from services.app_styles import Surface
from services.focus_context import FocusSnapshot, _win, catalog
from services.focus_context._capture import CaptureService
from services.runtime.command import CommandRuntime

HWND = 0x10876
# Windows pids are multiples of four, so this is never OpenWhisper's own.
CLIENT_PID = os.getpid() + 1
MEDIUM = {"transcript_cleanup_enabled": True, "transcript_cleanup_level": "medium"}


class FakeWin32:
    def __init__(self, exe, window_class):
        self.user32 = SimpleNamespace(GetForegroundWindow=lambda: HWND)
        self.exe = exe
        self.window_class = window_class
        self.titles = []

    def window_pid(self, hwnd):
        # GetWindowThreadProcessId on a classic console reports its first
        # client process, not the conhost.exe that draws the window.
        return CLIENT_PID

    def image_name(self, pid):
        return self.exe

    def class_name(self, hwnd):
        return self.window_class

    def title(self, hwnd):
        self.titles.append(hwnd)
        return ""

    def uwp_app_window(self, frame, frame_pid):
        return None, 0


def _identity(monkeypatch, exe, window_class="ConsoleWindowClass"):
    fake = FakeWin32(exe, window_class)
    monkeypatch.setattr(_win, "_win32", lambda: fake)
    return _win.foreground_identity()


def _text_allowed(identity):
    platform = SimpleNamespace(name="fake", sync_identity=True, text_supported=True)
    service = CaptureService(platform, settings=lambda: {})
    try:
        return service._text_allowed(identity)
    finally:
        service.shutdown()


@pytest.mark.parametrize("client", ["ubuntu.exe", "wsl.exe", "ssh.exe", "python.exe",
                                    "claude.exe", ""])
@pytest.mark.parametrize("window_class", ["ConsoleWindowClass", "PseudoConsoleWindow"])
def test_a_classic_console_gets_every_terminal_protection(monkeypatch, client, window_class):
    identity = _identity(monkeypatch, client, window_class)

    assert identity is not None
    assert (identity.pid, identity.window) == (CLIENT_PID, hex(HWND))
    assert catalog.is_terminal(identity)
    assert synthetic_keys.is_terminal(identity)
    assert not _text_allowed(identity)
    assert app_styles.style_for(FocusSnapshot(identity), {}).surface == Surface.TERMINAL
    assert (cleanup_prompts.inline_lists_block(MEDIUM, FocusSnapshot(identity))
            == config.TRANSCRIPT_CLEANUP_TERMINAL_LINES)


@pytest.mark.parametrize("client, name", [
    ("powershell.exe", "PowerShell"),
    ("pwsh.exe", "PowerShell"),
    ("cmd.exe", "Console"),
])
def test_a_console_run_by_a_known_shell_keeps_its_name(monkeypatch, client, name):
    identity = _identity(monkeypatch, client)

    assert (identity.app_id, identity.name) == (client, name)
    assert synthetic_keys.is_terminal(identity)


def test_other_windows_keep_their_own_app(monkeypatch):
    identity = _identity(monkeypatch, "python.exe", "TkTopLevel")

    assert identity.app_id == "python.exe"
    assert not catalog.is_terminal(identity)


def test_command_mode_never_sends_a_copy_to_a_classic_console(monkeypatch):
    identity = _identity(monkeypatch, "ubuntu.exe")
    focus: Future = Future()
    focus.set_result(FocusSnapshot(identity))
    emitted, results = [], []
    runtime = SimpleNamespace(_qt=SimpleNamespace(requested=SimpleNamespace(emit=emitted.append)))
    monkeypatch.setattr(synthetic_keys, "wait_for_modifiers_released",
                        lambda timeout_s=0.8: True)

    CommandRuntime._read_selection(runtime, focus, results.append)

    assert results == [""]
    assert emitted == []
