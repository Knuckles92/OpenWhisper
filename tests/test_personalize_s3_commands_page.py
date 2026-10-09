"""Settings → Commands: the Command Mode shortcut, its switches, and the transforms library."""

import os
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QLabel,
    QLineEdit,
    QMessageBox,
)

from services import text_rewrite
from services.settings import SettingsKey, SettingsManager, SettingsView
from services.text_transforms import STARTER_TRANSFORMS, load_transforms
from ui_qt.dialogs import settings_commands
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_metadata
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import (
    BASIC_DICTATION,
    CLEANUP,
    CLEANUP_PROFILES,
    COMMANDS,
    GENERAL,
)

PROFILE = {"id": "ticket", "name": "Support ticket", "instructions": "Format a ticket.",
           "hotkey": "ctrl+alt+t"}


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _restore_metadata():
    controls = dict(settings_metadata.CONTROL_DESTINATIONS)
    fields = dict(settings_metadata.PAGE_SEARCH_FIELDS)
    yield
    settings_metadata.CONTROL_DESTINATIONS.clear()
    settings_metadata.CONTROL_DESTINATIONS.update(controls)
    settings_metadata.PAGE_SEARCH_FIELDS.clear()
    settings_metadata.PAGE_SEARCH_FIELDS.update(fields)


@pytest.fixture
def provider(monkeypatch):
    state = {"ready": True}
    monkeypatch.setattr(text_rewrite, "provider_ready", lambda _settings: state["ready"])
    return state


@pytest.fixture
def make_dialog(provider):
    stacks = []
    temp = tempfile.TemporaryDirectory()

    def build(values=None):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({SettingsKey.SETTINGS_VIEW: SettingsView.ADVANCED, **(values or {})})
        stack = ExitStack()
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(patch.object(settings_dialog_module.history_manager, "set_retention"))
        for module in (models_module, downloads_module):
            stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        stacks.append(stack)
        dialog = settings_dialog_module.SettingsDialog(background_cache_scan=False)
        dialog.on_profile_hotkey_capture = MagicMock()
        dialog.on_settings_changed = MagicMock()
        return dialog, store

    yield build
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def _commands(make_dialog, values=None):
    dialog, store = make_dialog(values)
    dialog.select_destination(COMMANDS)
    return dialog, store


def test_rail_value_counts_transforms_and_the_shortcut():
    assert settings_commands.rail_value({}) == "No shortcut · 4 transforms"
    settings = {
        "hotkeys": {"command_mode": "ctrl+alt+k"},
        SettingsKey.TEXT_TRANSFORMS: [{"id": "a", "name": "A", "instruction": "Do."}],
    }
    assert settings_commands.rail_value(settings) == "Shortcut set · 1 transform"
    assert settings_commands.rail_value({"hotkeys": "broken"}) == "No shortcut · 4 transforms"


def test_the_ai_cleanup_notice_shows_only_without_a_provider(make_dialog, provider):
    provider["ready"] = False
    dialog, _store = _commands(make_dialog)
    dialog.show()
    try:
        assert dialog.commands_gate_tile.isVisibleTo(dialog._pages[COMMANDS])
        link = next(b for b in dialog.commands_gate_tile.findChildren(QAbstractButton)
                    if b.text() == "Open AI cleanup")
        link.click()
        assert dialog.rail.current_key() == CLEANUP

        # A provider set up on AI cleanup clears the notice on the way back.
        provider["ready"] = True
        dialog.select_destination(COMMANDS)
        QApplication.processEvents()
        assert not dialog.commands_gate_tile.isVisibleTo(dialog._pages[COMMANDS])
    finally:
        dialog.close()


def test_the_shortcut_saves_through_the_hotkey_path(make_dialog):
    dialog, store = _commands(make_dialog)
    field = dialog.commands_shortcut

    field.input.captured.emit("ctrl+alt+k")

    assert store.load_hotkey_settings()["command_mode"] == "ctrl+alt+k"
    assert field.input.text() == "Ctrl+Alt+K"
    assert field.message.isHidden()
    assert dialog.rail.value(COMMANDS) == "Shortcut set · 4 transforms"

    field.clear_button.click()
    assert store.load_hotkey_settings()["command_mode"] == ""
    assert not field.clear_button.isEnabled()


