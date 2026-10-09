"""Hotkeys page: new rows, conflicts, the hands-free switch, side-button capture."""

import os
import tempfile
import threading
import types
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from services import text_transforms
from services.hotkey_manager import format_hotkey_display
from services.settings import RecordingTriggerMode, SettingsKey, SettingsManager, SettingsView
from services.text_transforms import Transform
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import BASIC_DICTATION
from ui_qt.dialogs.settings_dialog import HOTKEYS
from ui_qt.widgets import hotkey_capture
from ui_qt.widgets.hotkey_capture import HotkeyCaptureInput
from ui_qt.widgets.profile_hotkey_input import ProfileHotkeyInput

NEW_ROWS = ("command_mode", "scratchpad_toggle", "cycle_language", "paste_last_original")


class _FakeCaptureThread:
    """Stands in for the global capture thread, which would hook the real keyboard."""

    def __init__(self, _parent=None):
        self.captured = MagicMock()
        self.failed = MagicMock()
        self.finished = MagicMock()
        self.stopped = False

    def start(self):
        pass

    def deleteLater(self):
        pass

    def isRunning(self):
        return not self.stopped

    def stop(self):
        self.stopped = True

    def wait(self, _timeout):
        return True


@pytest.fixture
def make_dialog():
    stacks = []
    temp = tempfile.TemporaryDirectory()

    def build(values=None, *, wayland=False):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({SettingsKey.SETTINGS_VIEW: SettingsView.ADVANCED, **(values or {})})
        stack = ExitStack()
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(patch.object(settings_dialog_module.history_manager, "set_retention"))
        for module in (models_module, downloads_module):
            stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        stack.enter_context(
            patch.object(settings_dialog_module, "HotkeyCaptureThread", _FakeCaptureThread)
        )
        stack.enter_context(
            patch.object(settings_dialog_module, "is_native_wayland_session", return_value=wayland)
        )
        stacks.append(stack)
        dialog = settings_dialog_module.SettingsDialog(background_cache_scan=False)
        dialog.on_profile_hotkey_capture = MagicMock()
        dialog.select_destination(HOTKEYS)
        return dialog, store

    yield build
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def _note(dialog, key):
    note = dialog.hotkey_row_notes[key]
    return note.text() if not note.isHidden() else ""


def _click_side_button(widget, button=Qt.MouseButton.BackButton, modifiers=Qt.KeyboardModifier.NoModifier):
    QTest.mouseClick(widget, button, modifiers, QPoint(8, 8))


def test_new_actions_are_optional_rows_with_their_own_clear(make_dialog):
    dialog, store = make_dialog({SettingsKey.HOTKEYS: {"scratchpad_toggle": "ctrl+alt+n"}})
    for key in NEW_ROWS:
        assert dialog.hotkey_inputs[key].property("optional") is True
    assert dialog.hotkey_inputs["scratchpad_toggle"].text() == format_hotkey_display("ctrl+alt+n")
    assert dialog.hotkey_clear_buttons["scratchpad_toggle"].isEnabled()
    assert not dialog.hotkey_clear_buttons["command_mode"].isEnabled()

    dialog.hotkey_clear_buttons["scratchpad_toggle"].click()
    assert store.load_hotkey_settings()["scratchpad_toggle"] == ""
    assert dialog.message_label.text() == "Scratchpad hotkey cleared."
    assert not dialog.hotkey_clear_buttons["scratchpad_toggle"].isEnabled()
    dialog.close()


def test_a_shortcut_another_action_uses_is_refused_on_its_row(make_dialog):
    dialog, store = make_dialog()
    cancel = dialog.current_hotkeys["cancel"]
    dialog.capture_thread = thread = object()
    dialog.capturing = "command_mode"
    dialog._on_hotkey_captured(thread, cancel)

    assert store.load_hotkey_settings().get("command_mode", "") == ""
    assert _note(dialog, "command_mode") == "That shortcut is already used by Cancel."
    assert dialog.hotkey_row_notes["command_mode"].property("tone") == "warning"
    assert dialog.message_label.text() == "That shortcut is already used by Cancel."

    dialog.capture_thread = thread
    dialog.capturing = "command_mode"
    dialog._on_hotkey_captured(thread, "ctrl+alt+k")
    assert store.load_hotkey_settings()["command_mode"] == "ctrl+alt+k"
    assert _note(dialog, "command_mode") == ""
    assert dialog.message_label.text() == "Command Mode hotkey updated."
    dialog.close()


