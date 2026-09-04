"""Linux Meeting Mode readiness dialog."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PyQt6.QtWidgets import QApplication

from meeting.capture.linux_audio import LinuxAudioCapability
from ui_qt.dialogs.meeting_linux_audio_dialog import (
    MeetingLinuxAudioDialog,
    ensure_meeting_linux_system_audio,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _capability(reason="libpulse_missing", family="apt", ready=False):
    return LinuxAudioCapability(
        ready=ready,
        reason=reason if not ready else "ready",
        server_kind="unknown",
        default_sink="",
        monitor_source="",
        package_family=family,
        remediation_key=reason if not ready else "ready",
    )


class TestMeetingLinuxAudioDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_copy_command_and_guide(self):
        dialog = MeetingLinuxAudioDialog(_capability())
        self.assertIn("libpulse0", dialog.commands_edit.toPlainText())
        clipboard = MagicMock()
        with patch(
            "ui_qt.dialogs.meeting_linux_audio_dialog.QGuiApplication.clipboard",
            return_value=clipboard,
        ):
            dialog._copy_commands()
        clipboard.setText.assert_called_once()
        self.assertIn("libpulse0", clipboard.setText.call_args.args[0])

        with patch(
            "ui_qt.dialogs.meeting_linux_audio_dialog.QDesktopServices.openUrl"
        ) as open_url:
            dialog._open_guide()
        open_url.assert_called_once()

    def test_frozen_guide_url_resolves_beneath_bundle_root(self):
        root = Path(__file__).resolve().parent / "frozen-bundle"
        with patch(
            "ui_qt.dialogs.meeting_linux_audio_dialog.bundle_root",
            return_value=str(root),
        ):
            url = MeetingLinuxAudioDialog._default_guide_url()
        expected = (root / "docs" / "linux-system-audio.md").as_uri()
        self.assertEqual(url, expected)

    def test_retry_ready_finishes(self):
        from PyQt6.QtCore import QTimer

        dialog = MeetingLinuxAudioDialog(
            _capability(),
            probe=lambda: _capability(ready=True),
        )
        dialog._retry_detection()
        # Async probe completion is bounced onto the Qt event loop.
        deadline = __import__("time").monotonic() + 2.0
        while (
            dialog.result_action != MeetingLinuxAudioDialog.RESULT_READY
            and __import__("time").monotonic() < deadline
        ):
            self.app.processEvents()
            __import__("time").sleep(0.01)
        self.assertEqual(dialog.result_action, MeetingLinuxAudioDialog.RESULT_READY)

    def test_mic_only_and_cancel(self):
        dialog = MeetingLinuxAudioDialog(_capability())
        dialog.mic_only_btn.click()
        self.assertEqual(
            dialog.result_action, MeetingLinuxAudioDialog.RESULT_MICROPHONE_ONLY
        )

        dialog = MeetingLinuxAudioDialog(_capability())
        dialog.go_back_btn.click()
        self.assertEqual(dialog.result_action, MeetingLinuxAudioDialog.RESULT_CANCEL)

    def test_ensure_helper_ready_path(self):
        with patch(
            "ui_qt.dialogs.meeting_linux_audio_dialog.probe_linux_audio",
            return_value=_capability(ready=True),
        ), patch("sys.platform", "linux"):
            self.assertEqual(
                ensure_meeting_linux_system_audio(),
                MeetingLinuxAudioDialog.RESULT_READY,
            )

    def test_ensure_helper_non_linux(self):
        with patch("sys.platform", "win32"):
            self.assertEqual(
                ensure_meeting_linux_system_audio(),
                MeetingLinuxAudioDialog.RESULT_READY,
            )

    def test_unknown_family_has_no_apt_command(self):
        dialog = MeetingLinuxAudioDialog(
            _capability(reason="audio_server_unavailable", family="unknown")
        )
        text = dialog.commands_edit.toPlainText()
        self.assertNotIn("sudo apt", text)

    def test_remediation_varies_by_package_family(self):
        apt = MeetingLinuxAudioDialog(_capability(family="apt"))
        dnf = MeetingLinuxAudioDialog(_capability(family="dnf"))
        pacman = MeetingLinuxAudioDialog(_capability(family="pacman"))
        self.assertIn("apt install", apt.commands_edit.toPlainText())
        self.assertIn("dnf install", dnf.commands_edit.toPlainText())
        self.assertIn("pacman -S", pacman.commands_edit.toPlainText())

    def test_retry_failed_updates_dialog(self):
        dialog = MeetingLinuxAudioDialog(
            _capability(reason="libpulse_missing", family="apt"),
            probe=lambda: _capability(
                reason="pipewire_pulse_missing", family="apt"
            ),
        )
        dialog._retry_detection()
        deadline = __import__("time").monotonic() + 2.0
        while dialog._probe_pending and __import__("time").monotonic() < deadline:
            self.app.processEvents()
            __import__("time").sleep(0.01)
        self.app.processEvents()
        self.assertEqual(dialog.result_action, MeetingLinuxAudioDialog.RESULT_CANCEL)
        combined = (dialog.title_label.text() + " " + dialog.body_label.text()).lower()
        self.assertIn("pipewire", combined)
        self.assertIn("pulse", combined)

    def test_blocking_retry_marshals_ready_result_to_gui_thread(self):
        import threading
        import time

        release = threading.Event()

        def slow_ready():
            release.wait(timeout=5.0)
            return _capability(ready=True)

        dialog = MeetingLinuxAudioDialog(
            _capability(reason="monitor_open_failed", family="apt"),
            probe=slow_ready,
            probe_timeout_s=5.0,
        )
        # Cancellation stays available while probing.
        dialog._retry_detection()
        self.assertTrue(dialog._probe_pending)
        self.assertTrue(dialog.mic_only_btn.isEnabled())
        self.assertTrue(dialog.go_back_btn.isEnabled())
        self.assertFalse(dialog.retry_btn.isEnabled())

        # GUI heartbeat while the worker is still blocked.
        for _ in range(5):
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(dialog._probe_pending)

        release.set()
        deadline = time.monotonic() + 3.0
        while (
            dialog.result_action != MeetingLinuxAudioDialog.RESULT_READY
            and time.monotonic() < deadline
        ):
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(
            dialog.result_action, MeetingLinuxAudioDialog.RESULT_READY
        )
        self.assertFalse(dialog._probe_pending)

    def test_ensure_helper_bounds_probe_timeout(self):
        import threading
        import time

        def never_returns():
            time.sleep(30.0)
            return _capability(ready=True)

        class _CancelDialog:
            RESULT_CANCEL = MeetingLinuxAudioDialog.RESULT_CANCEL
            RESULT_READY = MeetingLinuxAudioDialog.RESULT_READY
            RESULT_MICROPHONE_ONLY = MeetingLinuxAudioDialog.RESULT_MICROPHONE_ONLY

            def __init__(self, *args, **kwargs):
                self.result_action = self.RESULT_CANCEL

            def exec(self):
                return 0

        before = {
            t.ident for t in threading.enumerate() if t.is_alive() and not t.daemon
        }
        with patch("sys.platform", "linux"), patch(
            "ui_qt.dialogs.meeting_linux_audio_dialog.MeetingLinuxAudioDialog",
            _CancelDialog,
        ):
            started = time.monotonic()
            action = ensure_meeting_linux_system_audio(
                probe=never_returns,
                probe_timeout_s=0.2,
            )
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        self.assertEqual(action, MeetingLinuxAudioDialog.RESULT_CANCEL)
        after = {
            t.ident for t in threading.enumerate() if t.is_alive() and not t.daemon
        }
        self.assertEqual(after, before)

    def test_late_ready_after_timeout_does_not_auto_accept(self):
        import threading
        import time

        release = threading.Event()

        def late_ready():
            release.wait(timeout=5.0)
            return _capability(ready=True)

        dialog = MeetingLinuxAudioDialog(
            _capability(reason="monitor_open_failed", family="apt"),
            probe=late_ready,
            probe_timeout_s=0.15,
        )
        dialog._retry_detection()
        deadline = time.monotonic() + 2.0
        while dialog._probe_pending and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertFalse(dialog._probe_pending)
        self.assertEqual(dialog.result_action, MeetingLinuxAudioDialog.RESULT_CANCEL)
        self.assertIn("probe_timeout", dialog._capability.detail)

        # Late ready must not flip the dialog to accepted.
        release.set()
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(dialog.result_action, MeetingLinuxAudioDialog.RESULT_CANCEL)
        self.assertFalse(dialog.isHidden() and dialog.result_action == "ready")

    def test_retry_after_hung_probe_uses_fresh_daemon_worker(self):
        import threading
        import time

        calls = {"n": 0}
        release_second = threading.Event()

        def probe():
            calls["n"] += 1
            if calls["n"] == 1:
                time.sleep(30.0)
                return _capability(ready=False, reason="monitor_open_failed")
            release_second.wait(timeout=2.0)
            return _capability(ready=True)

        dialog = MeetingLinuxAudioDialog(
            _capability(reason="monitor_open_failed", family="apt"),
            probe=probe,
            probe_timeout_s=0.15,
        )
        before = {
            t.ident for t in threading.enumerate() if t.is_alive() and not t.daemon
        }
        dialog._retry_detection()
        deadline = time.monotonic() + 2.0
        while dialog._probe_pending and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(dialog._probe_pending)

        dialog._retry_detection()
        release_second.set()
        deadline = time.monotonic() + 2.0
        while (
            dialog.result_action != MeetingLinuxAudioDialog.RESULT_READY
            and time.monotonic() < deadline
        ):
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(
            dialog.result_action, MeetingLinuxAudioDialog.RESULT_READY
        )
        after = {
            t.ident for t in threading.enumerate() if t.is_alive() and not t.daemon
        }
        self.assertEqual(after, before)
        self.assertGreaterEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
