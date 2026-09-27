"""The Live preview checkbox in the engine card footer.

It mirrors Settings → Recording → Live preview: same key, same legacy-key
drops, and the runtime is reconfigured through the same hook.
"""
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from config import config
from services.settings import LEGACY_STREAMING_KEYS, SettingsKey
from ui_qt.widgets.quick_record_tab import QuickRecordTab
from ui_qt.widgets.upload_file_tab import UploadFileTab


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _settings(stored=None):
    """A stand-in settings manager so tests never touch the real file."""
    stored = {} if stored is None else stored
    manager = MagicMock()
    manager.get.side_effect = lambda key, default=None: stored.get(key, default)
    manager.load_all_settings.return_value = dict(stored)
    return manager


def _shown(widget):
    widget.resize(680, 500)
    widget.show()
    QApplication.processEvents()
    return widget


class TestFooterPlacement:
    def test_quick_record_shows_the_checkbox_beside_ai_cleanup(self):
        tab = _shown(QuickRecordTab())
        try:
            assert tab.live_preview_check.isVisible()
            assert tab.live_preview_check.text() == "Live preview"
            assert tab.engine_card.isAncestorOf(tab.live_preview_check)
            cleanup_x = tab.cleanup_check.mapTo(tab, tab.cleanup_check.rect().topLeft()).x()
            preview_x = tab.live_preview_check.mapTo(
                tab, tab.live_preview_check.rect().topLeft()
            ).x()
            assert cleanup_x < preview_x
        finally:
            tab.close()

    def test_upload_tab_has_no_dictation_preview_so_it_hides_the_checkbox(self):
        tab = _shown(UploadFileTab())
        try:
            assert not tab.live_preview_check.isVisible()
            assert tab.cleanup_check.isVisible()
        finally:
            tab.close()

    def test_recording_locks_the_toggle_with_the_backend_choice(self):
        tab = QuickRecordTab()
        tab.is_recording = True
        tab._update_recording_state()
        assert not tab.live_preview_check.isEnabled()
        assert not tab.model_combo.isEnabled()
        tab.is_recording = False
        tab._update_recording_state()
        assert tab.live_preview_check.isEnabled()


class TestPersistence:
    def test_loads_the_saved_streaming_setting(self):
        manager = _settings({SettingsKey.STREAMING_ENABLED: True})
        with patch("ui_qt.widgets.transcription_tab_base.settings_manager", manager):
            tab = QuickRecordTab()
        assert tab.live_preview_check.isChecked()

    def test_defaults_to_config_when_nothing_is_saved(self):
        manager = _settings()
        with patch("ui_qt.widgets.transcription_tab_base.settings_manager", manager):
            tab = QuickRecordTab()
        assert tab.live_preview_check.isChecked() is bool(config.STREAMING_ENABLED)

    def test_toggle_writes_the_settings_key_and_drops_legacy_switches(self):
        manager = _settings()
        with patch("ui_qt.widgets.transcription_tab_base.settings_manager", manager):
            tab = QuickRecordTab()
            emitted = []
            tab.live_preview_changed.connect(lambda: emitted.append(True))
            tab.live_preview_check.setChecked(True)

        manager.update_settings.assert_called_once_with(
            {SettingsKey.STREAMING_ENABLED: True},
            remove=LEGACY_STREAMING_KEYS,
        )
        assert emitted == [True]

    def test_reload_from_settings_does_not_write_or_emit(self):
        manager = _settings({SettingsKey.STREAMING_ENABLED: True})
        with patch("ui_qt.widgets.transcription_tab_base.settings_manager", manager):
            tab = QuickRecordTab()
            emitted = []
            tab.live_preview_changed.connect(lambda: emitted.append(True))
            tab.load_live_preview_setting()

        assert tab.live_preview_check.isChecked()
        manager.update_settings.assert_not_called()
        assert emitted == []

    def test_failed_save_reverts_the_checkbox(self):
        manager = _settings({SettingsKey.STREAMING_ENABLED: False})
        manager.update_settings.side_effect = OSError("disk full")
        with patch("ui_qt.widgets.transcription_tab_base.settings_manager", manager):
            tab = QuickRecordTab()
            emitted = []
            tab.live_preview_changed.connect(lambda: emitted.append(True))
            tab.live_preview_check.setChecked(True)

        assert not tab.live_preview_check.isChecked()
        assert emitted == []


def _remote_models(family, host="jed"):
    from transcriber.remote_backend import HostModel, RemoteModels

    current = HostModel(family, "model", "Model") if family else None
    return RemoteModels(host=host, models=(), current=current)