def test_profiles_and_transforms_block_a_standard_shortcut(make_dialog, monkeypatch):
    profile = {"id": "ticket", "name": "Ticket", "instructions": "Ticket.", "hotkey": "ctrl+alt+t"}
    monkeypatch.setattr(
        text_transforms, "load_transforms",
        lambda _settings: [Transform("polish", "Polish", "Polish it.", "ctrl+alt+p")],
    )
    dialog, store = make_dialog({SettingsKey.TRANSCRIPT_CLEANUP_PROFILES: [profile]})
    dialog._on_local_hotkey_captured("cycle_language", "alt+ctrl+t")
    assert _note(dialog, "cycle_language") == "That shortcut is already used by Ticket."
    dialog._on_local_hotkey_captured("cycle_language", "ctrl+alt+p")
    assert _note(dialog, "cycle_language") == "That shortcut is already used by Polish."
    # Re-saving an action's own shortcut is not a conflict.
    record = dialog.current_hotkeys["record_toggle"]
    dialog._on_local_hotkey_captured("record_toggle", record)
    assert _note(dialog, "record_toggle") == ""
    assert store.load_hotkey_settings().get("cycle_language", "") == ""
    dialog.close()


def test_double_tap_switch_shows_for_push_and_hold_and_saves(make_dialog):
    dialog, store = make_dialog()
    dialog.show()
    QApplication.instance().processEvents()
    switch = dialog.hands_free_latch_switch
    assert not dialog.hands_free_latch_row.isVisible()
    assert switch.isChecked()

    dialog.record_mode_combo.setCurrentIndex(
        dialog.record_mode_combo.findData(RecordingTriggerMode.PUSH_HOLD)
    )
    assert dialog.hands_free_latch_row.isVisible()
    description = dialog.hotkey_row_descriptions["record_toggle"]
    assert "Double-tap to record hands-free" in description.text()

    switch.click()
    assert store.get(SettingsKey.RECORDING_HANDS_FREE_LATCH) is False
    assert "Quick taps are canceled" in description.text()

    dialog.record_mode_combo.setCurrentIndex(
        dialog.record_mode_combo.findData(RecordingTriggerMode.TOGGLE)
    )
    assert not dialog.hands_free_latch_row.isVisible()
    dialog.close()


def test_saved_latch_state_loads_with_the_page(make_dialog):
    dialog, _store = make_dialog({
        SettingsKey.RECORDING_TRIGGER_MODE: RecordingTriggerMode.PUSH_HOLD,
        SettingsKey.RECORDING_HANDS_FREE_LATCH: False,
    })
    assert not dialog.hands_free_latch_switch.isChecked()
    assert not dialog.hands_free_latch_row.isHidden()
    assert "Quick taps are canceled" in dialog.hotkey_row_descriptions["record_toggle"].text()
    dialog.close()


def test_basic_recording_shortcut_mentions_double_tap(make_dialog):
    dialog, _store = make_dialog({
        SettingsKey.SETTINGS_VIEW: SettingsView.BASIC,
        SettingsKey.RECORDING_TRIGGER_MODE: RecordingTriggerMode.PUSH_HOLD,
    })
    page = dialog._basic_pages[BASIC_DICTATION]
    page.refresh_shortcut()
    assert "double-tap" in page.shortcut_description.text()
    dialog.close()


def test_side_button_click_on_the_capturing_field_saves_a_mouse_shortcut(make_dialog):
    dialog, store = make_dialog()
    field = dialog.hotkey_inputs["paste_last_original"]
    dialog.show()
    QTest.mouseClick(field, Qt.MouseButton.LeftButton)
    assert dialog.capturing == "paste_last_original"
    dialog.on_profile_hotkey_capture.assert_called_with(True)
    thread = dialog.capture_thread

    # The Windows wording; X11 says nothing about a side button with a modifier.
    with patch.object(hotkey_capture, "mouse_shortcuts_supported", return_value=True), \
            patch.object(hotkey_capture, "USE_PYNPUT_BACKEND", False):
        _click_side_button(field, modifiers=Qt.KeyboardModifier.ShiftModifier)

        assert thread.stopped
        assert dialog.capturing is None
        dialog.on_profile_hotkey_capture.assert_called_with(False)
        assert store.load_hotkey_settings()["paste_last_original"] == "shift+mouse4"
        assert field.text() == format_hotkey_display("shift+mouse4")
        assert _note(dialog, "paste_last_original") == (
            "Mouse 4 won't work as Back in other apps while it's a shortcut."
        )
    dialog.close()