def test_a_taken_shortcut_is_refused_inline(make_dialog):
    dialog, store = _commands(make_dialog, {SettingsKey.TRANSCRIPT_CLEANUP_PROFILES: [PROFILE]})
    field = dialog.commands_shortcut

    field.input.set_hotkey("ctrl+alt+t")
    field.input.captured.emit("ctrl+alt+t")

    assert field.message.text() == "That shortcut is already used by Support ticket."
    assert not field.message.isHidden()
    assert store.load_hotkey_settings().get("command_mode", "") == ""
    assert field.input.hotkey == ""


def test_capturing_pauses_global_shortcuts_and_leaving_stops_it(make_dialog):
    dialog, _store = _commands(make_dialog)
    dialog.commands_shortcut.begin_capture()
    assert dialog.commands_shortcut.input._capturing

    dialog.select_destination(GENERAL)

    assert not dialog.commands_shortcut.input._capturing
    assert [call.args for call in dialog.on_profile_hotkey_capture.call_args_list] == [
        (True,), (False,),
    ]


def test_a_shortcut_set_elsewhere_shows_when_the_page_returns(make_dialog):
    dialog, _store = _commands(make_dialog)
    dialog.show()
    try:
        dialog.select_destination(GENERAL)
        assert dialog.set_standard_hotkey("command_mode", "ctrl+alt+j") == ""
        dialog.select_destination(COMMANDS)
        QApplication.processEvents()
        assert dialog.commands_shortcut.input.text() == "Ctrl+Alt+J"
    finally:
        dialog.close()


@pytest.mark.parametrize("mode, hint", [
    ("push_hold", "Hold the shortcut while you speak, then let go."),
    ("toggle", "Press the shortcut, speak, then press it again."),
])
def test_the_hint_follows_the_recording_mode(make_dialog, mode, hint):
    dialog, _store = _commands(make_dialog, {SettingsKey.RECORDING_TRIGGER_MODE: mode})
    assert dialog.commands_mode_hint.text() == hint


def test_writing_without_a_selection_is_a_saved_switch(make_dialog):
    dialog, store = _commands(make_dialog, {SettingsKey.COMMAND_MODE_INSERT_WITHOUT_SELECTION: False})
    tile = dialog.commands_insert_tile
    assert not tile.checkbox.isChecked()
    assert tile.property("checked") is False

    tile.checkbox.toggle()

    assert store.get(SettingsKey.COMMAND_MODE_INSERT_WITHOUT_SELECTION) is True
    assert tile.property("checked") is True


def test_the_page_says_rewrites_are_kept_in_history(make_dialog):
    dialog, _store = _commands(make_dialog)
    texts = [label.text() for label in dialog._pages[COMMANDS].findChildren(QLabel)]
    assert any("saved in History" in text for text in texts)


def test_profile_shortcuts_are_listed_and_link_to_profiles(make_dialog):
    dialog, _store = _commands(make_dialog, {SettingsKey.TRANSCRIPT_CLEANUP_PROFILES: [
        PROFILE, {"id": "mail", "name": "Email", "instructions": "Email."},
    ]})
    assert dialog.commands_profiles_list.text() == "Support ticket · Ctrl+Alt+T"
    link = next(b for b in dialog.commands_profiles_tile.findChildren(QAbstractButton)
                if b.text() == "Open Profiles")
    link.click()
    assert dialog.rail.current_key() == CLEANUP_PROFILES

    dialog, _store = _commands(make_dialog)
    assert dialog.commands_profiles_list.text() == "No profile has a shortcut yet."


# --- transforms library ------------------------------------------------------


def _answer(monkeypatch, button):
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: asked.append(args[2]) or button)
    return asked


