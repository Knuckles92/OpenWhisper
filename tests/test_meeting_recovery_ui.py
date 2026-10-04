"""Recovery inventory errors remain actionable in both desktop styles."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QWidget

from ui_qt.ui_controller import UIController


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("mode", ["classic", "omarchy"])
def test_recovery_scan_failure_has_modeless_retry_at_narrow_width(
    qapp, monkeypatch, mode
):
    monkeypatch.setenv("OPENWHISPER_UI", mode)
    window = QWidget()
    window.resize(390, 520)
    statuses = []
    target = SimpleNamespace(
        main_window=window,
        _meeting_recovery_scan_dialog=None,
        set_meeting_status=statuses.append,
    )
    retries = []
    try:
        UIController.show_meeting_recovery_scan_error(
            target, "Database unavailable; saved meetings were not checked.",
            on_retry=lambda: retries.append(True),
        )
        qapp.processEvents()
        dialog = target._meeting_recovery_scan_dialog
        assert dialog is not None and dialog.isVisible()
        assert dialog.isModal() is False
        assert "not checked" in dialog.text()
        assert "Meeting recovery scan failed" in statuses
        retry = next(
            button for button in dialog.buttons()
            if button.text() == "Retry scan"
        )
        retry.click()
        qapp.processEvents()
        assert retries == [True]
        assert target._meeting_recovery_scan_dialog is None
    finally:
        window.close()
