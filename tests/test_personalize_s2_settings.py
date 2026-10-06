"""The cleanup level in Settings: AI cleanup page, Learned rules gate, Basic, rail."""

import os
import tempfile
from contextlib import ExitStack
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QEventLoop, QTimer
from PyQt6.QtGui import QFocusEvent
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton

from config import config
from services.settings import SettingsKey, SettingsManager, SettingsView
from ui_qt.dialogs import cleanup_levels
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import (
    BASIC_DICTATION,
    CLEANUP,
    CLEANUP_RULES,
    OVERVIEW,
)

ENABLED = SettingsKey.TRANSCRIPT_CLEANUP_ENABLED
LEVEL = SettingsKey.TRANSCRIPT_CLEANUP_LEVEL
PROMPT = SettingsKey.TRANSCRIPT_CLEANUP_PROMPT
PRESETS = config.TRANSCRIPT_CLEANUP_LEVEL_PROMPTS
CUSTOM = "Rewrite it as a friendly Slack message."
YES, NO = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
MODEL = {
    SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openai",
    SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "gpt-test",
}


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def make_dialog():
    """Build Settings against a throwaway store with no model cache."""
    stacks = []
    temp = tempfile.TemporaryDirectory()

    def build(values=None, view=SettingsView.ADVANCED):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({SettingsKey.SETTINGS_VIEW: view, **(values or {})})
        stack = ExitStack()
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(
            patch.object(settings_dialog_module.history_manager, "set_retention")
        )
        for module in (models_module, downloads_module):
            stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        stacks.append(stack)
        dialog = settings_dialog_module.SettingsDialog(
            get_loaded_model=lambda: None, background_cache_scan=False
        )
        return dialog, store

    yield build
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def _answer(reply):
    return patch.object(settings_dialog_module.QMessageBox, "question", return_value=reply)


def _segment(value):
    return cleanup_levels.segment_index(value)


def _pump():
    for _ in range(3):
        QApplication.processEvents()


def _selected(dialog):
    return cleanup_levels.SEGMENTS[dialog.cleanup_level_bar.currentIndex()][0]


def _focus_out(widget):
    QApplication.sendEvent(widget, QFocusEvent(QEvent.Type.FocusOut))


# --- the level bar ------------------------------------------------------------


def test_the_bar_shows_the_saved_level_and_saves_only_clicks(make_dialog):
    dialog, store = make_dialog({ENABLED: True, LEVEL: "high"})
    dialog.ensure_page(CLEANUP)

    assert _selected(dialog) == "high"
    assert dialog.cleanup_level_note.text() == cleanup_levels.NOTES["high"]
    assert not dialog.cleanup_level_bar.buttons[-1].isEnabled()
    assert dialog.cleanup_prompt_edit.toPlainText() == ""
    assert dialog.cleanup_prompt_edit.placeholderText() == PRESETS["high"]

    dialog.cleanup_level_bar.setCurrentIndex(_segment("light"))
    assert store.get(LEVEL) == "high"

    with _answer(YES) as question:
        dialog.cleanup_level_bar.buttons[_segment("light")].click()
        _pump()
    question.assert_not_called()
    assert store.get(LEVEL) == "light"
    assert _selected(dialog) == "light"
    assert dialog.cleanup_level_note.text() == cleanup_levels.NOTES["light"]
    assert dialog.cleanup_prompt_edit.placeholderText() == PRESETS["light"]
    assert dialog.rail.value(CLEANUP) == "Light · openrouter/free"


def test_the_level_follows_the_switch(make_dialog):
    dialog, store = make_dialog({ENABLED: False})
    dialog.ensure_page(CLEANUP)
    assert not dialog.cleanup_level_tile.isEnabled()
    assert _selected(dialog) == "medium"

    dialog.transcript_cleanup_check.setChecked(True)

    assert dialog.cleanup_level_tile.isEnabled()
    assert store.get(ENABLED) is True
    assert LEVEL not in store.load_all_settings()


def test_a_saved_custom_prompt_shows_as_custom(make_dialog):
    dialog, _store = make_dialog({ENABLED: True, PROMPT: f"  {CUSTOM} ", **MODEL})
    dialog.ensure_page(CLEANUP)

    assert _selected(dialog) == cleanup_levels.CUSTOM
    assert dialog.cleanup_level_bar.buttons[-1].isEnabled()
    assert dialog.cleanup_level_note.text() == cleanup_levels.NOTES[cleanup_levels.CUSTOM]
    assert dialog.cleanup_prompt_edit.toPlainText() == CUSTOM
    assert dialog.cleanup_prompt_reset_btn.isEnabled()
    assert dialog.rail.value(CLEANUP) == "Custom · gpt-test"


