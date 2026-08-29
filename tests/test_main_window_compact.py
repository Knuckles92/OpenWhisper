import pytest
import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication

from config import config
from services.settings import SettingsKey, settings_manager
from ui_qt.dialogs.settings_dialog import is_native_wayland_session
from ui_qt.main_window import MainWindow


def test_native_wayland_session_detection():
    assert is_native_wayland_session("linux", {"XDG_SESSION_TYPE": "wayland"})
    assert is_native_wayland_session("linux", {"WAYLAND_DISPLAY": "wayland-0"})
    assert not is_native_wayland_session("linux", {"XDG_SESSION_TYPE": "x11"})
    assert not is_native_wayland_session("win32", {"XDG_SESSION_TYPE": "wayland"})


class TestMainWindowCompactMode:
    @pytest.fixture(scope="class", autouse=True)
    @classmethod
    def _qapp(cls):
        cls.app = QApplication.instance() or QApplication([])

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.load_settings = patch.object(
            settings_manager,
            "load_all_settings",
            return_value={},
        )
        self.get_setting = patch.object(
            settings_manager,
            "get",
            side_effect=lambda key, default=None: default,
        )
        self.save_setting = patch.object(settings_manager, "save_setting")
        self.load_settings.start()
        self.mock_get_setting = self.get_setting.start()
        self.saved_setting = self.save_setting.start()
        self.window = MainWindow()

    @pytest.fixture(autouse=True)
    def _teardown(self):
        yield
        self.window._force_quit = True
        self.window.close()
        self.app.processEvents()
        self.save_setting.stop()
        self.get_setting.stop()
        self.load_settings.stop()

    def test_compact_mode_uses_fixed_controller_and_restores_geometry(self):
        """Compact mode swaps the workspace without losing full geometry."""
        self.window.setGeometry(40, 50, 620, 600)
        full_geometry = self.window.geometry()

        self.window.set_compact_mode(True)

        assert self.window._compact_mode
        assert self.window.size().width() == config.MAIN_WINDOW_COMPACT_WIDTH
        assert self.window.size().height() == config.MAIN_WINDOW_COMPACT_HEIGHT
        assert self.window.minimumSizeHint().width() <= config.MAIN_WINDOW_COMPACT_WIDTH, True
        assert self.window.minimumSizeHint().height() <= config.MAIN_WINDOW_COMPACT_HEIGHT
        assert self.window.compact_controller.isVisibleTo(self.window)
        assert not self.window.tabbed_content.isVisibleTo(self.window)
        assert not self.window.history_edge_tab.isVisibleTo(self.window)
        assert not self.window.models_button.isVisibleTo(self.window)
        assert self.window.settings_button.text() == "Settings"

        self.window.set_compact_mode(False)

        assert not self.window._compact_mode
        assert self.window.geometry() == full_geometry
        assert self.window.tabbed_content.isVisibleTo(self.window)
        assert self.window.models_button.isVisibleTo(self.window)
        assert self.window.settings_button.text() == "Settings"

    def test_missing_system_tray_disables_tray_only_controls(self):
        self.window.set_tray_available(False)

        assert self.window.tray_button.isHidden()
        assert not self.window.minimize_to_tray_action.isVisible()

        with patch.object(self.window, "showMinimized") as minimize:
            self.window.minimize_to_tray()
        minimize.assert_called_once_with()

    def test_close_is_not_hidden_when_system_tray_is_unavailable(self):
        self.window.set_tray_available(False)
        event = QCloseEvent()

        self.window.closeEvent(event)

        assert event.isAccepted()

    def test_footer_model_manager_button_opens_manager(self):
        """Footer Model Manager button uses the existing open signal path."""
        opened = []
        self.window.model_manager_requested.connect(lambda: opened.append(True))

        self.window.models_button.click()

        assert opened == [True]
        assert self.window.models_button.text() == "Model Manager"

    def test_footer_settings_button_opens_settings(self):
        """Footer Settings button uses the existing open signal path."""
        opened = []
        self.window.settings_requested.connect(lambda: opened.append(True))

        self.window.settings_button.click()

        assert opened == [True]
        assert self.window.settings_button.text() == "Settings"

    def test_file_menu_downloads_opens_downloads(self):
        """File → Downloads uses the existing downloads signal path."""
        opened = []
        self.window.model_manager_requested.connect(opened.append)

        file_menu = self.window.title_bar.menu_bar.actions()[0].menu()
        downloads_action = next(
            action
            for action in file_menu.actions()
            if action.text() == "Downloads..."
        )
        downloads_action.trigger()

        assert opened == ["downloads"]

    def test_compact_controls_delegate_to_quick_record(self):
        """Compact controls use the existing recording signal path."""
        toggles = []
        canceled = []
        self.window.record_toggled.connect(toggles.append)
        self.window.record_canceled.connect(lambda: canceled.append(True))
        self.window.set_compact_mode(True)

        self.window.compact_controller.record_button.click()
        assert toggles == [True]
        assert not self.window.is_recording

        self.window.compact_controller.cancel_button.click()
        assert canceled == [True]
        assert not self.window.is_recording

    def test_compact_mode_selection_is_persisted(self):
        self.window.set_compact_mode(True)
        self.saved_setting.assert_any_call(SettingsKey.COMPACT_MODE, True)

        self.window.set_compact_mode(False)
        self.saved_setting.assert_any_call(SettingsKey.COMPACT_MODE, False)

    def test_persisted_compact_mode_is_restored(self):
        self.mock_get_setting.side_effect = (
            lambda key, default=None: True
            if key == SettingsKey.COMPACT_MODE
            else default
        )

        self.window._restore_compact_mode()

        assert self.window._compact_mode
        assert self.window.settings_button.text() == "Settings"

    def test_collapsed_transcript_caps_tall_saved_geometry_on_restore(self):
        """Collapsed startup does not reserve space for the hidden transcript."""
        saved_geometry = {
            "x": 10,
            "y": 10,
            "width": 700,
            "height": 900,
            "format": self.window._geometry_format,
            "history_expanded": False,
        }
        self.mock_get_setting.side_effect = (
            lambda key, default=None: saved_geometry
            if key == SettingsKey.WINDOW_GEOMETRY
            else default
        )

        self.window._restore_window_geometry()

        assert self.window.quick_record_tab.is_transcription_collapsed()
        assert self.window.height() == config.MAIN_WINDOW_COLLAPSED_RESTORE_MAX_HEIGHT
        assert self.window.width() == saved_geometry["width"]

    def test_restore_clamps_short_saved_geometry_to_scroll_free_height(self):
        """An older saved size cannot reopen the full UI with a scrollbar."""
        saved_geometry = {
            "x": 10,
            "y": 10,
            "width": config.MAIN_WINDOW_DEFAULT_WIDTH,
            "height": 500,
            "format": self.window._geometry_format,
            "history_expanded": False,
        }
        self.mock_get_setting.side_effect = (
            lambda key, default=None: saved_geometry
            if key == SettingsKey.WINDOW_GEOMETRY
            else default
        )

        self.window._restore_window_geometry()

        assert self.window.height() == config.MAIN_WINDOW_MIN_HEIGHT

    def test_restore_clamps_narrow_saved_geometry_to_full_ui_width(self):
        """An older saved size cannot reopen with clipped full-UI controls."""
        saved_geometry = {
            "x": 10,
            "y": 10,
            "width": 500,
            "height": config.MAIN_WINDOW_DEFAULT_HEIGHT,
            "format": self.window._geometry_format,
            "history_expanded": False,
        }
        self.mock_get_setting.side_effect = (
            lambda key, default=None: saved_geometry
            if key == SettingsKey.WINDOW_GEOMETRY
            else default
        )

        self.window._restore_window_geometry()

        assert self.window.width() == config.MAIN_WINDOW_DEFAULT_WIDTH

    def test_minimum_full_width_keeps_tabs_and_footer_controls_separate(self):
        self.window.resize(
            config.MAIN_WINDOW_MIN_WIDTH,
            config.MAIN_WINDOW_DEFAULT_HEIGHT,
        )
        self.window.show()
        self.app.processEvents()

        tab_bar = self.window.tabbed_content.tab_bar
        last_tab = tab_bar.tabRect(tab_bar.count() - 1)
        assert last_tab.right() < tab_bar.width()

        buttons = (
            self.window.models_button,
            self.window.tray_button,
            self.window.settings_button,
            self.window.quit_button,
        )
        for left, right in zip(buttons, buttons[1:]):
            assert left.geometry().right() < right.geometry().left()

    def test_clamp_keeps_footer_on_short_screen(self):
        """1280x800 available geometry must not grow the window to 840px."""
        from PyQt6.QtCore import QRect

        self.window._available_screen_rect = lambda: QRect(0, 0, 1280, 800)
        clamped = self.window._clamp_geometry(
            0, 0, config.MAIN_WINDOW_DEFAULT_WIDTH, 840
        )
        assert clamped.height() == 800
        assert clamped.width() == config.MAIN_WINDOW_DEFAULT_WIDTH
        assert clamped.x() >= 0
        assert clamped.y() >= 0

    def test_geometry_save_waits_until_initial_show(self):
        self.window._initial_show_complete = False
        self.window._geometry_save_timer = None
        self.window._schedule_geometry_save()
        assert self.window._geometry_save_timer is None

    def test_history_toggle_preserves_height(self):
        from PyQt6.QtCore import QRect

        self.window._available_screen_rect = lambda: QRect(0, 0, 1280, 800)
        self.window.setGeometry(40, 40, 605, 580)
        self.window._sidebar_base_width = 605
        before = self.window.height()
        self.window._on_sidebar_width_animated(380)
        assert self.window.height() == before
