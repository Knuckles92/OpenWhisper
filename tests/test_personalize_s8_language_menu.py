"""Switching the dictation language from the tray, the shortcut and the overlay chip."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QMenu

from services import dictation_language
from services.settings import SettingsKey, settings_manager
from ui_qt.dialogs.settings_destinations import VOICE_MODEL
from ui_qt.overlays.waveform_overlay import WaveformOverlay
from ui_qt.widgets import language_menu


@pytest.fixture
def ui():
    return SimpleNamespace(
        overlay=MagicMock(),
        set_status=MagicMock(),
        open_settings_destination=MagicMock(),
    )


def _save(engine="local_whisper", chosen=("en", "es", "fr"), active="en"):
    settings_manager.update_settings({
        SettingsKey.SELECTED_MODEL: engine,
        SettingsKey.DICTATION_LANGUAGES: list(chosen),
        SettingsKey.DICTATION_ACTIVE_LANGUAGE: active,
    })


def _texts(menu):
    return [action.text() for action in menu.actions() if not action.isSeparator()]


class TestMenu:
    def test_lists_the_choices_with_the_active_one_checked(self, ui):
        _save(active="es")
        menu = QMenu()
        language_menu.populate(menu, ui)
        assert _texts(menu) == ["English", "Spanish", "French", "Choose languages…"]
        checked = [action.text() for action in menu.actions() if action.isChecked()]
        assert checked == ["Spanish"]

    def test_choosing_one_makes_it_active_and_says_so(self, ui):
        _save()
        menu = QMenu()
        language_menu.populate(menu, ui)
        next(action for action in menu.actions() if action.text() == "French").trigger()
        assert settings_manager.get(SettingsKey.DICTATION_ACTIVE_LANGUAGE) == "fr"
        ui.overlay.set_language.assert_called_once_with("fr", ["en", "es", "fr"])
        ui.overlay.show_language_notice.assert_not_called()
        ui.set_status.assert_called_once_with("Dictation language: French")

    def test_single_language_engines_explain_themselves(self, ui):
        _save("moonshine")
        menu = QMenu()
        language_menu.populate(menu, ui)
        assert _texts(menu) == ["Moonshine understands English only.", "Choose languages…"]
        assert not menu.actions()[0].isEnabled()

    def test_choose_languages_opens_the_voice_model_page(self, ui):
        menu = QMenu()
        language_menu.populate(menu, ui)
        menu.actions()[-1].trigger()
        ui.open_settings_destination.assert_called_once_with(VOICE_MODEL)

    def test_a_broken_language_list_still_leaves_a_usable_menu(self, ui, monkeypatch):
        monkeypatch.setattr(dictation_language, "language_choices", MagicMock(side_effect=ValueError))
        menu = QMenu()
        language_menu.populate(menu, ui)
        assert _texts(menu) == ["No other languages", "Choose languages…"]


class TestCycle:
    def test_cycle_moves_on_and_confirms_near_the_pointer(self, ui):
        _save()
        ui.overlay.isVisible.return_value = False
        language_menu.cycle(ui)
        assert settings_manager.get(SettingsKey.DICTATION_ACTIVE_LANGUAGE) == "es"
        ui.overlay.set_language.assert_called_once_with("es", ["en", "es", "fr"])
        ui.overlay.show_language_notice.assert_called_once_with()
        ui.set_status.assert_called_once_with("Dictation language: Spanish")

    def test_while_recording_the_chip_updates_in_place(self, ui):
        _save()
        ui.overlay.isVisible.return_value = True
        language_menu.cycle(ui)
        ui.overlay.show_language_notice.assert_not_called()
        ui.overlay.set_language.assert_called_once()

    @pytest.mark.parametrize("engine,chosen,status", [
        ("moonshine", ("en", "es"), "Moonshine understands English only."),
        ("local_whisper", ("es",), "Add another language in Settings → Voice model to switch"),
    ])
    def test_nothing_to_switch_to_says_why(self, ui, engine, chosen, status):
        _save(engine, chosen, active=chosen[0])
        language_menu.cycle(ui)
        ui.set_status.assert_called_once_with(status)
        ui.overlay.set_language.assert_not_called()
        assert settings_manager.get(SettingsKey.DICTATION_ACTIVE_LANGUAGE) == chosen[0]

    def test_language_notice_on_a_real_overlay(self, ui):
        _save(active="fr")
        overlay = WaveformOverlay()
        ui.overlay = overlay
        try:
            language_menu.cycle(ui)
            assert overlay.isVisible()
            assert overlay.current_state == overlay.STATE_LANGUAGE
            assert overlay._language == "en"

            overlay.animation_time = 0.5
            language_menu.cycle(ui)
            assert overlay.current_state == overlay.STATE_LANGUAGE
            assert overlay._language == "es"
            assert overlay.animation_time == 0.0
            assert overlay.hidden_timer.isActive()
        finally:
            overlay.hide()
            overlay.close()


class TestControllerWiring:
    @pytest.fixture
    def app_ui(self):
        from ui_qt.ui_controller import UIController

        controller = UIController()
        yield controller
        controller.cleanup()

    def test_overlay_chip_and_shortcut_cycle_the_language(self, app_ui, monkeypatch):
        calls = []
        monkeypatch.setattr(language_menu, "cycle", lambda ui: calls.append(ui))
        app_ui.overlay.language_cycle_requested.emit()
        app_ui.cycle_dictation_language()
        assert calls == [app_ui, app_ui]

    def test_remote_engine_reports_reach_the_language_choices(self, app_ui):
        _save("remote", ("en", "de", "es"))
        from transcriber.remote_backend import RemoteModels

        runtime = {"family": "parakeet", "languages": ["en", "es", "auto"], "selected": {"language": "en"}}
        app_ui.get_remote_models = lambda: RemoteModels("Desk", runtime=runtime, engine={"family": "parakeet"})
        try:
            app_ui.refresh_remote_models()
            assert dictation_language.language_choices(settings_manager.load_all_settings()) == ["en", "es"]
        finally:
            dictation_language.note_remote_runtime(None)