def test_side_button_outside_capture_changes_nothing(make_dialog):
    dialog, store = make_dialog()
    field = dialog.hotkey_inputs["command_mode"]
    _click_side_button(field, Qt.MouseButton.ForwardButton)
    assert dialog.capturing is None
    assert store.load_hotkey_settings().get("command_mode", "") == ""
    dialog.close()


def test_unsupported_desktop_explains_and_keeps_capturing(make_dialog):
    dialog, store = make_dialog()
    field = dialog.hotkey_inputs["command_mode"]
    QTest.mouseClick(field, Qt.MouseButton.LeftButton)
    with patch.object(hotkey_capture, "mouse_shortcuts_supported", return_value=False):
        _click_side_button(field)
    assert dialog.capturing == "command_mode"
    assert _note(dialog, "command_mode") == hotkey_capture.MOUSE_UNSUPPORTED_MESSAGE
    assert store.load_hotkey_settings().get("command_mode", "") == ""
    dialog._cancel_hotkey_capture()
    assert _note(dialog, "command_mode") == ""
    dialog.close()


def test_wayland_rows_capture_in_qt_and_explain_side_buttons(make_dialog):
    dialog, store = make_dialog(wayland=True)
    field = dialog.hotkey_inputs["scratchpad_toggle"]
    assert isinstance(field, ProfileHotkeyInput)
    field.begin_capture()
    with patch.object(hotkey_capture, "mouse_shortcuts_supported", return_value=False):
        _click_side_button(field)
    assert field._capturing
    assert _note(dialog, "scratchpad_toggle") == hotkey_capture.MOUSE_UNSUPPORTED_MESSAGE
    QTest.keyClick(field, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
    # Qt reports Command as the Control modifier on macOS.
    primary = "cmd" if hotkey_capture.sys.platform == "darwin" else "ctrl"
    assert store.load_hotkey_settings()["scratchpad_toggle"] == f"{primary}+alt+n"
    dialog.close()


# Capture widgets


def test_capture_field_turns_side_buttons_into_shortcuts_only_while_capturing(_session_qt_application):
    field = HotkeyCaptureInput()
    requested, captured, notices = [], [], []
    field.capture_requested.connect(lambda: requested.append(True))
    field.mouse_captured.connect(captured.append)
    field.notice.connect(notices.append)

    with patch.object(hotkey_capture, "mouse_shortcuts_supported", return_value=True):
        _click_side_button(field)
        assert (requested, captured) == ([], [])
        field.set_capturing(True)
        _click_side_button(field, Qt.MouseButton.ForwardButton, Qt.KeyboardModifier.ControlModifier)
        QTest.mouseClick(field, Qt.MouseButton.LeftButton)
    assert captured == [f"{'cmd' if hotkey_capture.sys.platform == 'darwin' else 'ctrl'}+mouse5"]
    assert requested == [True]
    assert notices == []


def test_profile_field_captures_a_side_button_and_stops(_session_qt_application):
    field = ProfileHotkeyInput()
    captured, changes = [], []
    field.captured.connect(captured.append)
    field.capture_changed.connect(changes.append)
    field.begin_capture()
    with patch.object(hotkey_capture, "mouse_shortcuts_supported", return_value=True):
        _click_side_button(field)
    assert captured == ["mouse4"]
    assert field.hotkey == "mouse4"
    assert changes == [True, False]


def test_side_button_notes_depend_on_the_desktop():
    with patch.object(hotkey_capture, "USE_PYNPUT_BACKEND", False):
        assert "won't work as Forward" in hotkey_capture.mouse_shortcut_note("mouse5")
        assert hotkey_capture.mouse_shortcut_note("ctrl+r") == ""
    with patch.object(hotkey_capture, "USE_PYNPUT_BACKEND", True):
        assert hotkey_capture.mouse_shortcut_note("mouse4") == (
            "Mouse 4 also still goes Back in the app you're using."
        )
        assert hotkey_capture.mouse_shortcut_note("alt+mouse4") == ""


@pytest.mark.skipif(hotkey_capture.USE_PYNPUT_BACKEND, reason="Windows capture thread")
def test_windows_capture_removes_only_its_own_hook(_session_qt_application):
    hooked = threading.Event()
    calls = {"hook": [], "unhook": [], "unhook_all": 0}

    def hook(callback, suppress):
        calls["hook"].append((callback, suppress))
        hooked.set()
        return "capture-hook"

    def unhook_all():
        calls["unhook_all"] += 1

    stub = types.SimpleNamespace(
        KEY_UP="up", hook=hook, unhook=calls["unhook"].append, unhook_all=unhook_all,
        get_hotkey_name=lambda names: "windows+f9",
    )
    with patch.object(hotkey_capture, "keyboard", stub):
        thread = hotkey_capture.HotkeyCaptureThread()
        thread.start()
        assert hooked.wait(2)
        thread.stop()
        assert thread.wait(2000)
        assert calls["unhook"] == ["capture-hook"]
        assert calls["unhook_all"] == 0
        assert calls["hook"][0][1] is False

        captured = []
        hooked.clear()
        thread = hotkey_capture.HotkeyCaptureThread()
        thread.captured.connect(captured.append, Qt.ConnectionType.DirectConnection)
        thread.start()
        assert hooked.wait(2)
        callback = calls["hook"][-1][0]
        callback(types.SimpleNamespace(event_type="down", name="windows", is_keypad=False))
        callback(types.SimpleNamespace(event_type="down", name="f9", is_keypad=False))
        callback(types.SimpleNamespace(event_type="up", name="f9", is_keypad=False))
        assert thread.wait(2000)
    assert captured == ["win+f9"]
    assert calls["unhook_all"] == 0


# Fit: classic and Omarchy, narrow windows, large fonts


@pytest.mark.parametrize("ui_mode,width", [("classic", 720), ("omarchy", 720), ("omarchy", 460)])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_hotkey_rows_fit_narrow_windows_and_large_fonts(make_dialog, monkeypatch, ui_mode, width, theme):
    from PyQt6.QtWidgets import QAbstractButton, QLabel, QLineEdit
    from ui_qt.widgets.hotkey_row import ReflowCard
    from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
    from ui_qt.utils.palette import current_palette, set_current_palette
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    # The Windows backend, whose side-button note is the longest; X11 shows
    # none for a side button with modifiers.
    monkeypatch.setattr(hotkey_capture, "USE_PYNPUT_BACKEND", False)
    app = QApplication.instance()
    previous_style, previous_font = app.styleSheet(), app.font()
    previous_scale, previous_palette = current_ui_font_scale_percent(), current_palette()
    manager = ThemeManager(theme)
    dialog = None
    try:
        apply_ui_font_scale(130, app=app, theme_manager=manager)
        dialog, _store = make_dialog({
            SettingsKey.RECORDING_TRIGGER_MODE: RecordingTriggerMode.PUSH_HOLD,
            SettingsKey.HOTKEYS: {"command_mode": "ctrl+alt+shift+mouse4"},
        })
        dialog.show()
        dialog._fit_to_screen()
        dialog.resize(width, 600)
        for _ in range(10):
            app.processEvents()
        page = dialog._pages[HOTKEYS]
        assert dialog.hands_free_latch_row.isVisible()
        assert dialog.hotkey_row_notes["command_mode"].isVisible()
        # Below the classic minimum width only the Advanced rail is too wide;
        # the cards themselves must still hold their controls.
        cards = [card for card in page.findChildren(ReflowCard) if card.isVisible()]
        assert len(cards) == 10
        for card in cards:
            controls = card.findChildren(QAbstractButton) + card.findChildren(QLineEdit)
            rects = sorted(
                (start.x(), start.y(), end.x(), end.y())
                for control in controls if control.isVisible()
                for start, end in [(
                    control.mapTo(card, control.rect().topLeft()),
                    control.mapTo(card, control.rect().bottomRight()),
                )]
            )
            for left, _top, right, _bottom in rects:
                assert left >= 0 and right < card.width()
            for first, second in zip(rects, rects[1:]):
                if first[1] <= second[3] and second[1] <= first[3]:
                    assert first[2] < second[0]
        if width > 460:
            for control in page.findChildren(QAbstractButton) + page.findChildren(QLineEdit):
                if control.isVisible():
                    assert control.mapTo(page, control.rect().bottomRight()).x() < page.width(), (
                        control.objectName() or control.text())
        for label in page.findChildren(QLabel):
            if label.isVisible() and label.wordWrap():
                assert label.height() >= label.heightForWidth(label.width())
    finally:
        if dialog is not None:
            dialog.close()
        apply_ui_font_scale(previous_scale, app=app)
        set_current_palette(previous_palette)
        app.setFont(previous_font)
        app.setStyleSheet(previous_style)
