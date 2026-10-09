"""App shell for the personalization features: UIController hooks, menus, tray, overlay."""
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMenu

import services
from config import config
from services.settings import settings_manager
from ui_qt import history_actions
from ui_qt.dialogs import stats_dialog
from ui_qt.dialogs.settings_destinations import DICTIONARY
from ui_qt.overlay_state import OverlayState
from ui_qt.overlays import waveform_overlay
from ui_qt.ui_controller import UIController
from ui_qt.widgets import language_menu, scratchpad
from ui_qt.widgets.transcription_progress import ProgressStage, stage_for_overlay_state


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ui():
    """A controller with fakes in place of the window, overlay, and clipboard."""
    controller = UIController.__new__(UIController)
    controller.overlay = MagicMock()
    controller.main_window = MagicMock()
    controller.tray_manager = MagicMock()
    controller._temporary_clipboard = MagicMock()
    controller._settings_dialog = None
    controller.set_status = MagicMock()
    return controller


@pytest.fixture
def app_ui():
    controller = UIController()
    yield controller
    controller.cleanup()


def _language_in_effect():
    # With nothing to switch to, the menu still names the language in use,
    # or why there is none to pick (Parakeet MLX, the Apple Silicon default).
    from services import dictation_language

    settings = settings_manager.load_all_settings()
    reason = dictation_language.single_language_reason(settings)
    if reason:
        return reason
    return dictation_language.label(dictation_language.current_language(settings))


def _menu_texts(menu):
    return [action.text() for action in menu.actions() if not action.isSeparator()]


def _view_menu(window):
    return next(
        action.menu() for action in window.title_bar.menu_bar.actions()
        if action.text() == "View"
    )


def _install_module(monkeypatch, name, module):
    # ``from services import x`` reads the package attribute first, so both
    # must point at the fake whether or not the real module exists yet.
    monkeypatch.setitem(sys.modules, f"services.{name}", module)
    monkeypatch.setattr(services, name, module, raising=False)


class TestClipboardHooks:
    def test_staging_without_html_keeps_the_one_argument_call(self, ui):
        stage = ui._temporary_clipboard.stage_text
        assert ui.stage_transcript_for_paste("hello") is stage.return_value
        ui.stage_transcript_for_paste("hello", html="")
        ui.stage_transcript_for_paste("hello", None)
        assert [call.args for call in stage.call_args_list] == [("hello",)] * 3
        assert [call.kwargs for call in stage.call_args_list] == [{}] * 3

    def test_staging_with_html_passes_it_on(self, ui):
        ui.stage_transcript_for_paste("hello", html="<b>hello</b>")
        ui._temporary_clipboard.stage_text.assert_called_once_with("hello", html="<b>hello</b>")

    def test_staging_without_restore_passes_it_on(self, ui):
        ui.stage_transcript_for_paste("hello", restore=False)
        ui._temporary_clipboard.stage_text.assert_called_once_with("hello", restore=False)

    def test_capture_selection_hands_over_the_copy_shortcut_and_timeout(self, ui, monkeypatch):
        keys = types.ModuleType("services.synthetic_keys")
        keys.send_copy = MagicMock()
        _install_module(monkeypatch, "synthetic_keys", keys)
        monkeypatch.setattr(config, "COMMAND_SELECTION_TIMEOUT_MS", 700, raising=False)
        callback = MagicMock()

        ui.capture_selection(callback)
        ui.capture_selection(callback, timeout_ms=250)

        capture = ui._temporary_clipboard.capture_selection
        assert [call.kwargs for call in capture.call_args_list] == [
            {"send_copy": keys.send_copy, "callback": callback, "timeout_ms": 700},
            {"send_copy": keys.send_copy, "callback": callback, "timeout_ms": 250},
        ]
        keys.send_copy.assert_not_called()
        callback.assert_not_called()


