"""Focused tests for MeetingRuntime finalization bridging and guards."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal

from services.runtime.meeting import MeetingRuntime


class _Controller(QObject):
    """Minimal ApplicationController stand-in with meeting signals."""

    meeting_status_update = pyqtSignal(str)
    meeting_state_changed = pyqtSignal(object)
    meeting_error = pyqtSignal(str)
    meeting_server_started = pyqtSignal(object)
    meeting_guest_link_ready = pyqtSignal(str)
    meeting_consent_requested = pyqtSignal()
    meeting_recovery_found = pyqtSignal(object)
    status_update = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.meeting_active = False
        self.current_backend = None
        self.recorder = SimpleNamespace(is_recording=False)


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    return app


@pytest.fixture
def runtime(qapp):
    controller = _Controller()
    rt = MeetingRuntime(controller)
    return rt, controller


def test_finalization_running_blocks_second_meeting_not_claim(runtime):
    rt, controller = runtime
    statuses = []
    controller.meeting_status_update.connect(statuses.append)

    rt._finalizing = True
    rt._finalization = {"status": "running", "message": "Preparing…"}
    rt._host_url = "http://127.0.0.1:8765/host"

    assert rt.is_claimed is False
    assert rt.is_finalizing is True

    rt.start_meeting(cloud_enabled=False)
    assert any("Final insights are still being prepared" in s for s in statuses)
    assert controller.meeting_active is False


def test_start_demo_meeting_blocked_while_finalizing(runtime):
    rt, controller = runtime
    statuses = []
    controller.meeting_status_update.connect(statuses.append)

    rt._finalizing = True
    rt.start_demo_meeting(cloud_enabled=True)
    assert any("Final insights are still being prepared" in s for s in statuses)
    assert controller.meeting_active is False


def test_ended_unlocks_active_while_retaining_dashboard(runtime):
    rt, controller = runtime
    states = []
    controller.meeting_state_changed.connect(lambda p: states.append(dict(p)))

    rt._host_url = "http://127.0.0.1:8765/host"
    rt._finalizing = True
    rt._finalization = {
        "status": "running",
        "message": "Preparing final cloud insights…",
    }
    controller.meeting_active = True

    rt._on_engine_event("ended", {"status": "ended", "meeting_id": "m1"})

    assert controller.meeting_active is False
    assert rt.is_claimed is False
    assert rt._host_url == "http://127.0.0.1:8765/host"
    assert states
    assert states[-1]["active"] is False
    assert states[-1]["finalization"]["status"] == "running"


def test_terminal_finalization_clears_guard_without_modal(runtime):
    rt, controller = runtime
    errors = []
    states = []
    controller.meeting_error.connect(errors.append)
    controller.meeting_state_changed.connect(lambda p: states.append(dict(p)))

    rt._finalizing = True
    rt._finalization = {"status": "running", "message": "…"}
    controller.meeting_active = False

    rt._on_engine_event("status", {
        "status": "ended",
        "finalization": {
            "status": "completed",
            "message": "Final cloud insights are ready.",
        },
    })

    assert rt.is_finalizing is False
    assert rt._finalization["status"] == "completed"
    assert errors == []
    assert any(
        (s.get("finalization") or {}).get("status") == "completed"
        for s in states
    )


def test_failed_finalization_is_non_modal(runtime):
    rt, controller = runtime
    errors = []
    controller.meeting_error.connect(errors.append)

    rt._on_engine_event("status", {
        "status": "ended",
        "finalization": {
            "status": "failed",
            "message": "Final cloud insights failed: boom",
        },
    })

    assert errors == []
    assert rt._finalization["status"] == "failed"
    assert rt.is_finalizing is False


def test_retry_insights_blocked_when_active(runtime):
    rt, controller = runtime
    controller.meeting_active = True
    rt.retry_insights()
    assert rt.is_finalizing is False


def test_retry_insights_runs_and_updates_finalization(runtime, monkeypatch):
    import time
    rt, controller = runtime
    controller.meeting_active = False

    fake_engine = MagicMock()
    fake_engine.is_active.return_value = False
    fake_engine.meeting_id = "m_test_123"
    fake_engine.store = MagicMock()
    rt._engine = fake_engine

    called = []

    def fake_rerun(*args, **kwargs):
        called.append(kwargs)
        return {"ok": True, "applied": 5, "error": None}

    monkeypatch.setattr("meeting.reinsight.rerun_insights", fake_rerun)

    rt.retry_insights()

    # Wait briefly for daemon thread to complete
    for _ in range(50):
        if not rt.is_finalizing:
            break
        time.sleep(0.02)

    assert called
    assert rt._finalization["status"] == "completed"
    assert "ready" in rt._finalization["message"]
    fake_engine._set_finalization.assert_called_with(
        "completed", "Final cloud insights are ready."
    )