@pytest.mark.parametrize("saved", [config.TRANSCRIPT_CLEANUP_PROMPT, PRESETS["high"], "   "])
def test_a_saved_built_in_prompt_is_not_custom(make_dialog, saved):
    dialog, _store = make_dialog({ENABLED: True, PROMPT: saved})
    dialog.ensure_page(CLEANUP)

    assert _selected(dialog) == "medium"
    assert dialog.cleanup_prompt_edit.toPlainText() == ""
    assert not dialog.cleanup_prompt_reset_btn.isEnabled()


def test_declining_keeps_the_custom_prompt(make_dialog):
    dialog, store = make_dialog({ENABLED: True, LEVEL: "light", PROMPT: CUSTOM})
    dialog.ensure_page(CLEANUP)

    with _answer(NO) as question:
        dialog.cleanup_level_bar.buttons[_segment("high")].click()
        _pump()

    question.assert_called_once()
    assert store.get(PROMPT) == CUSTOM
    assert store.get(LEVEL) == "light"
    assert _selected(dialog) == cleanup_levels.CUSTOM
    assert dialog.cleanup_prompt_edit.toPlainText() == CUSTOM


def test_declining_in_a_real_dialog_still_shows_custom(make_dialog, monkeypatch):
    dialog, _store = make_dialog({ENABLED: True, PROMPT: CUSTOM})
    dialog.ensure_page(CLEANUP)

    def modal_no(*_args):
        # A real QMessageBox runs an event loop of its own before answering.
        loop = QEventLoop()
        answer = QTimer(loop)
        answer.setSingleShot(True)
        answer.timeout.connect(loop.quit)
        answer.start(30)
        loop.exec()
        return NO

    monkeypatch.setattr(settings_dialog_module.QMessageBox, "question", modal_no)
    dialog.cleanup_level_bar.buttons[_segment("light")].click()
    _pump()

    assert _selected(dialog) == cleanup_levels.CUSTOM


def test_accepting_removes_the_custom_prompt_and_saves_the_level(make_dialog):
    dialog, store = make_dialog({ENABLED: True, LEVEL: "light", PROMPT: CUSTOM})
    dialog.ensure_page(CLEANUP)

    with _answer(YES):
        dialog.cleanup_level_bar.buttons[_segment("high")].click()
        _pump()

    assert PROMPT not in store.load_all_settings()
    assert store.get(LEVEL) == "high"
    assert _selected(dialog) == "high"
    assert not dialog.cleanup_level_bar.buttons[-1].isEnabled()
    assert dialog.cleanup_prompt_edit.toPlainText() == ""
    assert dialog.cleanup_prompt_edit.placeholderText() == PRESETS["high"]


def test_clicking_custom_while_it_is_in_use_changes_nothing(make_dialog):
    dialog, store = make_dialog({ENABLED: True, PROMPT: CUSTOM})
    dialog.ensure_page(CLEANUP)

    with _answer(YES) as question:
        dialog.cleanup_level_bar.buttons[-1].click()
        _pump()

    question.assert_not_called()
    assert store.get(PROMPT) == CUSTOM
    assert _selected(dialog) == cleanup_levels.CUSTOM


# --- the custom prompt --------------------------------------------------------


def test_typing_a_prompt_makes_it_custom_on_focus_out(make_dialog):
    dialog, store = make_dialog({ENABLED: True})
    dialog.ensure_page(CLEANUP)

    dialog.cleanup_prompt_edit.setPlainText(f"{CUSTOM}\n")
    _focus_out(dialog.cleanup_prompt_edit)

    assert store.get(PROMPT) == CUSTOM
    assert _selected(dialog) == cleanup_levels.CUSTOM
    assert dialog.rail.value(CLEANUP) == "Custom · openrouter/free"


@pytest.mark.parametrize("text", ["", "  \n", PRESETS["medium"], config.TRANSCRIPT_CLEANUP_PROMPT])
def test_empty_or_built_in_text_removes_the_prompt(make_dialog, text):
    dialog, store = make_dialog({ENABLED: True, PROMPT: CUSTOM})
    dialog.ensure_page(CLEANUP)

    dialog.cleanup_prompt_edit.setPlainText(text)
    _focus_out(dialog.cleanup_prompt_edit)

    assert PROMPT not in store.load_all_settings()
    assert dialog.cleanup_prompt_edit.toPlainText() == ""
    assert _selected(dialog) == "medium"


def test_closing_never_saves_preset_text(make_dialog):
    dialog, store = make_dialog({ENABLED: True})
    dialog.ensure_page(CLEANUP)

    dialog.close()

    assert PROMPT not in store.load_all_settings()


