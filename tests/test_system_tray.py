import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QSystemTrayIcon

import ui_qt.system_tray as system_tray
from ui_qt.system_tray import SystemTrayManager

Reason = QSystemTrayIcon.ActivationReason


def _make_tray(click_to_restore: bool):
    with patch.object(system_tray, "_MANUAL_CONTEXT_MENU", click_to_restore):
        window = MagicMock()
        tray = SystemTrayManager(window)
    tray.menu.popup = MagicMock()
    return tray, window


def _activate(tray, reason, click_to_restore, modifiers=Qt.KeyboardModifier.NoModifier):
    with patch.object(system_tray, "_MANUAL_CONTEXT_MENU", click_to_restore), patch.object(
        system_tray.QGuiApplication, "queryKeyboardModifiers", return_value=modifiers
    ):
        tray._on_activated(reason)


class TestMacStatusItemClicks:
    def test_menu_is_not_attached_so_left_click_reaches_the_app(self):
        tray, _ = _make_tray(click_to_restore=True)
        assert tray.contextMenu() is None

    def test_left_click_restores_hidden_window(self):
        tray, window = _make_tray(click_to_restore=True)
        _activate(tray, Reason.Trigger, click_to_restore=True)
        window.restore_from_tray.assert_called_once()
        tray.menu.popup.assert_not_called()

    def test_right_click_opens_menu_without_restoring(self):
        tray, window = _make_tray(click_to_restore=True)
        _activate(tray, Reason.Context, click_to_restore=True)
        tray.menu.popup.assert_called_once()
        window.restore_from_tray.assert_not_called()

    def test_ctrl_click_opens_menu_without_restoring(self):
        tray, window = _make_tray(click_to_restore=True)
        # Qt maps the physical Control key to MetaModifier on macOS.
        _activate(
            tray,
            Reason.Trigger,
            click_to_restore=True,
            modifiers=Qt.KeyboardModifier.MetaModifier,
        )
        tray.menu.popup.assert_called_once()
        window.restore_from_tray.assert_not_called()


class TestOtherPlatformTrayClicks:
    def test_context_menu_stays_attached(self):
        tray, _ = _make_tray(click_to_restore=False)
        assert tray.contextMenu() is tray.menu

    def test_single_click_restores(self):
        tray, window = _make_tray(click_to_restore=False)
        _activate(tray, Reason.Trigger, click_to_restore=False)
        window.restore_from_tray.assert_called_once()

    def test_menu_shows_the_action_for_the_current_window_state(self):
        tray, window = _make_tray(click_to_restore=False)
        window.isVisible.return_value = True
        window.isMinimized.return_value = False
        with patch.object(system_tray, "can_copy_last_original", return_value=False):
            tray._refresh_menu()
        assert tray.hide_action.isVisible()
        assert not tray.show_action.isVisible()
        assert not tray.copy_original_action.isEnabled()

        window.isMinimized.return_value = True
        with patch.object(system_tray, "can_copy_last_original", return_value=True):
            tray._refresh_menu()
        assert tray.show_action.isVisible()
        assert not tray.hide_action.isVisible()
        assert tray.copy_original_action.isEnabled()
