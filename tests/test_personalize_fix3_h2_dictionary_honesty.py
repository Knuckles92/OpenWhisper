"""The Dictionary page tells the truth on engines that can't take your words.

On the default Windows setup (Parakeet, AI cleanup off) only Sounds like
spellings are fixed. The page says so, offers to turn AI cleanup on, and
doesn't offer a "Steer the speech model" switch that can't do anything.
"""
import os
import tempfile
from contextlib import ExitStack
from unittest.mock import Mock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from services.settings import SettingsKey, SettingsManager, SettingsView
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_dictionary as page_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_metadata
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import BASIC_DICTATION, DICTIONARY

KEY = SettingsKey.DICTATION_DICTIONARY
CLEANUP = SettingsKey.TRANSCRIPT_CLEANUP_ENABLED
KSENIA = {"id": "a", "term": "Ksenia", "starred": False, "heard": [], "learned": False, "new": False}
ADDED_HINT = "Added “Ksenia”. Without a Sounds like spelling it won't be fixed while AI cleanup is off."


@pytest.fixture(autouse=True)
def _restore_metadata():
    controls = dict(settings_metadata.CONTROL_DESTINATIONS)
    fields = dict(settings_metadata.PAGE_SEARCH_FIELDS)
    yield
    settings_metadata.CONTROL_DESTINATIONS.clear()
    settings_metadata.CONTROL_DESTINATIONS.update(controls)
    settings_metadata.PAGE_SEARCH_FIELDS.clear()
    settings_metadata.PAGE_SEARCH_FIELDS.update(fields)
    page_module.set_backend_provider(None)


@pytest.fixture
def make_dialog():
    stacks = []
    temp = tempfile.TemporaryDirectory()

    def build(values=None):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({SettingsKey.SETTINGS_VIEW: SettingsView.ADVANCED,
                                 SettingsKey.SELECTED_MODEL: "parakeet", **(values or {})})
        stack = ExitStack()
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(patch.object(settings_dialog_module.history_manager, "set_retention"))
        for module in (models_module, downloads_module):
            stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        stacks.append(stack)
        dialog = settings_dialog_module.SettingsDialog(get_loaded_model=lambda: None,
                                                       background_cache_scan=False)
        dialog.on_settings_changed = Mock()
        dialog.on_cleanup_changed = Mock()
        return dialog, store

    yield build
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def open_page(dialog):
    dialog.select_destination(DICTIONARY)
    return dialog._dictionary_page


def add(dialog, term, heard=""):
    dialog.dictionary_term_edit.setText(term)
    dialog.dictionary_heard_edit.setText(heard)
    dialog.dictionary_add_button.click()
    return dialog.dictionary_message.text()


def gate_button(dialog):
    return next(button for tile, button in dialog._cleanup_gates
                if tile is dialog.dictionary_cleanup_gate_tile)


class TestDefaultWindowsSetup:
    """Parakeet with AI cleanup off."""

    def test_the_page_says_what_happens_and_offers_cleanup(self, make_dialog):
        dialog, _store = make_dialog()
        open_page(dialog)
        gate = dialog.dictionary_cleanup_gate_tile
        assert not gate.isHidden()
        assert gate.title_label.text() == "AI cleanup is off"
        assert gate.description_label.text() == (
            "Parakeet can't listen for your words, so only Sounds like spellings "
            "get fixed. AI cleanup uses your words too."
        )
        assert gate_button(dialog).text() == "Turn on (Medium)"

    def test_the_steer_switch_says_why_it_does_nothing(self, make_dialog):
        dialog, _store = make_dialog()
        open_page(dialog)
        assert not dialog.dictionary_steer_tile.isEnabled()
        assert dialog.dictionary_steer_tile.description_label.text() == (
            "Your engine (Parakeet) can't be steered, and AI cleanup is off, so "
            "only Sounds like spellings are fixed."
        )

    def test_adding_a_word_without_sounds_like_says_it_wont_be_fixed(self, make_dialog):
        dialog, _store = make_dialog()
        open_page(dialog)
        assert add(dialog, "Ksenia") == ADDED_HINT
        assert add(dialog, "Oluwaseun", "Olu Washington") == "Added “Oluwaseun”."
        dialog._dictionary_page.begin_edit(dialog._dictionary_page.library.visible_ids[0])
        dialog.dictionary_heard_edit.clear()
        dialog.dictionary_add_button.click()
        assert dialog.dictionary_message.text().startswith("Saved “")
        assert "Without a Sounds like spelling" in dialog.dictionary_message.text()

    def test_one_click_turns_cleanup_on_and_the_page_updates(self, make_dialog):
        dialog, store = make_dialog()
        open_page(dialog)
        gate_button(dialog).click()
        assert store.load_all_settings()[CLEANUP] is True
        assert dialog.dictionary_cleanup_gate_tile.isHidden()
        assert dialog.dictionary_steer_tile.description_label.text() == (
            "Your engine (Parakeet) can't be steered. AI cleanup and Sounds like "
            "spellings fix your words after recognition."
        )
        assert add(dialog, "Ksenia") == "Added “Ksenia”."

    def test_spelling_rules_dont_promise_to_work_without_cleanup(self, make_dialog):
        dialog, _store = make_dialog({
            SettingsKey.TRANSCRIPT_CLEANUP_RULES: ['Always spell my name "Alex Rivera"'],
        })
        open_page(dialog)
        text = dialog.dictionary_rules_tile.description_label.text()
        assert "when AI cleanup is off" not in text
        assert text.startswith("One of your Learned rules only spells a word.")


