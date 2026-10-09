"""Settings → Voice model: the "Languages I dictate in" chips."""
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractButton, QApplication, QLabel

from services.settings import SettingsKey, SettingsView, settings_manager
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import VOICE_MODEL
from ui_qt.widgets.local_engine_controls import DictationLanguagesField


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
        return dialog, dialog.models.dictation_languages

    yield build
    for dialog in dialogs:
        dialog.close()
    stack.close()


def _chip_names(field):
    return [chip.findChild(QLabel).text() for chip in field.chips]


class TestField:
    def test_adding_languages_saves_them_and_makes_the_first_active(self, make_dialog):
        dialog, field = make_dialog({SettingsKey.SELECTED_MODEL: "local_whisper"})
        dialog.notify_changed = MagicMock()
        assert field.chips == []
        assert field.caption.text().startswith("Dictation detects the language. Add languages")

        field.add("en")
        assert settings_manager.get(SettingsKey.DICTATION_LANGUAGES) == ["en"]
        assert settings_manager.get(SettingsKey.DICTATION_ACTIVE_LANGUAGE) == "en"
        assert field.caption.text() == "Dictation uses English. Add another to switch between them."

        field.add("es")
        assert _chip_names(field) == ["English", "Spanish"]
        assert field.caption.text().startswith("Now dictating in English.")
        assert [chip.property("active") for chip in field.chips] == [True, False]
        assert dialog.notify_changed.call_args_list == [(("languages",),)] * 2

    def test_removing_the_active_language_moves_to_the_next(self, make_dialog):
        _dialog, field = make_dialog({
            SettingsKey.SELECTED_MODEL: "local_whisper",
            SettingsKey.DICTATION_LANGUAGES: ["en", "es", "fr"],
            SettingsKey.DICTATION_ACTIVE_LANGUAGE: "en",
        })
        QTest.mouseClick(field.chips[0].remove_button, Qt.MouseButton.LeftButton)
        assert settings_manager.get(SettingsKey.DICTATION_LANGUAGES) == ["es", "fr"]
        assert settings_manager.get(SettingsKey.DICTATION_ACTIVE_LANGUAGE) == "es"
        assert _chip_names(field) == ["Spanish", "French"]

    def test_add_menu_offers_only_what_the_engine_takes(self, make_dialog):
        _dialog, field = make_dialog({
            SettingsKey.SELECTED_MODEL: "parakeet",
            SettingsKey.DICTATION_LANGUAGES: ["en"],
        })
        field._open_add_menu()
        try:
            assert [action.text() for action in field.add_menu.actions()] == [
                "Russian", "Spanish", "French", "Portuguese",
            ]
        finally:
            field.add_menu.close()

    def test_languages_the_engine_cannot_use_stay_saved_but_muted(self, make_dialog):
        _dialog, field = make_dialog({
            SettingsKey.SELECTED_MODEL: "parakeet",
            SettingsKey.DICTATION_LANGUAGES: ["en", "de"],
            SettingsKey.DICTATION_ACTIVE_LANGUAGE: "en",
        })
        assert [chip.property("available") for chip in field.chips] == [True, False]
        assert field.chips[1].toolTip() == "Parakeet can't use this language."
        assert field.caption.text() == "Dictation uses English. Add another to switch between them."

    @pytest.mark.parametrize("backend,reason", [
        ("moonshine", "Moonshine understands English only."),
        ("parakeet_mlx", "Parakeet MLX detects the language by itself."),
    ])
    def test_single_language_engines_show_why_instead(self, make_dialog, backend, reason):
        _dialog, field = make_dialog({SettingsKey.DICTATION_LANGUAGES: ["en", "es"]})
        field.set_backend(backend)
        assert field.chip_area.isHidden()
        assert field.caption.text() == reason
        assert settings_manager.get(SettingsKey.DICTATION_LANGUAGES) == ["en", "es"]

    def test_engine_switch_on_the_page_updates_the_field(self, make_dialog):
        dialog, field = make_dialog({
            SettingsKey.SELECTED_MODEL: "local_whisper",
            SettingsKey.DICTATION_LANGUAGES: ["en", "es"],
        })
        assert not field.chip_area.isHidden()
        dialog.models._update_ondemand_whisper_enabled = MagicMock(
            wraps=dialog.models._update_ondemand_whisper_enabled
        )
        combo = dialog.models.engine_combo
        index = combo.findData("moonshine")
        if index < 0:
            pytest.skip("Moonshine is not offered on this platform")
        combo.setCurrentIndex(index)
        assert field.chip_area.isHidden()
        assert field.caption.text() == "Moonshine understands English only."

    def test_never_writes_the_engine_language(self, make_dialog):
        _dialog, field = make_dialog({
            SettingsKey.SELECTED_MODEL: "parakeet",
            SettingsKey.LOCAL_ASR_LANGUAGE: "ru",
        })
        field.add("es")
        field.add("fr")
        assert settings_manager.get(SettingsKey.LOCAL_ASR_LANGUAGE) == "ru"


@pytest.mark.parametrize("ui_mode,width", [("classic", 720), ("omarchy", 460)])
def test_chips_wrap_and_fit_narrow_windows_at_large_fonts(make_dialog, monkeypatch, ui_mode, width):
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
        dialog, field = make_dialog({
            SettingsKey.SELECTED_MODEL: "local_whisper",
            SettingsKey.DICTATION_LANGUAGES: ["en", "es", "fr", "de", "pt", "ja", "uk"],
            SettingsKey.DICTATION_ACTIVE_LANGUAGE: "fr",
        })
        dialog.show()
        dialog._fit_to_screen()
        dialog.resize(width, 600)
        for _ in range(10):
            app.processEvents()
        page = dialog._page_scrolls[VOICE_MODEL].widget()
        assert field.chip_area.height() > field.chips[0].height() * 2
        for control in field.findChildren(QAbstractButton):
            if control.isVisible():
                assert control.mapTo(page, control.rect().topLeft()).x() >= 0
                assert control.mapTo(page, control.rect().bottomRight()).x() < page.width(), (
                    control.objectName() or control.text())
        chip_area = field.chip_area
        for chip in field.chips:
            assert chip.geometry().bottom() < chip_area.height()
        assert field.caption.height() >= field.caption.heightForWidth(field.caption.width())
    finally:
        if dialog is not None:
            dialog.close()
        apply_ui_font_scale(previous_scale, app=app)
        set_current_palette(previous_palette)
        app.setFont(previous_font)
        app.setStyleSheet(previous_style)


def test_field_works_outside_settings():
    settings_manager.update_settings({
        SettingsKey.SELECTED_MODEL: "api",
        SettingsKey.DICTATION_LANGUAGES: ["de", "it"],
        SettingsKey.DICTATION_ACTIVE_LANGUAGE: "it",
    })
    field = DictationLanguagesField()
    assert _chip_names(field) == ["German", "Italian"]
    assert field.caption.text().startswith("Now dictating in Italian.")
