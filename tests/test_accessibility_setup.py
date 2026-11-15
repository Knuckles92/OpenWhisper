"""macOS setup persists dismissal and observes permission changes without relaunch."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication, QWidget

from services import _hotkey_pynput as backend
from services import accessibility_grant as grant
from services.runtime import hotkeys
from services.settings import SettingsKey, SettingsManager
from ui_qt.dialogs import accessibility_dialog as setup
from ui_qt.utils.theme_manager import ThemeManager
from ui_qt.widgets.wrapped_label import WrappedLabel


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(tmp_path):
    manager = SettingsManager(str(tmp_path / "settings.json"))
    with patch.object(setup, "settings_manager", manager), patch.object(
        hotkeys, "settings_manager", manager
    ), patch.object(grant, "settings_manager", manager):
        yield manager


def test_dismissal_survives_relaunch_and_preserves_autopaste(app, settings):
    settings.save_setting(SettingsKey.AUTO_PASTE, True)
    parent = QWidget()
    with patch.object(setup, "is_accessibility_trusted", return_value=False), patch.object(
        setup, "accessibility_app_bundle", return_value=Path("/Applications/OpenWhisper.app")
    ), patch.object(hotkeys.sys, "platform", "darwin"), patch.object(
        hotkeys, "is_accessibility_trusted", return_value=False, create=True
    ), patch.object(hotkeys, "USE_PYNPUT_BACKEND", True):
        dialog = setup.show_accessibility_setup(parent)
        assert not dialog.isModal()
        assert dialog.timer.isActive()
        dialog.done_button.click()
        assert not dialog.timer.isActive()
        assert settings.get(SettingsKey.MACOS_ACCESSIBILITY_INTRO_SEEN)
        assert settings.get(SettingsKey.AUTO_PASTE)
        # A fresh settings reader and runtime simulate the next launch.
        relaunched = SettingsManager(settings.settings_file)
        with patch.object(hotkeys, "settings_manager", relaunched), patch.object(
            grant, "settings_manager", relaunched
        ), patch.object(hotkeys.QTimer, "singleShot") as scheduled:
            hotkeys.HotkeyRuntime(SimpleNamespace())._check_autopaste_permission()
            scheduled.assert_not_called()
    parent.close()


@pytest.mark.parametrize("trusted,enabled,seen,granted,expected", [
    (False, True, False, None, True),
    (True, True, False, None, False),
    (False, False, False, None, False),
    (False, True, True, False, False),
    # Access the last launch had is gone: an update replaced the signed build.
    (False, True, True, True, True),
    (False, False, True, True, False),
    (True, True, True, True, False),
    # Installed before the grant was recorded, so this update is what lost it.
    (False, True, True, None, True),
])
def test_startup_only_offers_setup_when_needed(settings, trusted, enabled, seen, granted, expected):
    settings.update_settings({SettingsKey.AUTO_PASTE: enabled,
                              SettingsKey.MACOS_ACCESSIBILITY_INTRO_SEEN: seen})
    if granted is not None:
        settings.save_setting(SettingsKey.MACOS_ACCESSIBILITY_GRANTED, granted)
    with patch.object(hotkeys.sys, "platform", "darwin"), patch.object(
        hotkeys, "is_accessibility_trusted", return_value=trusted, create=True
    ), patch.object(hotkeys, "USE_PYNPUT_BACKEND", True), patch.object(
        hotkeys.QTimer, "singleShot"
    ) as scheduled:
        hotkeys.HotkeyRuntime(SimpleNamespace())._check_autopaste_permission()
        assert scheduled.called == expected


def test_setup_reopens_and_updates_live_without_requesting_permission(app, settings):
    parent = QWidget()
    with patch.object(setup, "is_accessibility_trusted", return_value=False) as trust, patch.object(
        setup, "request_accessibility_trust"
    ) as request, patch.object(setup, "accessibility_app_bundle", return_value=None):
        dialog = setup.show_accessibility_setup(parent)
        assert setup.show_accessibility_setup(parent) is dialog
        assert "not detected" in dialog.status.text()
        request.assert_not_called()
        trust.return_value = True
        dialog.timer.timeout.emit()
        assert "enabled" in dialog.status.text()
        assert dialog.done_button.text() == "Done"
        assert dialog.open_button.isEnabled()
        with patch.object(setup.QDesktopServices, "openUrl", return_value=True):
            dialog.open_button.click()
        request.assert_not_called()
        dialog.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        reopened = setup.show_accessibility_setup(parent)
        assert reopened is not dialog
        reopened.close()
    parent.close()


def test_settings_action_registers_identity_and_opens_exact_pane(app, settings):
    with patch.object(setup, "is_accessibility_trusted", return_value=False), patch.object(
        setup, "request_accessibility_trust"
    ) as request, patch.object(setup.QDesktopServices, "openUrl", return_value=True) as opened:
        dialog = setup.AccessibilityDialog()
        dialog.open_button.click()
        request.assert_called_once()
        assert opened.call_args.args[0].toString().endswith("?Privacy_Accessibility")
        dialog.close()


def test_launcher_executable_resolves_to_selectable_app(tmp_path):
    bundle = tmp_path / "Python.app"
    executable = bundle / "Contents" / "MacOS" / "Python"
    executable.parent.mkdir(parents=True)
    executable.touch()
    with patch.object(backend.sys, "platform", "darwin"), patch.dict(
        os.environ, {"OPENWHISPER_MACOS_LAUNCH_APP": str(executable)}
    ):
        assert backend.accessibility_app_bundle() == bundle
        instructions = backend.accessibility_permission_instructions()
        assert "Enable Python.app" in instructions
        assert str(bundle) in instructions
        assert str(executable) not in instructions


def test_instructions_fit_with_recovery_expanded(app, settings):
    with patch.object(setup, "is_accessibility_trusted", return_value=False), patch.object(
        setup, "accessibility_app_bundle", return_value=Path(
            "/opt/homebrew/Cellar/python@3.12/3.12.13_4/Frameworks/"
            "Python.framework/Versions/3.12/Resources/Python.app"
        )
    ):
        dialog = setup.AccessibilityDialog()
        dialog.setStyleSheet(ThemeManager().stylesheet)
        dialog.show()
        dialog.recovery.show()
        app.processEvents()
        for label in dialog.findChildren(WrappedLabel):
            assert label.height() >= label.heightForWidth(label.width()), label.text()
        dialog.close()


def _startup_check(trusted):
    with patch.object(hotkeys.sys, "platform", "darwin"), patch.object(
        hotkeys, "is_accessibility_trusted", return_value=trusted, create=True
    ), patch.object(hotkeys, "USE_PYNPUT_BACKEND", True), patch.object(
        hotkeys.QTimer, "singleShot"
    ) as scheduled:
        hotkeys.HotkeyRuntime(SimpleNamespace())._check_autopaste_permission()
    return scheduled.called


def test_lost_grant_is_offered_once_then_again_after_the_next_loss(settings):
    settings.update_settings({SettingsKey.AUTO_PASTE: True,
                              SettingsKey.MACOS_ACCESSIBILITY_INTRO_SEEN: True})
    assert not _startup_check(trusted=True)
    assert settings.get(SettingsKey.MACOS_ACCESSIBILITY_GRANTED) is True
    assert _startup_check(trusted=False)
    assert settings.get(SettingsKey.MACOS_ACCESSIBILITY_GRANTED) is False
    assert not _startup_check(trusted=False)
    assert not _startup_check(trusted=True)
    assert _startup_check(trusted=False)


def test_lost_grant_dialog_clears_the_stale_entry_once(app, settings):
    parent = QWidget()
    with patch.object(setup, "is_accessibility_trusted", return_value=False), patch.object(
        setup, "request_accessibility_trust"
    ) as request, patch.object(setup.accessibility_grant, "reset_stale_grant") as reset, patch.object(
        setup.QDesktopServices, "openUrl", return_value=True
    ), patch.object(setup, "accessibility_app_bundle", return_value=None):
        dialog = setup.show_accessibility_setup(parent, lost=True)
        assert dialog.windowTitle() == "Turn auto-paste back on"
        assert any("after an update" in label.text() for label in dialog.findChildren(WrappedLabel))
        reset.assert_not_called()
        dialog.open_button.click()
        dialog.open_button.click()
        # A second click must not remove the entry the user has just enabled.
        reset.assert_called_once()
        assert request.call_count == 2
        dialog.close()
    parent.close()


def test_dialog_records_access_once_granted(app, settings):
    with patch.object(setup, "is_accessibility_trusted", return_value=False) as trust, patch.object(
        setup, "accessibility_app_bundle", return_value=None
    ):
        dialog = setup.AccessibilityDialog()
        assert settings.get(SettingsKey.MACOS_ACCESSIBILITY_GRANTED) is False
        trust.return_value = True
        dialog.timer.timeout.emit()
        assert settings.get(SettingsKey.MACOS_ACCESSIBILITY_GRANTED) is True
        dialog.close()


def test_reset_leaves_a_development_launch_alone():
    with patch.object(grant.sys, "platform", "darwin"), patch.object(
        grant, "_frozen_bundle_id", return_value=""
    ), patch.object(grant.subprocess, "run") as run:
        assert not grant.reset_stale_grant()
        run.assert_not_called()


@pytest.mark.parametrize("returncode,expected", [(0, True), (1, False)])
def test_reset_removes_only_this_apps_accessibility_entry(returncode, expected):
    with patch.object(grant.sys, "platform", "darwin"), patch.object(
        grant, "_frozen_bundle_id", return_value="tech.fiorilabs.openwhisper"
    ), patch.object(
        grant.subprocess, "run", return_value=SimpleNamespace(returncode=returncode, stdout="", stderr="no")
    ) as run:
        assert grant.reset_stale_grant() is expected
        assert run.call_args.args[0] == [
            "/usr/bin/tccutil", "reset", "Accessibility", "tech.fiorilabs.openwhisper",
        ]


def test_bundle_id_comes_from_the_frozen_app_only(tmp_path):
    from services import app_update_macos

    with patch.object(app_update_macos, "running_bundle", return_value=None):
        assert grant._frozen_bundle_id() == ""
    with patch.object(app_update_macos, "running_bundle", return_value=str(tmp_path)), patch.object(
        app_update_macos, "bundle_info", return_value={"CFBundleIdentifier": "tech.fiorilabs.openwhisper"}
    ):
        assert grant._frozen_bundle_id() == "tech.fiorilabs.openwhisper"
