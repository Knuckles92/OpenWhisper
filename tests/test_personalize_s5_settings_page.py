"""Settings → Personalize → Snippets: the switch, library, editor and Basic row."""
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
    QPushButton,
)

from services.settings import SettingsKey, SettingsManager, SettingsView
from services.snippets import Snippet, load_snippets
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_metadata
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs import settings_snippets
from ui_qt.dialogs.settings_destinations import BASIC_DICTATION, SNIPPETS
from ui_qt.widgets.setting_tile import TileBase

CAL = Snippet("cal", "my calendar link", "https://cal.example.com/dana")
ADDRESS = Snippet("address", "my email address", "dana@example.com")
SIGN_OFF = Snippet("sig", "my sign-off", "**Dana Lee**\nDesigner", formatted=True)
QUESTION = "ui_qt.widgets.snippets_panel.QMessageBox.question"


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
def make_dialog():
    stacks = []
    dialogs = []
    temp = tempfile.TemporaryDirectory()

    def build(values=None, view=SettingsView.ADVANCED):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({SettingsKey.SETTINGS_VIEW: view, **(values or {})})
        stack = ExitStack()
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(patch.object(settings_dialog_module.history_manager, "set_retention"))
        for module in (models_module, downloads_module):
            stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        stacks.append(stack)
        dialog = settings_dialog_module.SettingsDialog(background_cache_scan=False)
        dialog.on_settings_changed = MagicMock()
        dialogs.append(dialog)
        return dialog, store

    yield build
    for dialog in dialogs:
        dialog.close()
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def _library(*snippets):
    return {SettingsKey.DICTATION_SNIPPETS: [s.to_dict() for s in snippets]}


def _open(make_dialog, *snippets, **extra):
    dialog, store = make_dialog({**_library(*snippets), **extra})
    dialog.select_destination(SNIPPETS)
    return dialog, store, dialog.snippets_panel


def _changes(dialog):
    return [call.args for call in dialog.on_settings_changed.call_args_list]


def test_rail_value_counts_snippets_and_says_when_off():
    assert settings_snippets.rail_value({}) == "None yet"
    assert settings_snippets.rail_value(_library(CAL)) == "1 snippet"
    assert settings_snippets.rail_value(_library(CAL, ADDRESS)) == "2 snippets"
    assert settings_snippets.rail_value({**_library(CAL), SettingsKey.SNIPPETS_ENABLED: False}) == "Off"


def test_the_switch_saves_and_tells_the_app(make_dialog):
    dialog, store, _panel = _open(make_dialog, CAL)
    assert dialog.snippets_enabled_check.isChecked()
    assert dialog.rail.value(SNIPPETS) == "1 snippet"

    dialog.snippets_enabled_check.click()

    assert store.get(SettingsKey.SNIPPETS_ENABLED) is False
    assert dialog.snippets_enabled_tile.property("checked") is False
    assert dialog.rail.value(SNIPPETS) == "Off"
    assert _changes(dialog) == [("snippets",)]


def test_a_failed_switch_save_shows_what_is_still_saved(make_dialog):
    dialog, store, _panel = _open(make_dialog, CAL)
    with patch.object(store, "save_setting", side_effect=OSError("Disk full")):
        dialog.snippets_enabled_check.click()

    assert dialog.snippets_enabled_check.isChecked()
    assert dialog.snippets_enabled_tile.property("checked") is True
    assert "Disk full" in dialog.message_label.text()
    assert _changes(dialog) == []


def test_the_page_has_its_tiles_and_shows_the_first_snippet(make_dialog):
    dialog, _store, panel = _open(make_dialog, CAL, SIGN_OFF)

    titles = [tile.title_label.text() for tile in dialog._pages[SNIPPETS].findChildren(TileBase)]
    assert titles == ["Expand snippets as you dictate", "Your snippets"]
    assert panel.count_label.text() == "2 / 200"
    assert [panel.snippet_list.item(i).text() for i in range(2)] == [CAL.trigger, SIGN_OFF.trigger]
    assert panel.trigger_edit.text() == CAL.trigger
    assert panel.text_edit.toPlainText() == CAL.text
    assert not panel.format_switch.isChecked()
    assert panel.preview.text() == "Say “my calendar link” → inserts 28 characters"

    panel.snippet_list.setCurrentRow(1)

    assert panel.format_switch.isChecked()
    assert panel.preview.text() == "Say “my sign-off” → inserts 17 characters with formatting"
    assert not panel.has_unsaved_changes()


def test_a_new_snippet_is_saved_and_reported(make_dialog):
    dialog, store, panel = _open(make_dialog, CAL)

    panel.new_snippet()
    panel.trigger_edit.setText("  My Sign-off ")
    panel.text_edit.setPlainText("Best,\nDana\n")
    panel.format_switch.setChecked(True)
    assert panel.save_snippet()

    saved = load_snippets(store.load_all_settings())
    assert [(s.trigger, s.text, s.formatted) for s in saved] == [
        (CAL.trigger, CAL.text, False), ("My Sign-off", "Best,\nDana", True),
    ]
    assert panel.snippet_list.currentItem().text() == "My Sign-off"
    assert panel.message.text() == "Saved. Say “My Sign-off” to insert it."
    assert dialog.rail.value(SNIPPETS) == "2 snippets"
    assert _changes(dialog) == [("snippets",)]


