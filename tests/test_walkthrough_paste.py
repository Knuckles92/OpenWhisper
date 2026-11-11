"""Dedicated walkthrough paste: ordering, cancellation and hotkey isolation."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.focus_context import AppIdentity
from services.runtime.hotkeys import HotkeyRuntime
from services.walkthrough.session import WalkthroughResult
from tests.test_clipboard_files import FakeClipboard
from tests.test_personalize_s6_backends import _windows_manager
from tests.test_push_hold_recording import _key_event
from ui_qt.clipboard import TemporaryClipboard
from ui_qt.walkthrough_paste import WalkthroughPaste


@pytest.fixture
def paste_case(tmp_path, _session_qt_application, monkeypatch):
    from services import synthetic_keys
    monkeypatch.setattr(synthetic_keys, "modifiers_held", lambda: False)
    folder = tmp_path / "walkthrough with spaces"
    folder.mkdir()
    pdf = folder / "write-up.pdf"
    pdf.write_bytes(b"test file")
    result = WalkthroughResult(folder, pdf, folder / "write-up.md", "body", "", None, 0, 1, None)
    target = AppIdentity("chat", "Chat", pid=42, window="window-1")
    clipboard = FakeClipboard()
    sent, reports = [], []

    def send():
        mime = clipboard.mimeData()
        sent.append(([Path(u.toLocalFile()) for u in mime.urls()],
                     mime.text() if mime.hasFormat("text/plain") else ""))

    identity = Mock(return_value=target)
    paste = WalkthroughPaste(clipboard, TemporaryClipboard(clipboard),
                             identity=identity, send_paste=send, report=reports.append)
    yield SimpleNamespace(paste=paste, result=result, target=target, identity=identity,
                          clipboard=clipboard, sent=sent, reports=reports)
    paste.cancel()


def test_pdf_then_plain_instructions_without_a_second_user_action(paste_case):
    c = paste_case
    c.paste.start(c.result, c.target)
    assert c.paste.busy and c.paste.timer.isActive()
    assert c.sent == [([c.result.pdf], "")]
    c.paste.timer.timeout.emit()
    assert len(c.sent) == 2
    files, instructions = c.sent[1]
    assert files == []
    assert "carry out the tasks and changes" in instructions
    assert str(c.result.pdf.resolve()) in instructions
    assert not c.paste.busy
    assert "review them before sending" in c.reports[-1]


@pytest.mark.parametrize("shortcut", [False, True])
def test_ui_auto_paste_and_shortcut_share_two_part_delivery(paste_case, monkeypatch, shortcut):
    from ui_qt.ui_controller import UIController

    c = paste_case
    ui = SimpleNamespace(_walkthrough_paste=c.paste, _walkthrough_ready=Mock(),
                         set_status=c.reports.append)
    ui.paste_walkthrough = lambda result, target: UIController.paste_walkthrough(ui, result, target)
    monkeypatch.setattr("ui_qt.walkthrough_paste.latest_result", lambda: c.result)
    if shortcut:
        UIController.paste_latest_walkthrough(ui, c.target)
    else:
        ui.paste_walkthrough(c.result, c.target)
    assert c.sent == [([c.result.pdf], "")]
    assert c.reports[-1] == "Pasting PDF, then instructions…"
    c.paste.timer.timeout.emit()
    assert len(c.sent) == 2 and c.sent[1][0] == []
    assert "carry out the tasks and changes" in c.sent[1][1]
    ui._walkthrough_ready.show_paste_status.assert_called_with(c.result, c.reports[-1])


def test_paste_requires_accessibility_permission(paste_case, monkeypatch):
    monkeypatch.setattr("services.hotkey_manager.is_accessibility_trusted", lambda: False)
    c = paste_case
    c.paste.start(c.result, c.target)
    assert c.sent == [] and not c.paste.busy
    assert "Accessibility" in c.reports[-1]


@pytest.mark.parametrize("change", ["window", "clipboard", "modifiers", "cancel"])
def test_interrupted_paste_never_sends_the_instructions(paste_case, monkeypatch, change):
    c = paste_case
    c.paste.start(c.result, c.target)
    if change == "window":
        c.identity.return_value = replace(c.target, window="another-window")
    elif change == "clipboard":
        c.clipboard.setText("new user copy")
    elif change == "modifiers":
        monkeypatch.setattr("services.synthetic_keys.modifiers_held", lambda: True)
    else:
        c.paste.cancel()
    c.paste.timer.timeout.emit()
    assert len(c.sent) == 1
    assert not c.paste.busy
    if change == "clipboard":
        assert c.clipboard.text() == "new user copy"


def test_repeated_shortcut_does_not_duplicate_pending_pastes(paste_case):
    c = paste_case
    c.paste.start(c.result, c.target)
    c.paste.start(c.result, c.target)
    assert len(c.sent) == 1
    c.paste.timer.timeout.emit()
    assert len(c.sent) == 2


@pytest.mark.parametrize("problem", ["missing_pdf", "focus", "unknown", "self", "write"])
def test_preflight_failure_sends_nothing(paste_case, problem):
    c = paste_case
    if problem == "missing_pdf":
        c.result.pdf.unlink()
    elif problem == "focus":
        c.identity.return_value = replace(c.target, window="other")
    elif problem == "unknown":
        c.target = None
    elif problem == "self":
        c.target = replace(c.target, is_self=True)
        c.identity.return_value = c.target
    else:
        c.clipboard.fail_writes = True
    c.paste.start(c.result, c.target)
    assert c.sent == [] and c.reports
    assert not c.paste.busy


def test_second_write_failure_does_not_repeat_pdf(paste_case):
    c = paste_case
    c.paste.start(c.result, c.target)
    c.clipboard.fail_writes = True
    c.paste.timer.timeout.emit()
    assert len(c.sent) == 1
    assert "couldn't paste instructions" in c.reports[-1]
    assert not c.paste.busy


def test_shortcut_dispatch_leaves_normal_ctrl_v_untouched(monkeypatch):
    held = {"ctrl"}
    module, manager = _windows_manager(held=held)
    manager.hotkeys["paste_walkthrough"] = "ctrl+shift+f12"
    callback = Mock()
    manager.set_callbacks(on_paste_walkthrough=callback)
    monkeypatch.setattr(module, "_spawn", lambda fn, *args: fn(*args))
    assert manager._handle_keyboard_event(_key_event("down", "v", keypad=False)) is True
    assert manager._handle_keyboard_event(_key_event("up", "v", keypad=False)) is True
    callback.assert_not_called()
    held.add("shift")
    assert manager._handle_keyboard_event(_key_event("down", "f12", keypad=False)) is False
    assert manager._handle_keyboard_event(_key_event("up", "f12", keypad=False)) is False
    callback.assert_called_once()


def test_virtual_key_shortcut_uses_os_modifiers(monkeypatch):
    module, manager = _windows_manager(held=set(), paste_walkthrough="ctrl+shift+f12")
    monkeypatch.setattr("services.synthetic_keys.native_windows_modifiers",
                        lambda: frozenset({"ctrl", "shift"}))
    callback = Mock()
    manager.set_callbacks(on_paste_walkthrough=callback)
    monkeypatch.setattr(module, "_spawn", lambda fn, *args: fn(*args))
    event = _key_event("down", "f12", keypad=False)
    event.scan_code = -123
    assert manager._handle_keyboard_event(event) is False
    callback.assert_called_once()


def test_virtual_modifiers_are_waited_for_even_when_the_hook_misses_them(monkeypatch):
    from services import synthetic_keys

    monkeypatch.setattr(synthetic_keys, "_physical_key_reader", lambda: lambda vk: vk == 0x11)
    assert synthetic_keys._windows_modifiers_held()


@pytest.mark.parametrize("released", [True, False])
def test_runtime_captures_target_and_waits_for_shortcut_release(monkeypatch, released):
    target = AppIdentity("chat", "Chat", pid=42, window="1")
    service = SimpleNamespace(current_identity=lambda: target)
    monkeypatch.setattr("services.focus_context.get_service", lambda: service)
    monkeypatch.setattr("services.synthetic_keys.wait_for_modifiers_released", lambda: released)
    controller = SimpleNamespace(status_update=Mock())
    runtime = HotkeyRuntime(controller)
    runtime.paste_walkthrough_requested = Mock()
    runtime._paste_walkthrough_after_keys_up()
    if released:
        runtime.paste_walkthrough_requested.emit.assert_called_once_with(target)
    else:
        runtime.paste_walkthrough_requested.emit.assert_not_called()
        controller.status_update.emit.assert_called_once()


def test_latest_completed_capture_skips_newer_drafts(tmp_path, monkeypatch):
    from ui_qt import walkthrough_paste

    draft, complete = tmp_path / "2026-11-06_23-30-00", tmp_path / "2026-11-06_23-29-05"
    draft.mkdir()
    complete.mkdir()
    (draft / "draft.json").write_text("{}")
    (complete / "walkthrough.json").write_text("{}")
    monkeypatch.setattr(walkthrough_paste.storage, "captures_root", lambda: tmp_path)
    load = Mock(return_value="completed result")
    monkeypatch.setattr(walkthrough_paste.library, "load_result", load)
    assert walkthrough_paste.latest_result() == "completed result"
    load.assert_called_once_with(complete)