class TestWhereWordsDoCount:
    @pytest.mark.parametrize("values", [
        {CLEANUP: True},
        {SettingsKey.SELECTED_MODEL: "local_whisper"},
    ])
    def test_no_notice_and_a_plain_confirmation(self, make_dialog, values):
        dialog, _store = make_dialog(values)
        open_page(dialog)
        assert dialog.dictionary_cleanup_gate_tile.isHidden()
        assert add(dialog, "Ksenia") == "Added “Ksenia”."

    def test_engines_that_take_hints_keep_the_switch(self, make_dialog):
        dialog, store = make_dialog({SettingsKey.SELECTED_MODEL: "local_whisper"})
        open_page(dialog)
        assert dialog.dictionary_steer_tile.isEnabled()
        dialog.dictionary_steer_switch.click()
        assert store.load_all_settings()[SettingsKey.DICTIONARY_STEER_RECOGNITION] is False
        assert dialog.dictionary_steer_tile.description_label.text() == (
            "Off. Only Sounds like spellings are fixed while AI cleanup is off."
        )
        # Steering off: the word reaches neither the model nor cleanup.
        assert add(dialog, "Ksenia") == ADDED_HINT

    def test_a_spelling_rule_with_a_variant_still_works_without_cleanup(self, make_dialog):
        dialog, _store = make_dialog({
            SettingsKey.TRANSCRIPT_CLEANUP_RULES: ['Spell "jon" as "John".'],
        })
        open_page(dialog)
        assert "also works when AI cleanup is off" in dialog.dictionary_rules_tile.description_label.text()

    def test_an_unknown_remote_engine_claims_nothing(self, make_dialog):
        dialog, _store = make_dialog({SettingsKey.SELECTED_MODEL: "remote"})
        open_page(dialog)
        assert dialog.dictionary_cleanup_gate_tile.isHidden()
        assert dialog.dictionary_steer_tile.isEnabled()
        assert add(dialog, "Ksenia") == "Added “Ksenia”."


class TestBasicRow:
    def _detail(self, dialog):
        page = dialog._basic_pages[BASIC_DICTATION]
        return [label.text() for label in page.findChildren(QLabel)
                if label.objectName() == "basicSettingsDescription"]

    def test_default_setup_says_only_sounds_like_spellings_are_fixed(self, make_dialog):
        dialog, _store = make_dialog({SettingsKey.SETTINGS_VIEW: SettingsView.BASIC, KEY: [KSENIA]})
        assert "1 word · Only Sounds like spellings are fixed while AI cleanup is off." in self._detail(dialog)

    def test_without_words_it_says_what_a_word_needs(self, make_dialog):
        dialog, _store = make_dialog({SettingsKey.SETTINGS_VIEW: SettingsView.BASIC})
        assert ("Names and terms to get right. With AI cleanup off, each needs a "
                "Sounds like spelling.") in self._detail(dialog)

    def test_with_cleanup_on_it_just_counts(self, make_dialog):
        dialog, _store = make_dialog({
            SettingsKey.SETTINGS_VIEW: SettingsView.BASIC, KEY: [KSENIA], CLEANUP: True,
        })
        assert "1 word" in self._detail(dialog)


@pytest.mark.parametrize("ui_mode,width", [("classic", 940), ("omarchy", 560)])
def test_notice_fits_narrow_windows_at_large_fonts(make_dialog, monkeypatch, ui_mode, width):
    from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
    from ui_qt.utils.palette import current_palette, set_current_palette
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    previous_style, previous_font = app.styleSheet(), app.font()
    previous_scale, previous_palette = current_ui_font_scale_percent(), current_palette()
    dialog = None
    try:
        apply_ui_font_scale(130, app=app, theme_manager=ThemeManager("light"))
        dialog, _store = make_dialog()
        dialog.show()
        dialog.resize(width, 600)
        open_page(dialog)
        for _ in range(10):
            app.processEvents()
        page = dialog._pages[DICTIONARY]
        gate = dialog.dictionary_cleanup_gate_tile
        assert gate.isVisible()
        assert page.rect().contains(gate.geometry())
        for label in (gate.title_label, gate.description_label):
            assert label.height() >= label.heightForWidth(label.width())
        button = gate_button(dialog)
        assert button.mapTo(page, button.rect().bottomRight()).x() < page.width()
    finally:
        if dialog is not None:
            dialog.close()
        apply_ui_font_scale(previous_scale, app=app)
        set_current_palette(previous_palette)
        app.setFont(previous_font)
        app.setStyleSheet(previous_style)