def test_a_new_transform_is_saved_and_registered(make_dialog):
    dialog, store = _commands(make_dialog)
    panel = dialog.transforms_panel
    assert [panel.transform_list.item(i).text() for i in range(panel.transform_list.count())] == [
        t.name for t in STARTER_TRANSFORMS
    ]

    panel.new_transform()
    panel.name_edit.setText("Friendlier")
    panel.instruction_edit.setPlainText("Make it warmer.")
    panel.hotkey_input.captured.emit("ctrl+alt+f")
    panel.hotkey_input.set_hotkey("ctrl+alt+f")
    assert panel.save_transform()

    saved = load_transforms(store.load_all_settings())
    assert [t.name for t in saved][-1] == "Friendlier"
    assert saved[-1].hotkey == "ctrl+alt+f"
    assert panel.message.text() == "Saved Friendlier. Select text anywhere and press Ctrl+Alt+F."
    dialog.on_settings_changed.assert_called_with("transforms")
    assert dialog.rail.value(COMMANDS) == "No shortcut · 5 transforms"


def test_a_taken_transform_shortcut_is_refused_when_captured(make_dialog):
    dialog, _store = _commands(make_dialog, {SettingsKey.TRANSCRIPT_CLEANUP_PROFILES: [PROFILE]})
    panel = dialog.transforms_panel
    panel.hotkey_input.begin_capture()
    panel.hotkey_input.set_hotkey("ctrl+alt+t")

    panel.hotkey_input.captured.emit("ctrl+alt+t")

    assert panel.hotkey_input.hotkey == ""
    assert panel.message.text() == "That shortcut is already used by Support ticket. Choose another."
    assert dialog.on_profile_hotkey_capture.call_args_list[0].args == (True,)


def test_invalid_transforms_explain_themselves(make_dialog):
    dialog, store = _commands(make_dialog)
    panel = dialog.transforms_panel
    panel.new_transform()
    panel.name_edit.setText("polish")
    panel.instruction_edit.setPlainText("Again.")

    assert not panel.save_transform()

    assert panel.message.text() == "A transform with that name already exists."
    assert SettingsKey.TEXT_TRANSFORMS not in store.load_all_settings()
    dialog.on_settings_changed.assert_not_called()


def test_starters_fill_the_instruction(make_dialog, monkeypatch):
    dialog, _store = _commands(make_dialog)
    panel = dialog.transforms_panel
    panel.new_transform()
    starter = STARTER_TRANSFORMS[1]

    panel.use_starter(starter)
    assert (panel.name_edit.text(), panel.instruction_edit.toPlainText()) == (
        starter.name, starter.instruction)

    asked = _answer(monkeypatch, QMessageBox.StandardButton.Cancel)
    panel.use_starter(STARTER_TRANSFORMS[0])
    assert asked == ["Replace the current instruction?"]
    assert panel.instruction_edit.toPlainText() == starter.instruction


def test_an_unsaved_draft_survives_a_refresh_and_asks_before_switching(make_dialog, monkeypatch):
    dialog, store = _commands(make_dialog)
    panel = dialog.transforms_panel
    panel.instruction_edit.setPlainText("Polish, but gently.")

    settings_commands.refresh(dialog)
    assert panel.instruction_edit.toPlainText() == "Polish, but gently."

    asked = _answer(monkeypatch, QMessageBox.StandardButton.Cancel)
    panel.transform_list.setCurrentRow(2)
    assert asked == ["Save your changes to this transform?"]
    assert panel.transform_list.currentRow() == 0
    assert panel.instruction_edit.toPlainText() == "Polish, but gently."

    _answer(monkeypatch, QMessageBox.StandardButton.Save)
    panel.transform_list.setCurrentRow(2)
    assert panel.name_edit.text() == "Fix grammar"
    assert load_transforms(store.load_all_settings())[0].instruction == "Polish, but gently."


def test_duplicates_stay_drafts_until_saved(make_dialog):
    dialog, store = _commands(make_dialog)
    panel = dialog.transforms_panel

    panel.duplicate_transform()

    assert panel.name_edit.text() == "Polish copy"
    assert panel.hotkey_input.hotkey == ""
    assert panel.has_unsaved_changes()
    assert SettingsKey.TEXT_TRANSFORMS not in store.load_all_settings()
    assert panel.save_transform()
    assert [t.name for t in load_transforms(store.load_all_settings())][-1] == "Polish copy"