class TestFeatureDelegation:
    @pytest.mark.parametrize("method,module,function", [
        ("toggle_scratchpad", scratchpad, "toggle"),
        ("paste_last_original", history_actions, "paste_last_original"),
        ("show_stats", stats_dialog, "show_stats"),
    ])
    def test_actions_hand_the_controller_to_their_module(self, ui, monkeypatch, method, module, function):
        target = MagicMock()
        monkeypatch.setattr(module, function, target)
        getattr(ui, method)()
        target.assert_called_once_with(ui)

    def test_language_menu_is_filled_by_its_module(self, ui, monkeypatch):
        populate = MagicMock()
        monkeypatch.setattr(language_menu, "populate", populate)
        menu = QMenu()
        ui.populate_language_menu(menu)
        populate.assert_called_once_with(menu, ui)

    @pytest.mark.parametrize("returned,expected", [
        (True, True), (False, False), (None, False), ("yes", False), (1, False),
    ])
    def test_scratchpad_insert_answers_with_a_real_bool(self, ui, monkeypatch, returned, expected):
        monkeypatch.setattr(scratchpad, "insert", MagicMock(return_value=returned))
        assert ui.insert_into_scratchpad("note") is expected
        scratchpad.insert.assert_called_once_with(ui, "note")

    def test_hands_free_and_device_switch_reach_the_overlay(self, ui):
        ui.set_hands_free(1)
        ui.overlay.set_hands_free.assert_called_once_with(True)

        ui.on_recording_device_switched("USB headset", "Laptop mic")
        ui.set_status.assert_called_once_with("Microphone disconnected — continuing on Laptop mic")
        ui.overlay.show_caption.assert_called_once_with("Switched to Laptop mic")

    def test_learned_term_says_so_and_refreshes_the_dictionary_page(self, ui):
        ui.on_dictionary_term_learned("Kubernetes")
        ui.set_status.assert_called_once_with('Added "Kubernetes" to your dictionary')

        ui._settings_dialog = MagicMock()
        ui.on_dictionary_term_learned("Kubernetes")
        ui._settings_dialog.refresh_page.assert_called_once_with(DICTIONARY)


class TestLanguageCycle:
    @pytest.fixture
    def languages(self, monkeypatch):
        module = types.ModuleType("services.dictation_language")

        def cycle(settings):
            settings["test_active_language"] = "de"

        module.cycle = MagicMock(side_effect=cycle)
        module.job_language = lambda settings: settings.get("test_active_language", "")
        module.current_language = module.job_language
        module.language_choices = lambda settings: ["en", "de"]
        module.single_language_reason = lambda settings: ""
        module.label = {"en": "English", "de": "German"}.get
        _install_module(monkeypatch, "dictation_language", module)
        yield module
        settings_manager.update_settings({}, remove=("test_active_language",))

    def test_cycle_saves_the_next_language_and_shows_it(self, ui, languages):
        ui.cycle_dictation_language()
        languages.cycle.assert_called_once()
        assert settings_manager.get("test_active_language") == "de"
        ui.overlay.set_language.assert_called_once_with("de", ["en", "de"])
        ui.set_status.assert_called_once_with("Dictation language: German")

    def test_cycle_failure_is_reported_and_leaves_the_overlay_alone(self, ui, languages):
        languages.cycle.side_effect = ValueError("bad list")
        ui.cycle_dictation_language()
        ui.overlay.set_language.assert_not_called()
        ui.set_status.assert_called_once_with("Couldn't switch the dictation language")


class TestEmptyStates:
    def test_entry_points_do_nothing_without_their_target(self, ui):
        assert scratchpad.insert(types.SimpleNamespace(), "note") is False
        for action in (history_actions.paste_last_original,):
            action(ui)
        assert [call.args[0] for call in ui.set_status.call_args_list] == [
            "Nothing to paste yet",
        ]

    def test_language_menu_replaces_old_entries(self, ui):
        menu = QMenu()
        menu.addAction("Stale")
        language_menu.populate(menu, ui)
        language_menu.populate(menu, ui)
        assert _menu_texts(menu) == [_language_in_effect(), "Choose languages…"]
        assert not menu.actions()[0].isEnabled()