class TestEngineSupport:
    """An engine that can't preview grays the box out without losing the choice."""

    def _tab(self, manager):
        with patch("ui_qt.widgets.transcription_tab_base.settings_manager", manager):
            return QuickRecordTab()

    def test_api_grays_out_unchecked_and_says_why(self):
        manager = _settings({SettingsKey.STREAMING_ENABLED: True})
        tab = self._tab(manager)
        tab.set_backend("API")
        check = tab.live_preview_check
        assert not check.isEnabled()
        assert not check.isChecked()
        assert check.toolTip().startswith(
            "Live preview needs Local Whisper, Parakeet, or Nemotron Streaming."
        )
        assert "turns back on" in check.toolTip()
        manager.update_settings.assert_not_called()

    def test_switching_back_restores_the_saved_choice(self):
        tab = self._tab(_settings({SettingsKey.STREAMING_ENABLED: True}))
        tab.set_backend("Moonshine")
        assert not tab.live_preview_check.isChecked()
        tab.set_backend("Parakeet")
        assert tab.live_preview_check.isEnabled()
        assert tab.live_preview_check.isChecked()
        assert tab.live_preview_check.toolTip().startswith("Show draft text")

    def test_saved_off_says_how_to_use_it_instead(self):
        tab = self._tab(_settings({SettingsKey.STREAMING_ENABLED: False}))
        tab.set_backend("API")
        assert "Switch to one to use it" in tab.live_preview_check.toolTip()

    def test_remote_host_without_a_preview_engine_names_the_host(self):
        tab = self._tab(_settings({SettingsKey.STREAMING_ENABLED: True}))
        tab.set_backend("Remote computer")
        tab.set_remote_models(_remote_models("local_whisper"))
        check = tab.live_preview_check
        assert not check.isEnabled()
        assert not check.isChecked()
        assert check.toolTip().startswith(
            "Live preview needs Parakeet or Nemotron Streaming on jed."
        )

    def test_remote_host_switching_to_parakeet_turns_it_back_on(self):
        tab = self._tab(_settings({SettingsKey.STREAMING_ENABLED: True}))
        tab.set_backend("Remote computer")
        tab.set_remote_models(_remote_models("local_whisper"))
        tab.set_remote_models(_remote_models("parakeet"))
        assert tab.live_preview_check.isEnabled()
        assert tab.live_preview_check.isChecked()

    def test_remote_not_connected_yet_keeps_the_choice(self):
        # The host decides once it connects; graying out until then would
        # flicker on every start.
        tab = self._tab(_settings({SettingsKey.STREAMING_ENABLED: True}))
        tab.set_backend("Remote computer")
        tab.set_remote_models(_remote_models(None))
        assert tab.live_preview_check.isEnabled()
        assert tab.live_preview_check.isChecked()

    def test_ending_a_recording_does_not_unlock_an_engine_that_cannot_preview(self):
        tab = self._tab(_settings({SettingsKey.STREAMING_ENABLED: True}))
        tab.set_backend("API")
        tab.is_recording = True
        tab._update_recording_state()
        tab.is_recording = False
        tab._update_recording_state()
        assert not tab.live_preview_check.isEnabled()


class TestMainWindowRelay:
    def _main_window(self):
        from ui_qt.main_window import MainWindow

        with patch(
            "ui_qt.dialogs.meeting_intro_dialog.maybe_show_meeting_mode_intro",
            return_value=False,
        ):
            return MainWindow()

    def test_toggle_syncs_the_other_tab_and_emits_once(self):
        stored = {SettingsKey.STREAMING_ENABLED: False}
        manager = _settings(stored)

        def update(updates, remove=()):
            stored.update(updates)
            return dict(stored)

        manager.update_settings.side_effect = update
        with patch("ui_qt.widgets.transcription_tab_base.settings_manager", manager), \
                patch("ui_qt.main_window.settings_manager", manager):
            window = self._main_window()
            try:
                emitted = []
                window.live_preview_changed.connect(lambda: emitted.append(True))
                window.quick_record_tab.live_preview_check.setChecked(True)
                assert emitted == [True]
                # The hidden Upload copy follows so a future tab flip agrees.
                assert window.upload_file_tab.live_preview_check.isChecked()
            finally:
                window.close()


class TestUiControllerHooks:
    """The controller's callback attributes, exercised without a real window."""

    def _controller(self):
        from ui_qt.ui_controller import UIController

        controller = UIController.__new__(UIController)
        controller.main_window = MagicMock()
        controller.on_streaming_settings_changed = MagicMock()
        return controller

    def test_tab_toggle_reconfigures_streaming(self):
        controller = self._controller()
        controller._on_live_preview_changed()
        controller.on_streaming_settings_changed.assert_called_once_with()

    def test_settings_dialog_toggle_refreshes_tabs_then_reconfigures(self):
        controller = self._controller()
        controller._on_settings_streaming_changed()
        controller.main_window.refresh_live_preview_controls.assert_called_once_with()
        controller.on_streaming_settings_changed.assert_called_once_with()
