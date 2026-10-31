"""Settings → Hotkeys lists every shortcut and changes each one in place.

The standard shortcuts, plus the ones you give profiles, transforms and
actions. Their own pages still change them too, and every page agrees.
"""

import os
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QLabel, QMessageBox, QPushButton

from config import config
from services.actions.model import Action, ActionKind
from services.cleanup_profiles import load_cleanup_profiles
from services.hotkey_conflicts import STANDARD_LABELS
from services.hotkey_manager import format_hotkey_display
from services.settings import SettingsKey, SettingsManager
from services.text_transforms import Transform, load_transforms, save_transform
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_dialog import (
    CLEANUP_PROFILES,
    COMMANDS,
    HOTKEYS,
    WALKTHROUGHS,
)
from ui_qt.widgets.hotkey_row import ReflowCard
from ui_qt.widgets.profile_hotkey_input import ProfileHotkeyInput


class _FakeCaptureThread:
    """Stands in for the global capture thread, which would hook the real keyboard."""

    def __init__(self, _parent=None):
        self.captured = MagicMock()
        self.cancelled = MagicMock()
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

    def build(values=None):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({**(values or {})})
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
            patch.object(settings_dialog_module, "is_native_wayland_session", return_value=False)
        )
        stacks.append(stack)
        dialog = settings_dialog_module.SettingsDialog(background_cache_scan=False)
        dialog.on_profile_hotkey_capture = MagicMock()
        dialog.on_cleanup_profiles_changed = MagicMock()
        dialog.on_settings_changed = MagicMock()
        dialog.select_destination(HOTKEYS)
        return dialog, store

    yield build
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def _field(dialog, name: str) -> ProfileHotkeyInput:
    """The Hotkeys page's box for the profile, transform or action called ``name``."""
    fields = [
        field for field in dialog.hotkey_bindings.findChildren(ProfileHotkeyInput)
        if field.accessibleName() == f"{name} shortcut"
    ]
    assert len(fields) == 1, name
    return fields[0]


def _card(widget) -> ReflowCard:
    while not isinstance(widget, ReflowCard):
        widget = widget.parentWidget()
    return widget


def _note(field) -> str:
    note = _card(field).findChild(QLabel, "hotkeyShortcutNote")
    return note.text() if not note.isHidden() else ""


def _clear(field) -> QPushButton:
    return _card(field).findChild(QPushButton, "hotkeyClearButton")


def _transform(store, name: str) -> Transform:
    return next(t for t in load_transforms(store.load_all_settings()) if t.name == name)


def test_every_standard_shortcut_has_a_row_on_hotkeys(make_dialog):
    dialog, _store = make_dialog()
    assert set(dialog.hotkey_inputs) == set(config.DEFAULT_HOTKEYS) == set(STANDARD_LABELS)
    page = dialog._pages[HOTKEYS]
    for field in dialog.hotkey_inputs.values():
        assert page.isAncestorOf(field)
    dialog.close()


def test_profiles_transforms_and_actions_are_listed_with_their_shortcuts(make_dialog):
    action = Action("ask", "Ask the docs", ActionKind.OPEN_LINK,
                    params={"template": "https://example.com/?q={text}"},
                    hotkey="ctrl+alt+d")
    dialog, store = make_dialog({SettingsKey.ACTIONS: [action.to_dict()]})
    settings = store.load_all_settings()

    for profile in load_cleanup_profiles(settings):
        assert _field(dialog, profile.name).text() == format_hotkey_display(profile.hotkey)
    for transform in load_transforms(settings):
        assert _field(dialog, transform.name).text() == format_hotkey_display(transform.hotkey)
    field = _field(dialog, "Ask the docs")
    assert field.text() == format_hotkey_display("ctrl+alt+d")
    assert _clear(field).isEnabled()
    dialog.close()


def test_a_transform_shortcut_set_on_hotkeys_saves_and_registers(make_dialog):
    dialog, store = make_dialog()
    dialog.show()
    field = _field(dialog, "Polish")
    QTest.mouseClick(field, Qt.MouseButton.LeftButton)
    assert field._capturing
    dialog.on_profile_hotkey_capture.assert_called_with(True)

    QTest.keyClick(field, Qt.Key.Key_P,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)

    # Qt reports Command as the Control modifier on macOS.
    primary = "cmd" if settings_dialog_module.sys.platform == "darwin" else "ctrl"
    assert _transform(store, "Polish").hotkey == f"{primary}+alt+p"
    dialog.on_profile_hotkey_capture.assert_called_with(False)
    dialog.on_settings_changed.assert_called_with("transforms")
    assert dialog.message_label.text() == (
        f"Polish shortcut set to {format_hotkey_display(f'{primary}+alt+p')}."
    )
    assert _clear(field).isEnabled()
    dialog.close()


def test_a_profile_shortcut_set_on_hotkeys_registers_like_one_saved_on_profiles(make_dialog):
    dialog, store = make_dialog()
    _field(dialog, "Email").captured.emit("ctrl+alt+e")

    profile = next(p for p in load_cleanup_profiles(store.load_all_settings()) if p.name == "Email")
    assert profile.hotkey == "ctrl+alt+e"
    dialog.on_cleanup_profiles_changed.assert_called_once()
    dialog.close()


def test_a_taken_shortcut_is_refused_with_its_editors_reason(make_dialog):
    dialog, store = make_dialog()
    cancel = dialog.current_hotkeys["cancel"]
    field = _field(dialog, "Polish")

    field.captured.emit(cancel)

    assert _transform(store, "Polish").hotkey == ""
    assert field.text() == ""
    assert _note(field) == "That shortcut is already used by Cancel. Choose another."
    dialog.on_settings_changed.assert_not_called()
    dialog.close()


