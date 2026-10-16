"""The title-bar and tray update notice that replaced the launch dialog."""
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QWidget

_QAPP = QApplication.instance() or QApplication([])

from services.app_update import (
    ApplyMode,
    InstallChannel,
    ReleaseInfo,
    UpdateCheckResult,
    UpdateStatus,
)
from services.settings import SettingsKey, settings_manager
from ui_qt.main_window import MainWindow
from ui_qt.system_tray import SystemTrayManager
from ui_qt.ui_controller import UIController
from ui_qt.widgets.update_notice import UpdateNoticeButton


@pytest.fixture(autouse=True)
def isolated_update_preferences(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings_manager, "settings_file", str(tmp_path / "settings.json")
    )


def _result(status=UpdateStatus.UPDATE_AVAILABLE, version="2.2.0"):
    return UpdateCheckResult(
        status=status,
        current_version="2.1.1",
        channel=InstallChannel.INSTALLER,
        release=ReleaseInfo(
            version=version,
            tag_name=f"v{version}",
            html_url=f"https://github.com/Knuckles92/OpenWhisper/releases/tag/v{version}",
            notes="Bug fixes",
        ),
        can_apply=False,
        apply_mode=ApplyMode.NOTIFY_ONLY,
    )


class _FakeWindow:
    def __init__(self):
        # A parent that is never shown, so the button's own show/hide state is
        # what isHidden() reports, without opening a top-level window.
        self._holder = QWidget()
        self.update_notice = UpdateNoticeButton(self._holder)

    def set_update_notice(self, version):
        MainWindow.set_update_notice(self, version)


class _FakeController:
    """Enough of a UIController for its update-notice methods to run."""

    def __init__(self):
        self.main_window = _FakeWindow()
        self.tray_manager = SystemTrayManager(None)
        self._update_dialog = None
        self._last_update_result = None
        self.show_app_update_dialog = MagicMock()

    def _refresh_update_notice(self):
        UIController._refresh_update_notice(self)

    def finish(self, result, error="", manual=False):
        UIController.on_update_check_finished(self, result, error, manual)


def _notice(controller):
    button = controller.main_window.update_notice
    return "" if button.isHidden() else button.text()


def _tray(controller):
    action = controller.tray_manager.update_action
    return action.text() if action.isVisible() else ""


def test_automatic_check_lights_the_notice_without_a_dialog():
    controller = _FakeController()
    controller.finish(_result())
    assert _notice(controller) == "Update to 2.2.0"
    assert _tray(controller) == "Update to 2.2.0…"
    assert "2.2.0" in controller.tray_manager.toolTip()
    controller.show_app_update_dialog.assert_not_called()


def test_manual_check_opens_the_dialog_and_lights_the_notice():
    controller = _FakeController()
    result = _result()
    controller.finish(result, manual=True)
    controller.show_app_update_dialog.assert_called_once_with(result, error="")
    assert _notice(controller) == "Update to 2.2.0"


def test_failed_check_keeps_a_release_found_earlier():
    controller = _FakeController()
    controller.finish(_result())
    controller.finish(None, "Could not reach the update server.")
    assert _notice(controller) == "Update to 2.2.0"


def test_up_to_date_result_withdraws_the_notice():
    controller = _FakeController()
    controller.finish(_result())
    controller.finish(_result(status=UpdateStatus.UP_TO_DATE, version="2.1.1"))
    assert _notice(controller) == ""
    assert _tray(controller) == ""
    assert controller.tray_manager.toolTip() == "OpenWhisper"


def test_notices_follow_the_notify_setting():
    controller = _FakeController()
    settings_manager.save_setting(SettingsKey.UPDATE_NOTIFY_ENABLED, False)
    controller.finish(_result())
    assert _notice(controller) == ""

    settings_manager.save_setting(SettingsKey.UPDATE_NOTIFY_ENABLED, True)
    controller._refresh_update_notice()
    assert _notice(controller) == "Update to 2.2.0"


def test_check_while_the_dialog_is_open_leaves_its_result_alone():
    controller = _FakeController()
    shown = _result()
    controller._last_update_result = shown
    controller._update_dialog = MagicMock()
    controller.finish(_result(version="2.3.0"))
    assert controller._last_update_result is shown
    controller._update_dialog.raise_.assert_not_called()


def test_compact_window_drops_the_version_but_keeps_it_in_the_tooltip():
    window = _FakeWindow()
    button = window.update_notice
    button.set_compact(True)
    button.show_version("2.2.0")
    assert button.text() == "Update"
    assert "2.2.0" in button.toolTip()
    button.set_compact(False)
    assert button.text() == "Update to 2.2.0"


def test_notice_and_tray_open_the_found_release():
    controller = _FakeController()
    controller.finish(_result())
    UIController.show_found_update(controller)
    controller.show_app_update_dialog.assert_called_once_with(
        controller._last_update_result
    )

    requested = MagicMock()
    controller.tray_manager.update_requested.connect(requested)
    controller.tray_manager.update_action.trigger()
    requested.assert_called_once_with()
