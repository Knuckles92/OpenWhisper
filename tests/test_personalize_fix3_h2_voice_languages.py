"""Voice model has one language control for dictation, and every surface agrees.

The engine's Language is what dictation uses until you pick languages you
dictate in; from then on the chips decide, and the engine combo steps aside
on the pages that dictate. Adding a language never changes, unannounced, the
language the next dictation uses.
"""
import os
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QMenu

from services import dictation_language, dictation_pipeline
from services.settings import SettingsKey, SettingsView, settings_manager
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import VOICE_MODEL
from ui_qt.widgets import language_menu
from ui_qt.widgets.local_engine_controls import DictationLanguagesField, LocalEngineControls


@pytest.fixture
def make_dialog():
    """Settings on the test's own store, with no model cache to scan."""
    dialogs = []
    stack = ExitStack()
    for module in (models_module, downloads_module):
        stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
    stack.enter_context(patch.object(settings_dialog_module.history_manager, "set_retention"))
    stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))

    def build(values=None):
        settings_manager.update_settings({SettingsKey.SETTINGS_VIEW: SettingsView.ADVANCED, **(values or {})})
        dialog = settings_dialog_module.SettingsDialog(
            get_loaded_model=lambda: None, background_cache_scan=False
        )
        dialog.select_destination(VOICE_MODEL)
        dialogs.append(dialog)
        return dialog.models

    yield build
    for dialog in dialogs:
        dialog.close()
    stack.close()


def _saved(*keys):
    settings = settings_manager.load_all_settings()
    return [settings.get(key) for key in keys]


class TestAddingKeepsTheCurrentLanguage:
    def test_first_language_joins_the_engines_own(self):
        settings_manager.update_settings({
            SettingsKey.SELECTED_MODEL: "parakeet",
            SettingsKey.LOCAL_ASR_LANGUAGE: "en",
        })
        field = DictationLanguagesField()
        field.add("es")
        settings = settings_manager.load_all_settings()
        assert settings[SettingsKey.DICTATION_LANGUAGES] == ["en", "es"]
        assert settings[SettingsKey.DICTATION_ACTIVE_LANGUAGE] == "en"
        assert settings[SettingsKey.LOCAL_ASR_LANGUAGE] == "en"
        assert dictation_pipeline.recognition_for(None, settings).language == "en"
        assert field.caption.text().startswith("Now dictating in English.")

    def test_adding_the_engines_own_language_adds_it_once(self):
        settings_manager.update_settings({
            SettingsKey.SELECTED_MODEL: "parakeet",
            SettingsKey.LOCAL_ASR_LANGUAGE: "en",
        })
        DictationLanguagesField().add("en")
        assert _saved(SettingsKey.DICTATION_LANGUAGES) == [["en"]]

    @pytest.mark.parametrize("chosen,expected", [
        # Chosen on another engine; Parakeet can use neither.
        (["de", "it"], ["de", "it", "fr", "es"]),
        # French is chosen but not active, so the engine's French is in effect.
        (["pt", "fr"], ["pt", "fr", "es"]),
    ])
    def test_the_language_in_effect_stays_in_effect(self, chosen, expected):
        settings_manager.update_settings({
            SettingsKey.SELECTED_MODEL: "parakeet",
            SettingsKey.LOCAL_ASR_LANGUAGE: "fr",
            SettingsKey.DICTATION_LANGUAGES: chosen,
            SettingsKey.DICTATION_ACTIVE_LANGUAGE: "de",
        })
        before = dictation_language.current_language(settings_manager.load_all_settings())
        DictationLanguagesField().add("es")
        settings = settings_manager.load_all_settings()
        assert settings[SettingsKey.DICTATION_LANGUAGES] == expected
        assert before == dictation_language.current_language(settings) == "fr"
        assert dictation_pipeline.recognition_for(None, settings).language == "fr"

    def test_once_the_chips_decide_adding_changes_nothing_else(self):
        settings_manager.update_settings({
            SettingsKey.SELECTED_MODEL: "parakeet",
            SettingsKey.LOCAL_ASR_LANGUAGE: "en",
            SettingsKey.DICTATION_LANGUAGES: ["es", "fr"],
            SettingsKey.DICTATION_ACTIVE_LANGUAGE: "fr",
        })
        DictationLanguagesField().add("pt")
        assert _saved(SettingsKey.DICTATION_LANGUAGES, SettingsKey.DICTATION_ACTIVE_LANGUAGE) == [
            ["es", "fr", "pt"], "fr",
        ]

    @pytest.mark.parametrize("values", [
        {SettingsKey.SELECTED_MODEL: "parakeet", SettingsKey.LOCAL_ASR_LANGUAGE: "auto"},
        {SettingsKey.SELECTED_MODEL: "local_whisper"},
    ])
    def test_an_engine_that_detects_the_language_says_what_changes(self, values):
        settings_manager.update_settings(values)
        field = DictationLanguagesField()
        assert field.caption.text() == (
            "Dictation detects the language. Add languages to switch between them "
            "from the overlay, the tray or a shortcut."
        )
        field.add("es")
        assert _saved(SettingsKey.DICTATION_LANGUAGES, SettingsKey.DICTATION_ACTIVE_LANGUAGE) == [["es"], "es"]
        assert field.caption.text() == "Dictation uses Spanish. Add another to switch between them."

    def test_empty_list_names_the_language_dictation_uses(self):
        settings_manager.update_settings({
            SettingsKey.SELECTED_MODEL: "parakeet",
            SettingsKey.LOCAL_ASR_LANGUAGE: "ru",
        })
        assert DictationLanguagesField().caption.text() == (
            "Dictation uses Russian. Add languages to switch between them "
            "from the overlay, the tray or a shortcut."
        )

    def test_choices_the_engine_cannot_use_name_the_fallback(self):
        settings_manager.update_settings({
            SettingsKey.SELECTED_MODEL: "parakeet",
            SettingsKey.LOCAL_ASR_LANGUAGE: "fr",
            SettingsKey.DICTATION_LANGUAGES: ["de", "it"],
        })
        assert DictationLanguagesField().caption.text() == (
            "Parakeet can't use these, so dictation uses French."
        )


