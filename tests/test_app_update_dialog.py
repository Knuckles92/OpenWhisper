"""Qt tests for the first update-available prompt and its opt-out boxes."""
import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QCheckBox, QPushButton

_QAPP = QApplication.instance() or QApplication([])

from services.app_update import (
    ApplyMode,
    InstallChannel,
    ReleaseAsset,
    ReleaseInfo,
    UpdateCheckResult,
    UpdateStatus,
)
from ui_qt.dialogs.app_update_dialog import AppUpdateDialog


def _result(
    *,
    status: str = UpdateStatus.UPDATE_AVAILABLE,
    channel: str = InstallChannel.SOURCE,
    can_apply: bool = False,
    version: str = "2.2.0",
    apply_mode: str | None = None,
) -> UpdateCheckResult:
    return UpdateCheckResult(
        status=status,
        current_version="2.1.1",
        channel=channel,
        release=ReleaseInfo(
            version=version,
            tag_name=f"v{version}",
            html_url=f"https://github.com/Knuckles92/OpenWhisper/releases/tag/v{version}",
            notes="Bug fixes",
            setup_asset=ReleaseAsset(
                url="https://example/OpenWhisper-Setup-2.2.0.exe",
                name="OpenWhisper-Setup-2.2.0.exe",
                size_bytes=90_000_000,
                sha256="ab" * 32,
            ),
        ),
        can_apply=can_apply,
        apply_mode=(
            apply_mode
            if apply_mode is not None
            else (ApplyMode.SETUP if can_apply else ApplyMode.NOTIFY_ONLY)
        ),
        git_hint=(
            "git pull --ff-only\npip install -r requirements.txt"
            if channel == InstallChannel.GIT
            else None
        ),
    )


class _QtTestCase:
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


class TestAppUpdateDialog(_QtTestCase):
    def _button(self, dialog, name):
        return dialog.findChild(QPushButton, name)

    def _box(self, dialog, name):
        return dialog.findChild(QCheckBox, name)

    def test_source_primary_is_open_release_notes(self):
        dialog = AppUpdateDialog(_result(channel=InstallChannel.GIT))
        assert self._button(dialog, "updatePrimaryButton").text() == (
            "Open release notes"
        )
        assert "git pull --ff-only" in dialog.hint_label.text()

    def test_installer_primary_is_download_and_install(self):
        dialog = AppUpdateDialog(
            _result(channel=InstallChannel.INSTALLER, can_apply=True)
        )
        assert self._button(dialog, "updatePrimaryButton").text() == (
            "Download and install"
        )

    def test_later_persists_opt_outs_and_skipped_version(self):
        dialog = AppUpdateDialog(_result())
        self._box(dialog, "updateDontNotifyCheck").setChecked(True)
        self._box(dialog, "updateDontCheckCheck").setChecked(True)
        with patch(
            "ui_qt.dialogs.app_update_dialog.persist_prompt_choices"
        ) as persist:
            self._button(dialog, "updateLaterButton").click()
        persist.assert_called_once_with(
            notify_enabled=False,
            check_enabled=False,
            skipped_version="2.2.0",
        )
        assert dialog.result_action == AppUpdateDialog.RESULT_LATER

    def test_dont_notify_only(self):
        dialog = AppUpdateDialog(_result())
        self._box(dialog, "updateDontNotifyCheck").setChecked(True)
        with patch(
            "ui_qt.dialogs.app_update_dialog.persist_prompt_choices"
        ) as persist:
            self._button(dialog, "updateLaterButton").click()
        persist.assert_called_once_with(
            notify_enabled=False,
            check_enabled=True,
            skipped_version="2.2.0",
        )

    def test_primary_does_not_skip_version(self):
        dialog = AppUpdateDialog(_result())
        with patch(
            "ui_qt.dialogs.app_update_dialog.persist_prompt_choices"
        ) as persist:
            self._button(dialog, "updatePrimaryButton").click()
        persist.assert_called_once_with(
            notify_enabled=True,
            check_enabled=True,
            skipped_version=None,
        )
        assert dialog.result_action == AppUpdateDialog.RESULT_PRIMARY

    def test_failed_check_offers_releases_page(self):
        dialog = AppUpdateDialog(
            None, error="GitHub rate-limited this update check."
        )
        assert dialog.title_label.text() == "Could not check for updates"
        assert "rate-limited" in dialog.body_label.text()
        assert self._button(dialog, "updatePrimaryButton").text() == (
            "Open releases page"
        )
        assert not self._button(dialog, "updatePrimaryButton").isHidden()

    def test_up_to_date_has_no_primary_and_does_not_skip(self):
        dialog = AppUpdateDialog(
            _result(status=UpdateStatus.UP_TO_DATE, version="2.1.1")
        )
        assert self._button(dialog, "updatePrimaryButton").isHidden()
        assert self._button(dialog, "updateLaterButton").text() == "Close"
        with patch(
            "ui_qt.dialogs.app_update_dialog.persist_prompt_choices"
        ) as persist:
            self._button(dialog, "updateLaterButton").click()
        persist.assert_called_once_with(
            notify_enabled=True,
            check_enabled=True,
            skipped_version=None,
        )

    def test_progress_copy_uses_update_not_installer(self):
        dialog = AppUpdateDialog(
            _result(channel=InstallChannel.INSTALLER, can_apply=True)
        )
        dialog._set_downloading()
        assert dialog.body_label.text() == "Downloading the update…"
        assert dialog.later_btn.text() == "Cancel"
        dialog.set_progress("verifying", 50, 100)
        assert dialog.body_label.text() == "Verifying the download…"
        dialog.set_progress("extracting", 50, 100)
        assert dialog.body_label.text() == "Preparing the update…"
        dialog.set_progress("restarting", 1, 1)
        assert dialog.body_label.text() == "Restarting to finish the update…"

    def test_error_offers_setup_fallback_for_native_mode(self):
        dialog = AppUpdateDialog(
            _result(
                channel=InstallChannel.INSTALLER,
                can_apply=True,
                apply_mode=ApplyMode.NATIVE,
            )
        )
        dialog.set_error("disk full", offer_setup=True)
        assert not dialog.setup_btn.isHidden()
        assert dialog.later_btn.text() == "Later"