def test_delete_asks_first(make_dialog, monkeypatch):
    dialog, store = _commands(make_dialog)
    panel = dialog.transforms_panel

    _answer(monkeypatch, QMessageBox.StandardButton.Cancel)
    panel.delete_transform()
    assert SettingsKey.TEXT_TRANSFORMS not in store.load_all_settings()

    _answer(monkeypatch, QMessageBox.StandardButton.Yes)
    panel.delete_transform()
    assert [t.id for t in load_transforms(store.load_all_settings())] == [
        "make-concise", "fix-grammar", "prompt-engineer",
    ]
    assert panel.name_edit.text() == "Make concise"
    dialog.on_settings_changed.assert_called_with("transforms")


def test_cancel_capture_stops_every_capture_on_the_page(make_dialog):
    dialog, _store = _commands(make_dialog)
    dialog.commands_shortcut.begin_capture()
    dialog.transforms_panel.hotkey_input.begin_capture()

    settings_commands.cancel_capture(dialog)

    assert not dialog.commands_shortcut.input._capturing
    assert not dialog.transforms_panel.hotkey_input._capturing


# --- Basic and layout ---------------------------------------------------------


def test_basic_has_a_command_mode_shortcut_row(make_dialog):
    dialog, store = make_dialog({SettingsKey.SETTINGS_VIEW: SettingsView.BASIC})
    page = dialog._basic_pages[BASIC_DICTATION]
    field = dialog.commands_basic_shortcut
    assert page.isAncestorOf(field)
    assert COMMANDS not in dialog._built_pages
    texts = [label.text() for label in page.findChildren(QLabel)]
    assert "Command Mode shortcut" in texts

    field.input.captured.emit("ctrl+alt+k")
    assert store.load_hotkey_settings()["command_mode"] == "ctrl+alt+k"

    record = dialog.current_hotkeys["record_toggle"]
    field.input.captured.emit(record)
    assert "already used by Recording" in dialog.message_label.text()
    assert field.message.isHidden()
    assert store.load_hotkey_settings()["command_mode"] == "ctrl+alt+k"


@pytest.mark.parametrize("ui_mode", ["classic", "omarchy"])
def test_the_page_fits_a_narrow_window_at_large_fonts(make_dialog, monkeypatch, ui_mode):
    from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
    from ui_qt.utils.palette import current_palette, set_current_palette
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    previous_style, previous_font = app.styleSheet(), app.font()
    previous_scale, previous_palette = current_ui_font_scale_percent(), current_palette()
    dialog = None
    try:
        apply_ui_font_scale(130, app=app, theme_manager=ThemeManager())
        dialog, _store = make_dialog({SettingsKey.TRANSCRIPT_CLEANUP_PROFILES: [PROFILE]})
        with patch.object(settings_dialog_module.SettingsDialog, "_fit_to_screen", lambda self: None):
            dialog.show()
        # Narrower than the classic window floor, as a tiled Omarchy window can be.
        dialog.setMinimumSize(0, 0)
        dialog.resize(800, 600)
        dialog.select_destination(COMMANDS)
        for _ in range(8):
            app.processEvents()
        page = dialog._pages[COMMANDS]
        scroll = dialog._page_scrolls[COMMANDS]
        for control in page.findChildren(QAbstractButton) + page.findChildren(QLineEdit):
            if not control.isVisible():
                continue
            assert control.mapTo(page, control.rect().topLeft()).x() >= 0
            assert control.mapTo(page, control.rect().bottomRight()).x() < page.width(), (
                control.objectName() or control.text())
            # Every button keeps at least its label's height.
            if isinstance(control, QAbstractButton) and control.text():
                assert control.height() >= control.fontMetrics().height()
        for label in page.findChildren(QLabel):
            if label.isVisible() and label.wordWrap():
                assert label.height() >= label.heightForWidth(label.width())
        panel = dialog.transforms_panel
        assert panel.duplicate_button.geometry().right() < panel.delete_button.geometry().left()
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        app.processEvents()
        assert page.mapTo(scroll.viewport(), page.rect().bottomRight()).y() <= scroll.viewport().height()
    finally:
        if dialog is not None:
            dialog.close()
        apply_ui_font_scale(previous_scale, app=app)
        set_current_palette(previous_palette)
        app.setFont(previous_font)
        app.setStyleSheet(previous_style)
