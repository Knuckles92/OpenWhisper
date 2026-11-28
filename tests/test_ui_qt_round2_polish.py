"""Regression tests for Round 2 Qt UI optimizations and polish."""
from __future__ import annotations

import errno
import os
import sqlite3
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QFont
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel

from ui_qt.dialogs.history_export_dialog import HistoryExportDialog
from ui_qt.utils.error_messages import format_action_error, friendly_error_message
from ui_qt.utils.font_scale import apply_scaled_font, apply_ui_font_scale, scaled_font
from ui_qt.widgets.wrapped_label import WrappedLabel


def _wait_until(predicate, timeout_ms: int = 3000) -> None:
    steps = max(1, timeout_ms // 10)
    for _ in range(steps):
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError("Timed out waiting for Qt condition")


def test_friendly_error_message_maps_technical_exceptions():
    assert "Permission denied" in friendly_error_message(PermissionError("Access is denied"))
    assert "database is busy" in friendly_error_message(
        sqlite3.OperationalError("database is locked")
    ).lower()
    enospc = OSError(errno.ENOSPC, "No space left on device")
    assert "disk space" in friendly_error_message(enospc).lower()
    raw_net = RuntimeError("HTTPSConnectionPool(host='api.example.com', port=443): Max retries exceeded")
    assert "Could not connect" in friendly_error_message(raw_net)
    # Clean domain validation errors stay intact.
    assert friendly_error_message(ValueError("Name cannot be empty")) == "Name cannot be empty"
    assert format_action_error("Couldn't save setting", PermissionError("denied")).startswith(
        "Couldn't save setting: Permission denied"
    )


def test_scaled_font_and_live_rescaling():
    app = QApplication.instance() or QApplication([])
    label = QLabel("Sample")
    try:
        apply_ui_font_scale(100, app=app)
        apply_scaled_font(label, 12, QFont.Weight.DemiBold)
        assert label.font().pointSizeF() == 12.0
        assert scaled_font(10).pointSizeF() == 10.0

        apply_ui_font_scale(150, app=app)
        assert label.font().pointSizeF() == 18.0
        assert scaled_font(10).pointSizeF() == 15.0
    finally:
        apply_ui_font_scale(100, app=app)
        label.deleteLater()


def test_export_dialog_async_load_and_retry():
    gate = threading.Event()
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        gate.wait(2.0)
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return [
            {
                "id": "e1",
                "timestamp": "2026-03-15T14:30:00+00:00",
                "formatted_timestamp": "Mar 15, 2026 02:30 PM",
                "preview_text": "Hello",
                "text": "Hello",
                "has_audio": False,
                "audio_file": None,
                "model": "local_whisper",
                "raw_text": None,
            }
        ]

    # Trigger async_load explicitly via ExportDialogBase.__init__ through HistoryExportDialog
    dialog = HistoryExportDialog.__new__(HistoryExportDialog)
    from ui_qt.dialogs.export_dialog_base import ExportDialogBase

    ExportDialogBase.__init__(dialog, None, provider, async_load=True)
    try:
        assert dialog._loading is True
        assert hasattr(dialog, "loading_label")
        assert "Loading transcriptions" in dialog.loading_label.text()

        gate.set()
        _wait_until(lambda: not dialog._loading)

        # First load failed -> friendly error message + Retry button, no raw sqlite3 error.
        assert hasattr(dialog, "retry_btn")
        labels = [w.text() for w in dialog.findChildren(WrappedLabel)]
        assert any("database is busy" in t.lower() for t in labels)
        assert not any("sqlite3" in t.lower() for t in labels)

        # Clicking Retry reloads asynchronously and shows export UI.
        dialog.retry_btn.click()
        _wait_until(lambda: not dialog._loading and hasattr(dialog, "export_btn"))
        assert len(dialog._items) == 1
        assert dialog.export_btn.isEnabled()
    finally:
        dialog.deleteLater()
