"""Profile persistence, editing, selection, and global shortcut behavior."""

import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QMessageBox

from config import config
from services.cleanup_profiles import (
    STARTER_PROFILES,
    CleanupProfile,
    compose_profile_prompt,
    delete_cleanup_profile,
    load_cleanup_profiles,
    profile_hotkey_conflict,
    save_cleanup_profile,
)
from services.settings import SettingsKey, SettingsManager, settings_manager
from ui_qt.dialogs.settings_dialog import CLEANUP_PROFILES, SettingsDialog
from ui_qt.ui_controller import UIController
from ui_qt.widgets import TabbedContentWidget
from ui_qt.widgets.cleanup_profiles_panel import CleanupProfilesPanel
from ui_qt.widgets.profile_hotkey_input import ProfileHotkeyInput
from ui_qt.widgets.quick_record_tab import QuickRecordTab


def test_starters_do_not_reappear_after_deleting_library():
    settings = {"unrelated": "preserve"}
    assert [p.name for p in load_cleanup_profiles(settings)] == [
        "Support ticket",
        "Email",
    ]
    for profile in STARTER_PROFILES:
        delete_cleanup_profile(settings, profile.id)
    assert load_cleanup_profiles(settings) == []
    assert settings["unrelated"] == "preserve"


def test_profile_round_trip_and_delete_selected_profile(tmp_path):
    manager = SettingsManager(str(tmp_path / "profiles.json"))
    profile = CleanupProfile(
        "custom", "Release notes", "Group changes into bullets.", "ctrl+shift+f9", False
    )
    manager.mutate_settings(lambda s: save_cleanup_profile(s, profile))
    manager.save_setting(SettingsKey.QUICK_RECORD_PROFILE, profile.id)
    reloaded = SettingsManager(manager.settings_file)
    assert load_cleanup_profiles(reloaded.load_all_settings())[-1] == profile
    reloaded.mutate_settings(lambda s: delete_cleanup_profile(s, profile.id))
    assert reloaded.get(SettingsKey.QUICK_RECORD_PROFILE) == ""
    assert all(
        p.id != profile.id for p in load_cleanup_profiles(reloaded.load_all_settings())
    )


@pytest.mark.parametrize(
    "name,instructions,hotkey,reason",
    [
        ("", "Make bullets", "", "name"),
        ("Custom", " ", "", "instructions"),
        ("EMAIL", "Make bullets", "", "name already"),
        (
            "Custom",
            "Make bullets",
            config.DEFAULT_HOTKEYS["record_toggle"],
            "already used",
        ),
    ],
)
def test_invalid_profile_never_changes_settings(name, instructions, hotkey, reason):
    settings = {}
    with pytest.raises(ValueError, match=reason):
        save_cleanup_profile(
            settings, CleanupProfile("new", name, instructions, hotkey)
        )
    assert settings == {}


def test_hotkey_conflicts_include_other_profiles_and_modifier_aliases():
    settings = {}
    save_cleanup_profile(settings, replace(STARTER_PROFILES[0], hotkey="ctrl+alt+t"))
    assert profile_hotkey_conflict("alt+control+t", settings) == "Support ticket"
    assert not profile_hotkey_conflict(
        "ctrl+alt+t", settings, exclude_id="support-ticket"
    )
    with pytest.raises(ValueError, match="Support ticket"):
        save_cleanup_profile(
            settings, replace(STARTER_PROFILES[1], hotkey="ctrl+alt+t")
        )


def test_malformed_profiles_are_skipped_without_crashing():
    settings = {
        SettingsKey.TRANSCRIPT_CLEANUP_PROFILES: [
            None,
            "bad",
            {},
            {"id": 4, "name": "Bad", "instructions": "Bad"},
            {"id": "ok", "name": " Good ", "instructions": " Do this ", "hotkey": 9},
            {"id": "ok", "name": "Duplicate", "instructions": "Skip"},
        ]
    }
    assert load_cleanup_profiles(settings) == [CleanupProfile("ok", "Good", "Do this")]


def test_profile_prompt_uses_format_and_optional_shared_rules():
    profile = STARTER_PROFILES[1]
    prompt = compose_profile_prompt(profile, ["Spell Acme correctly."])
    assert profile.instructions in prompt
    assert "Do not invent" in prompt
    assert "Spell Acme correctly." in prompt
    assert "Spell Acme correctly." not in compose_profile_prompt(
        replace(profile, use_learned_rules=False), ["Spell Acme correctly."]
    )