def test_clear_removes_an_action_shortcut(make_dialog):
    action = Action("ask", "Ask the docs", ActionKind.OPEN_LINK,
                    params={"template": "https://example.com/?q={text}"},
                    hotkey="ctrl+alt+d")
    dialog, store = make_dialog({SettingsKey.ACTIONS: [action.to_dict()]})
    field = _field(dialog, "Ask the docs")

    _clear(field).click()

    assert store.load_all_settings()[SettingsKey.ACTIONS][0]["hotkey"] == ""
    assert field.text() == ""
    assert not _clear(field).isEnabled()
    dialog.on_settings_changed.assert_called_with("actions")
    assert dialog.message_label.text() == "Ask the docs shortcut cleared."
    dialog.close()


def test_rows_say_when_a_shortcut_does_nothing(make_dialog):
    record = config.DEFAULT_HOTKEYS["record_toggle"]
    off = Action("off", "Paused action", ActionKind.OPEN_LINK,
                 params={"template": "https://example.com/?q={text}"},
                 hotkey="ctrl+alt+o", enabled=False)
    dialog, store = make_dialog({SettingsKey.ACTIONS: [off.to_dict()]})
    # A hand-edited file can hold a shortcut the app then skips.
    store.mutate_settings(lambda settings: settings.update(
        {SettingsKey.TEXT_TRANSFORMS: [
            {"id": "polish", "name": "Polish", "instruction": "Polish it.", "hotkey": record},
        ]}
    ))
    dialog.refresh()

    assert _note(_field(dialog, "Polish")) == (
        "Recording uses this shortcut too, so it does nothing here."
    )
    assert _note(_field(dialog, "Paused action")) == (
        "This action is turned off, so its shortcut does nothing."
    )
    dialog.close()


def test_a_shortcut_changed_on_its_own_page_shows_on_hotkeys(make_dialog):
    dialog, store = make_dialog()
    dialog.show()
    dialog.select_destination(COMMANDS)
    polish = _transform(store, "Polish")
    store.mutate_settings(
        lambda settings: save_transform(settings, Transform(
            polish.id, polish.name, polish.instruction, "ctrl+alt+j"))
    )

    dialog.select_destination(HOTKEYS)

    assert _field(dialog, "Polish").text() == format_hotkey_display("ctrl+alt+j")
    dialog.close()


def test_a_profile_draft_takes_the_shortcut_set_on_hotkeys(make_dialog):
    dialog, store = make_dialog()
    dialog.show()
    dialog.select_destination(CLEANUP_PROFILES)
    panel = dialog.cleanup_profiles_panel
    assert panel.name_edit.text() == "Support ticket"
    panel.instructions_edit.setPlainText("A short ticket.")

    dialog.select_destination(HOTKEYS)
    _field(dialog, "Support ticket").captured.emit("ctrl+alt+t")
    dialog.select_destination(CLEANUP_PROFILES)

    # The unsaved instructions stay, and saving them keeps the new shortcut.
    assert panel.instructions_edit.toPlainText() == "A short ticket."
    assert panel.hotkey_input.hotkey == "ctrl+alt+t"
    assert panel.save_profile()
    profile = next(p for p in load_cleanup_profiles(store.load_all_settings())
                   if p.name == "Support ticket")
    assert (profile.instructions, profile.hotkey) == ("A short ticket.", "ctrl+alt+t")
    dialog.close()


def test_walkthrough_shortcuts_agree_on_both_pages(make_dialog):
    dialog, store = make_dialog()
    dialog.show()
    dialog.select_destination(WALKTHROUGHS)
    on_walkthroughs = dialog.shortcut_input("walkthrough", WALKTHROUGHS)
    QTest.mouseClick(on_walkthroughs, Qt.MouseButton.LeftButton)
    assert dialog.capturing == "walkthrough"

    dialog._on_hotkey_captured(dialog.capture_thread, "ctrl+alt+w")

    assert store.load_hotkey_settings()["walkthrough"] == "ctrl+alt+w"
    shown = format_hotkey_display("ctrl+alt+w")
    assert on_walkthroughs.text() == shown
    assert dialog.hotkey_inputs["walkthrough"].text() == shown

    dialog.select_destination(HOTKEYS)
    dialog.hotkey_clear_buttons["walkthrough"].click()
    assert store.load_hotkey_settings()["walkthrough"] == ""
    assert on_walkthroughs.text() == ""
    dialog.close()


def test_a_binding_capture_stops_a_standard_one(make_dialog):
    dialog, _store = make_dialog()
    dialog.show()
    QTest.mouseClick(dialog.hotkey_inputs["cancel"], Qt.MouseButton.LeftButton)
    thread = dialog.capture_thread
    assert dialog.capturing == "cancel"

    _field(dialog, "Polish").begin_capture()

    assert dialog.capturing is None
    assert thread.stopped
    dialog.close()


def test_reset_leaves_your_own_shortcuts_alone(make_dialog):
    dialog, store = make_dialog({SettingsKey.HOTKEYS: {"scratchpad_toggle": "ctrl+alt+n"}})
    _field(dialog, "Polish").captured.emit("ctrl+alt+j")
    reset = dialog.findChild(QPushButton, "hotkeyResetButton")

    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        reset.click()

    assert store.load_hotkey_settings()["scratchpad_toggle"] == ""
    assert _transform(store, "Polish").hotkey == "ctrl+alt+j"
    dialog.close()


def test_each_group_opens_its_own_page(make_dialog):
    dialog, _store = make_dialog()
    links = {
        button.text(): button
        for button in dialog.hotkey_bindings.findChildren(QPushButton, "settingsPageLink")
    }

    links["Open Commands"].click()

    assert dialog.rail.current_key() == COMMANDS
    dialog.close()