class TestAppWiring:
    def test_settings_changes_reach_a_handler_assigned_later(self, app_ui):
        app_ui.on_settings_changed("dictionary")
        app_ui._settings_dialog = MagicMock()
        dialog = app_ui._prepare_settings_dialog()
        handler = MagicMock()
        app_ui.on_settings_changed = handler
        dialog.on_settings_changed("hotkeys")
        handler.assert_called_once_with("hotkeys")

    def test_tray_offers_the_new_actions_and_routes_them(self, app_ui, monkeypatch):
        tray = app_ui.tray_manager
        texts = _menu_texts(tray.menu)
        for text in ("Copy original of last dictation", "Language", "Scratchpad", "Stats"):
            assert text in texts
        assert texts.index("Start Recording") < texts.index("Copy original of last dictation")
        assert texts.index("Scratchpad") < texts.index("Settings")
        assert _menu_texts(tray.language_menu) == [_language_in_effect(), "Choose languages…"]

        calls = []
        monkeypatch.setattr(scratchpad, "toggle", lambda ui: calls.append(("scratchpad", ui)))
        monkeypatch.setattr(stats_dialog, "show_stats", lambda ui: calls.append(("stats", ui)))
        monkeypatch.setattr(
            history_actions, "copy_last_original", lambda ui: calls.append(("original", ui))
        )
        monkeypatch.setattr(
            language_menu, "populate", lambda menu, ui: calls.append(("language", menu, ui))
        )
        tray.scratchpad_action.trigger()
        tray.stats_action.trigger()
        tray.copy_original_action.trigger()
        tray.language_menu.aboutToShow.emit()
        assert calls == [
            ("scratchpad", app_ui),
            ("stats", app_ui),
            ("original", app_ui),
            ("language", tray.language_menu, app_ui),
        ]

    def test_view_menu_opens_the_scratchpad_and_stats(self, app_ui, monkeypatch):
        window = app_ui.main_window
        texts = _menu_texts(_view_menu(window))
        assert texts.index("Host Mode") < texts.index("Scratchpad") < texts.index("Stats")
        assert texts.index("Stats") < texts.index("Open Meeting Dashboard")

        calls = []
        monkeypatch.setattr(scratchpad, "toggle", lambda ui: calls.append("scratchpad"))
        monkeypatch.setattr(stats_dialog, "show_stats", lambda ui: calls.append("stats"))
        window.scratchpad_action.trigger()
        window.stats_action.trigger()
        assert calls == ["scratchpad", "stats"]

    def test_command_listening_records_and_rewriting_cleans_up_in_their_own_looks(self, app_ui):
        overlay = app_ui.overlay
        tab = app_ui.main_window.quick_record_tab
        app_ui.set_overlay_state(OverlayState.COMMAND_LISTENING)
        assert app_ui.tray_manager.toggle_action.text() == "Stop Recording"
        assert overlay.isVisible()
        assert overlay.current_state == overlay.STATE_COMMAND_LISTENING
        assert tab.resolved_label.text() == "Listening for an edit…"

        app_ui.set_overlay_state(OverlayState.REWRITING)
        assert app_ui.tray_manager.toggle_action.text() == "Start Recording"
        assert overlay.current_state == overlay.STATE_REWRITING
        assert tab.resolved_label.text() == "Rewriting…"

        app_ui.set_overlay_state(OverlayState.NONE)
        assert tab.resolved_label.text() != "Rewriting…"
        # A finished rewrite fades out first (see test_recording_look).
        assert overlay.isVisible() and overlay._fading_since is not None
        overlay._fading_since -= waveform_overlay._FADE_OUT_S
        overlay._update_animation()
        assert not overlay.isVisible()

    def test_new_overlay_hooks_change_nothing_yet(self, app_ui):
        overlay = app_ui.overlay
        state = overlay.current_state
        overlay.set_hands_free(True)
        overlay.set_language("de", ["en", "de"])
        overlay.show_caption("Switched to Laptop mic")
        assert not overlay.isVisible()
        assert overlay.current_state == state


def test_progress_stages_for_the_new_overlay_states():
    assert stage_for_overlay_state(OverlayState.REWRITING) is ProgressStage.CLEANING
    assert stage_for_overlay_state(OverlayState.COMMAND_LISTENING) is None


def test_omarchy_header_carries_the_same_view_actions(monkeypatch, tmp_path):
    from ui_qt.main_window import MainWindow
    from ui_qt.widgets.desktop_header import DesktopHeader

    monkeypatch.setenv("OPENWHISPER_UI", "omarchy")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    window = MainWindow()
    try:
        assert isinstance(window.title_bar, DesktopHeader)
        view = _view_menu(window)
        assert window.scratchpad_action in view.actions()
        assert window.stats_action in view.actions()
    finally:
        window._force_quit = True
        window.close()