def test_editor_save_duplicate_and_delete_update_library():
    panel = CleanupProfilesPanel()
    changed = Mock()
    panel.profiles_changed.connect(changed)
    panel.new_profile()
    panel.name_edit.setText("Release notes")
    panel.instructions_edit.setPlainText("Make concise bullets.")
    panel.hotkey_input.set_hotkey("ctrl+alt+f9")
    assert panel.save_profile()
    original = load_cleanup_profiles(settings_manager.load_all_settings())[-1]
    panel.duplicate_profile()
    assert panel.hotkey_input.hotkey == ""
    assert panel.save_profile()
    profiles = load_cleanup_profiles(settings_manager.load_all_settings())
    assert profiles[-1].id != original.id
    assert profiles[-1].name == "Release notes copy"
    with patch.object(
        QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
    ):
        panel.delete_profile()
    assert load_cleanup_profiles(settings_manager.load_all_settings())[-1] == original
    assert changed.call_count == 3


def test_editor_reports_conflict_and_keeps_draft():
    panel = CleanupProfilesPanel()
    panel.name_edit.setText("Ticket")
    panel.hotkey_input.set_hotkey(config.DEFAULT_HOTKEYS["cancel"])
    assert not panel.save_profile()
    assert "already used" in panel.message.text()
    assert panel.name_edit.text() == "Ticket"
    assert (
        load_cleanup_profiles(settings_manager.load_all_settings())[0].name
        == "Support ticket"
    )


def test_save_while_switching_keeps_list_and_editor_on_destination():
    panel = CleanupProfilesPanel()
    panel.name_edit.setText("Technical ticket")
    with patch.object(
        QMessageBox, "question", return_value=QMessageBox.StandardButton.Save
    ):
        panel.profile_list.setCurrentRow(1)
    assert panel.name_edit.text() == "Email"
    assert panel.profile_list.currentRow() == 1
    assert (
        load_cleanup_profiles(settings_manager.load_all_settings())[0].name
        == "Technical ticket"
    )


def test_cancel_switch_and_refresh_keep_unsaved_work():
    panel = CleanupProfilesPanel()
    panel.name_edit.setText("Unfinished ticket")
    with patch.object(
        QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel
    ):
        panel.profile_list.setCurrentRow(1)
    panel.refresh()
    assert panel.name_edit.text() == "Unfinished ticket"
    assert panel.profile_list.currentRow() == 0


def test_standard_hotkey_editor_rejects_profile_collision():
    settings_manager.mutate_settings(
        lambda s: save_cleanup_profile(
            s, replace(STARTER_PROFILES[0], hotkey="ctrl+alt+t")
        )
    )
    dialog = SettingsDialog()
    hotkeys = {**dialog.current_hotkeys, "record_toggle": "ctrl+alt+t"}
    assert not dialog._apply_hotkey_settings(hotkeys, "Updated")
    assert "Support ticket" in dialog.message_label.text()
    assert settings_manager.load_hotkey_settings()["record_toggle"] != "ctrl+alt+t"
    dialog.select_destination(CLEANUP_PROFILES)
    assert dialog.stack.currentWidget().widget() is dialog._pages[CLEANUP_PROFILES]


def test_quick_record_selection_persists_without_changing_standard_cleanup():
    settings_manager.save_setting(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, False)
    tab = QuickRecordTab()
    assert tab.selected_cleanup_profile_id() == ""
    tab.profile_combo.setCurrentIndex(tab.profile_combo.findData("email"))
    assert tab.cleanup_check.isChecked()
    assert not tab.cleanup_check.isEnabled()
    assert not settings_manager.get(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED)
    second = QuickRecordTab()
    assert second.selected_cleanup_profile_id() == "email"
    second.is_recording = True
    second._update_recording_state()
    assert not second.profile_combo.isEnabled()
    second.is_recording = False
    second._update_recording_state()
    second.profile_combo.setCurrentIndex(0)
    assert second.cleanup_check.isEnabled()
    assert not second.cleanup_check.isChecked()


def test_deleting_selected_profile_refreshes_quick_record_to_standard():
    tab = QuickRecordTab()
    tab.profile_combo.setCurrentIndex(tab.profile_combo.findData("email"))
    settings_manager.mutate_settings(lambda s: delete_cleanup_profile(s, "email"))
    tab.refresh_cleanup_profiles()
    assert tab.selected_cleanup_profile_id() == ""


def test_hotkey_profile_is_shown_during_capture_without_changing_next_selection():
    tab = QuickRecordTab()
    tab.set_recording_profile(STARTER_PROFILES[1])
    tab.is_recording = True
    tab._update_recording_state()
    assert tab.selected_cleanup_profile_id() == "email"
    assert tab.profile_hint.text() == "Recording with Email"
    settings_manager.mutate_settings(lambda s: delete_cleanup_profile(s, "email"))
    tab.refresh_cleanup_profiles()
    assert tab.selected_cleanup_profile_id() == "email"
    tab.is_recording = False
    tab._update_recording_state()
    assert tab.selected_cleanup_profile_id() == ""


