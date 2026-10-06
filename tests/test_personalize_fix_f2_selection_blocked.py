"""Apps on "Never read from" and password fields are not read by a copy either.

The capture refuses to read their text; Command Mode and transforms used to
take that refusal for "unknown" and copy the selection with Ctrl+C instead,
sending it to the AI model and saving it in History.
"""

import ctypes
import sys
import threading
from concurrent.futures import Future
from ctypes import byref
from types import SimpleNamespace

import pytest

from services import focus_context, synthetic_keys, text_rewrite
from services.dictation_pipeline import DictationJob, JobMode
from services.focus_context import AppIdentity, FocusSnapshot, TextContext
from services.focus_context._capture import CaptureService
from services.runtime import command
from services.runtime.command import CommandRefused, CommandRuntime, UnreadSelection
from services.settings import RecordingTriggerMode, SettingsKey
from tests.fakes.settings import InMemorySettings
from tests.test_personalize_s1_service import FakePlatform
from tests.test_personalize_s3_command_runtime import FakeRuntime, FakeUI, Signal, _settle

KEEPASS = AppIdentity("keepassxc.exe", "KeePassXC", pid=21, window="0x21")
NOTEPAD = AppIdentity("notepad.exe", "Notepad", pid=4, window="0x4")
EXCLUDED_MESSAGE = "OpenWhisper doesn't read text in KeePassXC"
PASSWORD_MESSAGE = "OpenWhisper doesn't read password fields"


def _capture(platform, excluded=("KeePassXC",)):
    return CaptureService(
        platform, deadline_s=1.0,
        settings=lambda: {"app_context_excluded_apps": list(excluded)},
        metrics=lambda **values: None,
    )


@pytest.mark.parametrize("text_supported", [True, False])
def test_a_selection_request_in_an_excluded_app_comes_back_refused(text_supported):
    platform = FakePlatform(KEEPASS, text=text_supported)
    service = _capture(platform)
    try:
        snapshot = service.request(include_text=False, include_selection=True).result(timeout=2)
        dictation = service.request(include_text=True).result(timeout=2)
    finally:
        service.shutdown()

    assert snapshot.identity == KEEPASS
    assert snapshot.text is not None and snapshot.text.blocked
    assert not snapshot.text.selection_known and not snapshot.text.caret_known
    # Dictation just gets no text, as before.
    assert dictation == FocusSnapshot(KEEPASS)
    assert platform.reads == []


def test_other_apps_are_still_read():
    platform = FakePlatform(NOTEPAD)
    service = _capture(platform)
    try:
        snapshot = service.request(include_text=False, include_selection=True).result(timeout=2)
    finally:
        service.shutdown()

    assert snapshot.text is not None and not snapshot.text.blocked
    assert platform.reads == [("notepad.exe", False, True)]


@pytest.fixture
def h(monkeypatch, _session_qt_application):
    settings = InMemorySettings({
        SettingsKey.RECORDING_TRIGGER_MODE: RecordingTriggerMode.PUSH_HOLD,
    })
    monkeypatch.setattr(command, "settings_manager", settings)
    monkeypatch.setattr(text_rewrite, "provider_ready", lambda _settings: True)
    monkeypatch.setattr(synthetic_keys, "wait_for_modifiers_released", lambda timeout_s=0.8: True)
    platform = FakePlatform(KEEPASS)
    service = _capture(platform)
    focus_context.set_service(service)
    recorder = SimpleNamespace(is_recording=False)
    controller = SimpleNamespace(
        recorder=recorder,
        status_update=Signal(),
        overlay_state_update=Signal(),
        stop_recording=lambda: setattr(recorder, "is_recording", False),
        cancel=lambda: setattr(recorder, "is_recording", False),
        ui_controller=FakeUI(),
    )
    controller.ui_controller.clipboard_selection = "Bank PIN 4321"
    runtime = FakeRuntime(controller, settings)
    controller.transcription_runtime = runtime
    commands = CommandRuntime(controller)
    yield SimpleNamespace(controller=controller, runtime=runtime, ui=controller.ui_controller,
                          commands=commands, platform=platform, settings=settings)
    commands.cleanup()
    service.shutdown()


@pytest.mark.parametrize("app_context", [True, False])
def test_command_mode_never_copies_from_an_excluded_app(h, app_context):
    h.settings.values[SettingsKey.APP_CONTEXT_ENABLED] = app_context

    h.commands.key_pressed(1.0)
    job = h.runtime._job
    h.commands.key_released(2.0)
    _settle(job.selection.done)

    assert h.ui.captures == []
    assert job.selection_text() == ""
    with pytest.raises(CommandRefused, match=f"^{EXCLUDED_MESSAGE}$"):
        h.commands.complete_recording("make it formal", job)
    assert h.runtime._transcript_cleanup.calls == []


def test_a_transform_in_an_excluded_app_is_abandoned_unread(h):
    h.commands.run_transform("polish")

    _settle(lambda: h.runtime.events[-1][0] == "abandon")
    assert h.runtime.events[-1] == ("abandon", EXCLUDED_MESSAGE)
    assert h.ui.captures == []


def test_a_password_field_is_not_copied_either(h):
    h.platform.current = NOTEPAD
    h.platform.contexts["notepad.exe"] = TextContext(blocked=True, source="uia")
    results = []

    h.commands._read_selection(h.commands._request_focus(), results.append)

    assert results == [UnreadSelection(PASSWORD_MESSAGE)]
    assert h.ui.captures == []


def test_an_unread_selection_reads_as_no_text():
    future: Future = Future()
    future.set_result(UnreadSelection(EXCLUDED_MESSAGE))

    assert DictationJob(mode=JobMode.COMMAND, selection=future).selection_text() == ""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UI Automation")
def test_ui_automation_marks_a_password_edit_as_refused():
    """Our own hidden ES_PASSWORD EDIT on its own message loop, never focused."""
    from ctypes import wintypes

    from services.focus_context._win_uia import UiaTextReader

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
        wintypes.HINSTANCE, wintypes.LPVOID]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                    wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.MsgWaitForMultipleObjects.argtypes = [
        wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, wintypes.DWORD]
    ready, stop = threading.Event(), threading.Event()
    window, results = {}, {}

    def pump():
        parent = user32.CreateWindowExW(0x80, "STATIC", "ow-test", 0x80000000,
                                        -32000, -32000, 400, 200, None, None, None, None)
        # WS_CHILD | WS_VISIBLE | ES_PASSWORD
        window["edit"] = user32.CreateWindowExW(0, "EDIT", "hunter2", 0x40000000 | 0x10000000 | 0x0020,
                                                0, 0, 400, 200, parent, None, None, None)
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

    def read():
        reader = UiaTextReader()
        try:
            results["context"] = reader._read_window(window["edit"], include_text=False,
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
        reading.join(10)
        assert not reading.is_alive(), "UI Automation did not answer"
    finally:
        stop.set()
        pumping.join(5)

    assert "error" not in results, results.get("error")
    assert results["context"] == TextContext(blocked=True, source="uia")