def test_save_explains_what_is_missing_and_writes_nothing(make_dialog):
    dialog, store, panel = _open(make_dialog, CAL)

    panel.new_snippet()
    panel.trigger_edit.setText("my home address")
    assert not panel.save_snippet()

    assert panel.message.text() == "Add the text to insert."
    assert panel.message.property("tone") == "error"
    assert load_snippets(store.load_all_settings()) == [CAL]
    assert _changes(dialog) == []


def test_trigger_notes_flag_duplicates_and_near_misses(make_dialog):
    _dialog, store, panel = _open(make_dialog, CAL, ADDRESS)

    panel.new_snippet()
    panel.trigger_edit.setText("My Calendar-Link!")
    assert panel.trigger_note.text() == "Another snippet already uses this trigger."
    assert panel.trigger_note.property("tone") == "error"
    panel.text_edit.setPlainText("https://other.example.com")
    assert not panel.save_snippet()
    assert len(load_snippets(store.load_all_settings())) == 2

    panel.trigger_edit.setText("my email")
    assert panel.trigger_note.text().startswith("Also part of “my email address”")
    assert panel.trigger_note.property("tone") == "hint"
    assert not panel.trigger_note.isHidden()

    panel.trigger_edit.setText("my office phone")
    assert panel.trigger_note.isHidden()


def test_switching_away_from_unsaved_edits_asks_first(make_dialog):
    _dialog, store, panel = _open(make_dialog, CAL, ADDRESS)
    panel.text_edit.setPlainText("https://cal.example.com/new")

    with patch(QUESTION, return_value=QMessageBox.StandardButton.Cancel) as ask:
        panel.snippet_list.setCurrentRow(1)
    assert ask.call_count == 1
    assert panel.snippet_list.currentRow() == 0
    assert panel.text_edit.toPlainText() == "https://cal.example.com/new"

    with patch(QUESTION, return_value=QMessageBox.StandardButton.Save):
        panel.snippet_list.setCurrentRow(1)
    assert panel.trigger_edit.text() == ADDRESS.trigger
    assert load_snippets(store.load_all_settings())[0].text == "https://cal.example.com/new"

    panel.text_edit.setPlainText("changed my mind")
    with patch(QUESTION, return_value=QMessageBox.StandardButton.Discard):
        assert panel.new_snippet()
    assert load_snippets(store.load_all_settings())[1] == ADDRESS
    assert panel.trigger_edit.text() == ""


def test_refreshing_settings_keeps_an_unsaved_draft(make_dialog):
    dialog, _store, panel = _open(make_dialog, CAL)
    panel.new_snippet()
    panel.trigger_edit.setText("my home address")

    dialog.refresh()

    assert panel.trigger_edit.text() == "my home address"
    assert panel.has_unsaved_changes()


def test_duplicate_starts_an_unsaved_copy_with_a_free_trigger(make_dialog):
    _dialog, store, panel = _open(make_dialog, SIGN_OFF, Snippet("two", "my sign-off 2", "x"))

    panel.duplicate_snippet()

    assert panel.trigger_edit.text() == "my sign-off 3"
    assert panel.text_edit.toPlainText() == SIGN_OFF.text
    assert panel.format_switch.isChecked()
    assert panel.has_unsaved_changes()
    assert panel.snippet_list.currentRow() == -1
    assert len(load_snippets(store.load_all_settings())) == 2


def test_delete_asks_then_removes_and_shows_the_next_snippet(make_dialog):
    dialog, store, panel = _open(make_dialog, CAL, ADDRESS)

    with patch(QUESTION, return_value=QMessageBox.StandardButton.Cancel):
        panel.delete_snippet()
    assert len(load_snippets(store.load_all_settings())) == 2

    with patch(QUESTION, return_value=QMessageBox.StandardButton.Yes):
        panel.delete_snippet()
    assert load_snippets(store.load_all_settings()) == [ADDRESS]
    assert panel.trigger_edit.text() == ADDRESS.trigger
    assert dialog.rail.value(SNIPPETS) == "1 snippet"
    assert _changes(dialog) == [("snippets",)]


def test_an_empty_library_offers_starters(make_dialog):
    dialog, _store, panel = _open(make_dialog)
    dialog.show()
    QApplication.processEvents()

    assert panel.empty_state.isVisible() and not panel.snippet_list.isVisible()
    assert not panel.delete_button.isEnabled() and not panel.duplicate_button.isEnabled()
    starter = next(b for b in panel.suggestion_buttons if b.text() == "my calendar link")
    starter.click()

    assert panel.trigger_edit.text() == "my calendar link"
    assert panel.text_edit.hasFocus()
    assert panel.preview.text() == "Say “my calendar link” → add the text it inserts."
    assert panel.has_unsaved_changes()
    # Closing the visible window would ask about this unfinished draft.
    dialog.hide()