class TestOneLanguageControl:
    def test_engine_language_steps_aside_while_the_chips_decide(self, make_dialog):
        models = make_dialog({
            SettingsKey.SELECTED_MODEL: "parakeet",
            SettingsKey.LOCAL_ASR_LANGUAGE: "en",
        })
        engine_field = models.speech_controls.language_field
        assert not engine_field.isHidden()

        models.dictation_languages.add("es")
        assert engine_field.isHidden()

        for code in ("en", "es"):
            models.dictation_languages._remove(code)
        assert not engine_field.isHidden()
        assert models.speech_controls.language_combo.currentData() == "en"

    def test_saved_choices_hide_it_when_the_page_opens(self, make_dialog):
        models = make_dialog({
            SettingsKey.SELECTED_MODEL: "parakeet",
            SettingsKey.DICTATION_LANGUAGES: ["en", "es"],
            SettingsKey.DICTATION_ACTIVE_LANGUAGE: "en",
        })
        assert models.speech_controls.language_field.isHidden()

    def test_an_active_language_this_engine_lacks_leaves_the_engine_in_charge(self, make_dialog):
        # German was active on Whisper; Parakeet can't use it, so the engine's
        # Russian is what dictation uses, and the page says so.
        models = make_dialog({
            SettingsKey.SELECTED_MODEL: "parakeet",
            SettingsKey.LOCAL_ASR_LANGUAGE: "ru",
            SettingsKey.DICTATION_LANGUAGES: ["en", "de", "fr"],
            SettingsKey.DICTATION_ACTIVE_LANGUAGE: "de",
        })
        assert not models.speech_controls.language_field.isHidden()
        assert models.dictation_languages.caption.text().startswith("Now dictating in Russian.")
        settings = settings_manager.load_all_settings()
        assert dictation_pipeline.recognition_for(None, settings).language == ""

    def test_single_language_engines_keep_their_own_field(self, make_dialog):
        models = make_dialog({
            SettingsKey.SELECTED_MODEL: "moonshine",
            SettingsKey.DICTATION_LANGUAGES: ["en", "es"],
        })
        assert not models.speech_controls.language_field.isHidden()
        assert models.dictation_languages.chip_area.isHidden()

    def test_upload_keeps_its_language_and_quick_record_follows_the_chips(self):
        settings_manager.update_settings({
            SettingsKey.SELECTED_MODEL: "parakeet",
            SettingsKey.DICTATION_LANGUAGES: ["en", "es"],
            SettingsKey.DICTATION_ACTIVE_LANGUAGE: "en",
        })
        files = LocalEngineControls()
        dictation = LocalEngineControls(dictation=True)
        for controls in (files, dictation):
            controls.set_backend("parakeet")
        assert not files.language_field.isHidden()
        assert dictation.language_field.isHidden()

        from ui_qt.widgets.quick_record_tab import QuickRecordTab
        from ui_qt.widgets.upload_file_tab import UploadFileTab

        for tab_type, hidden in ((QuickRecordTab, True), (UploadFileTab, False)):
            tab = tab_type()
            try:
                tab.set_model_selection("parakeet")
                assert tab.local_engine.language_field.isHidden() is hidden
            finally:
                tab.close()

    def test_a_languages_change_refreshes_the_main_window_fields(self):
        from ui_qt.ui_controller import UIController

        controller = UIController()
        try:
            controller.refresh_local_engine_controls = MagicMock()
            controller._on_settings_page_changed("languages")
            controller.refresh_local_engine_controls.assert_called_once_with()
        finally:
            controller.cleanup()


class TestTrayShowsWhatIsInEffect:
    @pytest.fixture
    def ui(self):
        return SimpleNamespace(overlay=MagicMock(), set_status=MagicMock(),
                               open_settings_destination=MagicMock())

    @pytest.mark.parametrize("values,name", [
        ({SettingsKey.SELECTED_MODEL: "local_whisper", SettingsKey.DICTATION_LANGUAGES: ["es"],
          SettingsKey.DICTATION_ACTIVE_LANGUAGE: "es"}, "Spanish"),
        ({SettingsKey.SELECTED_MODEL: "parakeet", SettingsKey.LOCAL_ASR_LANGUAGE: "en"}, "English"),
        ({SettingsKey.SELECTED_MODEL: "local_whisper"}, "Detect automatically"),
    ])
    def test_one_language_is_shown_checked(self, ui, values, name):
        settings_manager.update_settings(values)
        assert dictation_language.label(
            dictation_language.current_language(settings_manager.load_all_settings())
        ) == name
        menu = QMenu()
        language_menu.populate(menu, ui)
        first = menu.actions()[0]
        assert [action.text() for action in menu.actions() if not action.isSeparator()] == [
            name, "Choose languages…",
        ]
        assert first.isChecked() and not first.isEnabled()