def test_capture_suspends_only_until_release_or_cancel():
    field = ProfileHotkeyInput()
    states, shortcuts = [], []
    field.capture_changed.connect(states.append)
    field.captured.connect(shortcuts.append)
    field.begin_capture()
    modifiers = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
    QTest.keyPress(field, Qt.Key.Key_T, modifiers)
    assert states == [True]
    QTest.keyRelease(field, Qt.Key.Key_T, modifiers)
    assert states == [True, False]
    expected = "cmd+alt+t" if sys.platform == "darwin" else "ctrl+alt+t"
    assert shortcuts == [expected]
    field.begin_capture()
    QTest.keyClick(field, Qt.Key.Key_Escape)
    assert states[-2:] == [True, False]
    assert shortcuts == [expected]


def test_hotkey_dictation_routes_back_from_upload_to_quick_record():
    tab = QuickRecordTab()
    ui = SimpleNamespace(
        _transcription_source_tab=TabbedContentWidget.TAB_UPLOAD_FILE,
        main_window=SimpleNamespace(quick_record_tab=tab),
    )
    UIController.on_dictation_started(ui, STARTER_PROFILES[1])
    assert ui._transcription_source_tab == TabbedContentWidget.TAB_QUICK_RECORD
    assert tab._active_cleanup_profile.id == "email"


def test_capture_accepts_tab_without_moving_focus():
    field = ProfileHotkeyInput()
    field.begin_capture()
    QTest.keyClick(field, Qt.Key.Key_Tab, Qt.KeyboardModifier.AltModifier)
    assert field.hotkey == "alt+tab"
    assert not field._capturing


def test_windows_profile_shortcuts_toggle_once_per_press_and_unregister():
    from tests.test_push_hold_recording import (
        _CallbackLog,
        _key_event,
        _load_windows_backend,
    )

    module = _load_windows_backend({"ctrl", "alt"})
    manager = module.HotkeyManager()
    callback = _CallbackLog()
    manager.set_profile_hotkeys({"ticket": "ctrl+alt+t"}, callback)
    down = _key_event("down", "t", keypad=False)
    up = _key_event("up", "t", keypad=False)
    assert manager._handle_keyboard_event(down) is False
    assert callback.wait()
    manager._handle_keyboard_event(down)
    assert manager._handle_keyboard_event(up) is False
    assert callback.calls == [("ticket",)]
    manager.program_enabled = False
    assert manager._handle_keyboard_event(down) is True
    manager.program_enabled = True
    manager.set_capture_suspended(True)
    assert manager._handle_keyboard_event(down) is True
    manager.set_capture_suspended(False)
    manager.set_profile_hotkeys({}, callback)
    assert manager._handle_keyboard_event(down) is True


def test_pynput_and_carbon_route_profiles_without_changing_standard_mode():
    from services.settings import RecordingTriggerMode
    from tests.test_push_hold_recording import _CallbackLog, _load_pynput_backend

    module = _load_pynput_backend()
    manager = module.HotkeyManager()
    callback = _CallbackLog()
    manager._carbon_registrar = Mock()
    manager.set_profile_hotkeys({"email": "ctrl+alt+e"}, callback)
    registered = manager._carbon_registrar.register_hotkeys.call_args.args[0]
    assert registered["profile:email"] == "ctrl+alt+e"
    manager.set_record_mode(RecordingTriggerMode.PUSH_HOLD)
    assert manager.handle_hotkey_press(frozenset({"ctrl", "alt"}), "e")
    assert callback.wait()
    # Even a repeat arriving outside the duplicate-delivery interval is ignored
    # until release, including events delivered directly by Carbon.
    manager._last_action_times.clear()
    manager.trigger_action("profile:email")
    manager.trigger_action("profile:email", released=True)
    assert callback.calls == [("email",)]
    manager.set_capture_suspended(True)
    manager._carbon_registrar.unregister_all.assert_called_once()
    assert not manager.handle_hotkey_press(frozenset({"ctrl", "alt"}), "e")
    manager.set_capture_suspended(False)
    manager.program_enabled = False
    assert not manager.handle_hotkey_press(frozenset({"ctrl", "alt"}), "e")
    manager.set_profile_hotkeys({}, callback)
    registered = manager._carbon_registrar.register_hotkeys.call_args.args[0]
    assert "profile:email" not in registered