def test_reset_asks_then_removes_the_key(make_dialog):
    dialog, store = make_dialog({ENABLED: True, LEVEL: "high", PROMPT: CUSTOM})
    dialog.ensure_page(CLEANUP)

    with _answer(NO):
        dialog.cleanup_prompt_reset_btn.click()
    assert store.get(PROMPT) == CUSTOM

    with _answer(YES) as question:
        dialog.cleanup_prompt_reset_btn.click()

    assert "High" in question.call_args.args[2]
    assert PROMPT not in store.load_all_settings()
    assert store.get(LEVEL) == "high"
    assert dialog.cleanup_prompt_edit.toPlainText() == ""
    assert not dialog.cleanup_prompt_reset_btn.isEnabled()
    assert _selected(dialog) == "high"


def test_reset_also_clears_an_old_default_saved_by_earlier_versions(make_dialog):
    dialog, store = make_dialog({ENABLED: True, PROMPT: config.TRANSCRIPT_CLEANUP_PROMPT})
    dialog.ensure_page(CLEANUP)

    with _answer(YES) as question:
        dialog._reset_cleanup_prompt()

    question.assert_not_called()
    assert PROMPT not in store.load_all_settings()


def test_the_editor_starts_from_the_levels_instructions(make_dialog, monkeypatch):
    dialog, store = make_dialog({ENABLED: True, LEVEL: "light"})
    dialog.ensure_page(CLEANUP)
    opened = []

    class FakeEditor:
        def __init__(self, prompt, parent):
            opened.append(prompt)

        def exec(self):
            return QDialog.DialogCode.Accepted

        def prompt_text(self):
            return opened[0] + " Use British spelling."

    monkeypatch.setattr(settings_dialog_module, "CleanupPromptDialog", FakeEditor)

    dialog._open_cleanup_prompt_editor()

    assert opened == [PRESETS["light"]]
    assert store.get(PROMPT) == PRESETS["light"] + " Use British spelling."
    assert _selected(dialog) == cleanup_levels.CUSTOM


# --- rail, Overview and the Learned rules gate --------------------------------


@pytest.mark.parametrize(
    ("values", "rail", "card"),
    [
        ({}, "Off", "Off"),
        ({ENABLED: True, **MODEL}, "Medium · gpt-test", "Medium"),
        ({ENABLED: True, LEVEL: "high", **MODEL}, "High · gpt-test", "High"),
        ({ENABLED: True, PROMPT: CUSTOM, **MODEL}, "Custom · gpt-test", "Custom prompt"),
        ({PROMPT: CUSTOM, LEVEL: "light"}, "Off", "Off"),
    ],
)
def test_rail_and_overview_name_the_level(make_dialog, values, rail, card):
    dialog, _store = make_dialog(values)
    dialog.select_destination(OVERVIEW)

    assert dialog.rail.value(CLEANUP) == rail
    assert dialog.overview.cards["cleanup"].value_label.text() == card
    assert dialog.overview.cards["cleanup_profiles"].eyebrow_label.text() == "PERSONALIZE"


def test_the_learned_rules_gate_turns_cleanup_on_at_the_saved_level(make_dialog):
    dialog, store = make_dialog({LEVEL: "high"})
    dialog.ensure_page(CLEANUP_RULES)
    button = dialog.cleanup_rules_turn_on_btn

    assert not dialog.cleanup_rules_gate_tile.isHidden()
    assert button.text() == "Turn on (High)"
    assert not dialog.cleanup_rules_composer_tile.isEnabled()

    button.click()

    assert store.get(ENABLED) is True
    assert store.get(LEVEL) == "high"
    assert dialog.transcript_cleanup_check.isChecked()
    assert dialog.cleanup_rules_gate_tile.isHidden()
    assert dialog.cleanup_rules_composer_tile.isEnabled()
    assert dialog.rail.value(CLEANUP) == "High · openrouter/free"


def test_a_gate_on_another_page_turns_cleanup_on_without_building_it(make_dialog):
    dialog, store = make_dialog({LEVEL: "light"})
    # As Apps & styles would: a gate on a page of its own.
    gate = dialog.cleanup_gate_tile("Styles only change text while AI cleanup runs.")
    dialog._pages[OVERVIEW].layout().insertWidget(0, gate)
    dialog._update_cleanup_prompt_ui()
    button = gate.findChildren(QPushButton)[0]
    assert not gate.isHidden()
    assert button.text() == "Turn on (Light)"

    button.click()

    assert store.get(ENABLED) is True
    assert store.get(LEVEL) == "light"
    assert gate.isHidden()
    assert CLEANUP not in dialog._built_pages


# --- Basic --------------------------------------------------------------------


def _basic(make_dialog, values):
    dialog, store = make_dialog(values, view=SettingsView.BASIC)
    return dialog, store, dialog._basic_pages[BASIC_DICTATION]