def test_snippet_text_is_never_logged(make_dialog, caplog):
    secret = "https://cal.example.com/private-token-123"
    _dialog, _store, panel = _open(make_dialog, CAL)
    with caplog.at_level("DEBUG"):
        panel.new_snippet()
        panel.trigger_edit.setText("my private link")
        panel.text_edit.setPlainText(secret)
        panel.save_snippet()
    assert "private" not in caplog.text


def test_basic_tab_counts_snippets_and_adds_one(make_dialog):
    dialog, store = make_dialog({**_library(CAL)}, view=SettingsView.BASIC)
    page = dialog._basic_pages[BASIC_DICTATION]
    add = next(b for b in page.findChildren(QPushButton) if b.accessibleName() == "Add a snippet")
    detail = next(
        label for label in page.findChildren(QLabel, "basicSettingsDescription")
        if label.text() == "1 snippet"
    )
    assert SNIPPETS not in dialog._built_pages

    add.click()

    assert dialog.settings_view == SettingsView.ADVANCED
    assert dialog.rail.current_key() == SNIPPETS
    panel = dialog.snippets_panel
    assert panel.trigger_edit.text() == "" and panel.snippet_list.currentRow() == -1
    panel.trigger_edit.setText("my email address")
    panel.text_edit.setPlainText("dana@example.com")
    assert panel.save_snippet()
    assert detail.text() == "2 snippets"

    dialog.snippets_enabled_check.click()
    assert detail.text() == "Off · 2 snippets"


@pytest.mark.parametrize("ui_mode,width", [("classic", 940), ("omarchy", 560)])
def test_the_page_fits_narrow_windows_at_large_fonts(make_dialog, monkeypatch, ui_mode, width):
    from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
    from ui_qt.utils.palette import current_palette, set_current_palette
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    previous_style, previous_font = app.styleSheet(), app.font()
    previous_scale, previous_palette = current_ui_font_scale_percent(), current_palette()
    try:
        apply_ui_font_scale(130, app=app, theme_manager=ThemeManager())
        dialog, _store, panel = _open(make_dialog, CAL, ADDRESS, SIGN_OFF)
        dialog.show()
        dialog.resize(width, 600)
        for _ in range(8):
            app.processEvents()
        page = dialog._pages[SNIPPETS]
        assert dialog.width() == width
        for control in page.findChildren(QAbstractButton) + page.findChildren(QLineEdit):
            if control.isVisible():
                assert control.mapTo(page, control.rect().topLeft()).x() >= 0
                assert control.mapTo(page, control.rect().bottomRight()).x() < page.width()
        for label in page.findChildren(QLabel):
            if label.isVisible() and label.wordWrap():
                assert label.height() >= label.heightForWidth(label.width())
        buttons = [panel.new_button, panel.duplicate_button, panel.delete_button, panel.save_button]
        for index, button in enumerate(buttons):
            box = button.rect().translated(button.mapTo(page, button.rect().topLeft()))
            for other in buttons[index + 1:]:
                other_box = other.rect().translated(other.mapTo(page, other.rect().topLeft()))
                assert not box.intersects(other_box)
            assert button.width() >= button.minimumSizeHint().width()
        assert panel.text_edit.height() >= panel.text_edit.minimumHeight()
        scroll = dialog._page_scrolls[SNIPPETS]
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        app.processEvents()
        assert page.mapTo(scroll.viewport(), page.rect().bottomRight()).y() <= scroll.viewport().height()
    finally:
        apply_ui_font_scale(previous_scale, app=app)
        set_current_palette(previous_palette)
        app.setFont(previous_font)
        app.setStyleSheet(previous_style)


@pytest.mark.parametrize("ui_mode,width", [("classic", 720), ("omarchy", 460)])
def test_the_basic_row_fits_narrow_windows_at_large_fonts(make_dialog, monkeypatch, ui_mode, width):
    from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    previous_style, previous_font = app.styleSheet(), app.font()
    previous_scale = current_ui_font_scale_percent()
    try:
        apply_ui_font_scale(130, app=app, theme_manager=ThemeManager())
        dialog, _store = make_dialog(_library(CAL), view=SettingsView.BASIC)
        dialog.show()
        dialog._fit_to_screen()
        dialog.resize(width, 600)
        for _ in range(8):
            app.processEvents()
        page = dialog._basic_pages[BASIC_DICTATION]
        add = next(b for b in page.findChildren(QPushButton) if b.accessibleName() == "Add a snippet")
        assert add.isVisible()
        assert add.mapTo(page, add.rect().topLeft()).x() >= 0
        assert add.mapTo(page, add.rect().bottomRight()).x() < page.width()
        assert add.width() >= add.minimumSizeHint().width()
    finally:
        apply_ui_font_scale(previous_scale, app=app)
        app.setFont(previous_font)
        app.setStyleSheet(previous_style)