def _items(combo):
    return [combo.itemData(index) for index in range(combo.count())]


def test_basic_shows_the_level_under_the_switch(make_dialog):
    dialog, _store, page = _basic(make_dialog, {ENABLED: True, LEVEL: "high"})
    combo = page.cleanup_level_combo

    assert _items(combo) == ["light", "medium", "high"]
    assert combo.currentData() == "high"
    assert combo.isEnabled()
    assert page.cleanup_level_detail.text() == cleanup_levels.SHORT_NOTES["high"]
    assert CLEANUP not in dialog._built_pages


def test_basic_level_is_disabled_while_cleanup_is_off(make_dialog):
    _dialog, _store, page = _basic(make_dialog, {})

    assert not page.cleanup_level_combo.isEnabled()
    assert page.cleanup_level_combo.currentData() == "medium"


def test_basic_saves_a_level_without_building_the_cleanup_page(make_dialog):
    dialog, store, page = _basic(make_dialog, {ENABLED: True})
    combo = page.cleanup_level_combo

    with _answer(YES) as question:
        combo.setCurrentIndex(combo.findData("light"))
        combo.activated.emit(combo.currentIndex())

    question.assert_not_called()
    assert store.get(LEVEL) == "light"
    assert page.cleanup_level_detail.text() == cleanup_levels.SHORT_NOTES["light"]
    assert CLEANUP not in dialog._built_pages


def test_basic_asks_before_replacing_a_custom_prompt(make_dialog):
    _dialog, store, page = _basic(make_dialog, {ENABLED: True, PROMPT: CUSTOM})
    combo = page.cleanup_level_combo
    assert _items(combo) == ["light", "medium", "high", cleanup_levels.CUSTOM]
    assert combo.currentData() == cleanup_levels.CUSTOM

    with _answer(NO):
        combo.setCurrentIndex(combo.findData("high"))
        combo.activated.emit(combo.currentIndex())
    assert store.get(PROMPT) == CUSTOM
    assert combo.currentData() == cleanup_levels.CUSTOM

    with _answer(YES):
        combo.setCurrentIndex(combo.findData("high"))
        combo.activated.emit(combo.currentIndex())
    assert PROMPT not in store.load_all_settings()
    assert store.get(LEVEL) == "high"
    assert _items(combo) == ["light", "medium", "high"]
    assert combo.currentData() == "high"


def test_basic_follows_a_change_made_on_the_cleanup_page(make_dialog):
    dialog, _store, page = _basic(make_dialog, {ENABLED: True})
    dialog.ensure_page(CLEANUP)

    dialog.cleanup_level_bar.buttons[_segment("high")].click()

    assert page.cleanup_level_combo.currentData() == "high"


# --- layout: classic and Omarchy, narrow, 130% --------------------------------


@pytest.mark.parametrize("ui_mode,width", [("classic", 940), ("omarchy", 720)])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_cleanup_tiles_fit_narrow_windows_at_large_fonts(make_dialog, monkeypatch, ui_mode, width, theme):
    from PyQt6.QtWidgets import QAbstractButton, QLabel
    from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
    from ui_qt.utils.palette import current_palette, set_current_palette
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    previous_style, previous_font = app.styleSheet(), app.font()
    previous_scale, previous_palette = current_ui_font_scale_percent(), current_palette()
    dialog = None
    try:
        apply_ui_font_scale(130, app=app, theme_manager=ThemeManager(theme))
        dialog, _store = make_dialog({PROMPT: CUSTOM})
        dialog.show()
        dialog._fit_to_screen()
        dialog.resize(width, 640)
        for key, tiles in (
            (CLEANUP, ("cleanup_level_tile", "cleanup_prompt_tile")),
            (CLEANUP_RULES, ("cleanup_rules_gate_tile",)),
        ):
            dialog.select_destination(key)
            for _ in range(8):
                app.processEvents()
            page = dialog._pages[key]
            for name in tiles:
                tile = getattr(dialog, name)
                assert tile.isVisible(), name
                for control in tile.findChildren(QAbstractButton):
                    if not control.isVisible():
                        continue
                    left = control.mapTo(page, control.rect().topLeft()).x()
                    right = control.mapTo(page, control.rect().bottomRight()).x()
                    assert 0 <= left and right < page.width(), (name, control.text())
                    assert control.width() >= control.minimumSizeHint().width(), control.text()
                for label in tile.findChildren(QLabel):
                    if label.isVisible() and label.wordWrap():
                        assert label.height() >= label.heightForWidth(label.width()), label.text()
    finally:
        if dialog is not None:
            dialog.close()
        apply_ui_font_scale(previous_scale, app=app)
        set_current_palette(previous_palette)
        app.setFont(previous_font)
        app.setStyleSheet(previous_style)
