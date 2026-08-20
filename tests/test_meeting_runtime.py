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
    fake_repo = MagicMock()
    fake_repo.get_meeting.return_value = {"id": "m_test_123", "asr_model": "base"}
    rt._repo = fake_repo

    called = []

    def fake_rerun(*args, **kwargs):
        called.append(kwargs)
        return {
            "ok": True,
            "applied": 5,
            "error": None,
            "finalization": {
                "status": "completed",
                "message": "Final cloud insights are ready.",
                "steps": [
                    {
                        "id": "consolidation",
                        "name": "Summary & Action Items",
                        "status": "completed",
                    }
                ],
            },
        }

    monkeypatch.setattr("meeting.refinalize.rerun_finalization", fake_rerun)

    rt.retry_insights()

    # Wait briefly for daemon thread to complete
    for _ in range(50):
        if not rt.is_finalizing:
            break
        time.sleep(0.02)

    assert called
    assert called[0]["from_step"] == "failed"
    assert rt._finalization["status"] == "completed"
    assert "ready" in rt._finalization["message"]
    assert fake_engine._set_finalization.called
    status, message = fake_engine._set_finalization.call_args[0][:2]
    assert status == "completed"
    assert message == "Final cloud insights are ready."


def test_retry_after_engine_teardown_uses_card_meeting(runtime, monkeypatch):
    import time

    rt, controller = runtime
    controller.meeting_active = False
    rt._engine = None
    rt._card_meeting_id = "m_card"
    fake_repo = MagicMock()
    fake_repo.get_meeting.return_value = {"id": "m_card", "asr_model": "base"}
    fake_repo.list_meetings.return_value = [{"id": "m_other"}]
    rt._repo = fake_repo
    called = []

    def fake_rerun(repository, meeting_id, **kwargs):
        called.append((meeting_id, kwargs.get("from_step")))
        return {
            "ok": True,
            "applied": 1,
            "error": None,
            "finalization": {
                "status": "completed",
                "message": "Final cloud insights are ready.",
                "steps": [],
            },
        }

    monkeypatch.setattr("meeting.refinalize.rerun_finalization", fake_rerun)
    rt.retry_finalization("polish")
    for _ in range(50):
        if not rt.is_finalizing:
            break
        time.sleep(0.02)

    assert called == [("m_card", "polish")]
    fake_repo.list_meetings.assert_not_called()


def test_retry_without_card_uses_list_meetings(runtime, monkeypatch):
    import time

    rt, controller = runtime
    controller.meeting_active = False
    rt._engine = None
    rt._card_meeting_id = None
    fake_repo = MagicMock()
    fake_repo.list_meetings.return_value = [{"id": "m_latest"}]
    fake_repo.get_meeting.return_value = {"id": "m_latest", "asr_model": "base"}
    rt._repo = fake_repo
    called = []

    def fake_rerun(repository, meeting_id, **kwargs):
        called.append(meeting_id)
        return {
            "ok": True,
            "finalization": {"status": "completed", "message": "ready"},
        }

    monkeypatch.setattr("meeting.refinalize.rerun_finalization", fake_rerun)
    rt.retry_insights()
    for _ in range(50):
        if not rt.is_finalizing:
            break
        time.sleep(0.02)

    fake_repo.list_meetings.assert_called_with()
    assert called == ["m_latest"]


def _ended_meeting(meeting_id, *, deferred=False, status="failed", started_at="2026-08-19T18:00:00"):
    import json

    from meeting.state.schema import FinalizationState, MeetingState

    state = MeetingState(
        meeting_id=meeting_id,
        status="ended",
        cloud_enabled=True,
        finalization=FinalizationState(
            status=status,
            message="boom",
            card_deferred=deferred,
        ),
    )
    return {
        "id": meeting_id,
        "status": "ended",
        "started_at": started_at,
        "cloud_enabled": True,
        "state_json": json.dumps(state.to_dict()),
    }


def test_restore_skips_deferred_and_hydrates_older(runtime):
    rt, controller = runtime
    states = []
    controller.meeting_state_changed.connect(lambda p: states.append(dict(p)))
    fake_repo = MagicMock()
    fake_repo.list_meetings.return_value = [
        _ended_meeting("m_new", deferred=True),
        _ended_meeting("m_old", deferred=False, started_at="2026-08-18T18:00:00"),
    ]
    rt._repo = fake_repo

    rt._restore_last_finalization_worker()

    assert rt._card_meeting_id == "m_old"
    assert states[-1]["meeting_id"] == "m_old"
    assert states[-1]["finalization"]["status"] == "failed"
    assert states[-1]["finalization"]["card_deferred"] is False


def test_restore_all_deferred_leaves_idle(runtime):
    rt, controller = runtime
    states = []
    controller.meeting_state_changed.connect(lambda p: states.append(dict(p)))
    fake_repo = MagicMock()
    fake_repo.list_meetings.return_value = [
        _ended_meeting("m_a", deferred=True),
        _ended_meeting("m_b", deferred=True),
    ]
    rt._repo = fake_repo

    rt._restore_last_finalization_worker()

    assert rt._card_meeting_id is None
    assert rt._finalization is None
    assert states == []


def test_hydrate_skips_pending_without_steps(runtime):
    rt, controller = runtime
    fake_repo = MagicMock()
    fake_repo.list_meetings.return_value = [
        {
            "id": "m_pending",
            "status": "canceled",
            "cloud_enabled": True,
            "state_json": (
                '{"meeting_id":"m_pending","status":"canceled",'
                '"finalization":{"status":"pending"}}'
            ),
        }
    ]
    rt._repo = fake_repo

    rt._restore_last_finalization_worker()

    assert rt._card_meeting_id is None


def test_defer_persists_flag_and_hides_card(runtime):
    import json

    rt, controller = runtime
    states = []
    controller.meeting_state_changed.connect(lambda p: states.append(dict(p)))
    meeting = _ended_meeting("m_card")
    fake_repo = MagicMock()
    fake_repo.get_meeting.return_value = meeting
    persisted = []

    def persist(meeting_id, data):
        persisted.append((meeting_id, data))

    fake_repo.persist_state.side_effect = persist
    rt._repo = fake_repo
    rt._card_meeting_id = "m_card"
    rt._finalization = {"status": "failed", "message": "boom"}

    assert rt.defer_finalization_card() is True
    assert rt._finalization is None
    assert rt._card_meeting_id is None
    assert persisted[0][0] == "m_card"
    assert persisted[0][1]["finalization"]["card_deferred"] is True
    assert persisted[0][1]["finalization"]["status"] == "failed"
    assert states[-1]["finalization"] is None
    fake_repo.get_meeting.assert_called_with("m_card")


def test_defer_refused_while_finalizing(runtime):
    rt, controller = runtime
    statuses = []
    controller.meeting_status_update.connect(statuses.append)
    fake_repo = MagicMock()
    rt._repo = fake_repo
    rt._finalizing = True
    rt._card_meeting_id = "m_card"
    rt._finalization = {"status": "running", "message": "Preparing…"}

    assert rt.defer_finalization_card() is False
    fake_repo.persist_state.assert_not_called()
    assert any("still being prepared" in status for status in statuses)


def test_open_past_meeting_clears_deferral(runtime):
    rt, controller = runtime
    states = []
    controller.meeting_state_changed.connect(lambda p: states.append(dict(p)))
    meeting = _ended_meeting("m_saved", deferred=True)
    fake_repo = MagicMock()
    fake_repo.get_meeting.return_value = meeting
    persisted = []
    fake_repo.persist_state.side_effect = lambda meeting_id, data: persisted.append(
        (meeting_id, data)
    )
    rt._repo = fake_repo

    assert rt._hydrate_finalization_card(meeting, reveal=True) is True
    assert rt._card_meeting_id == "m_saved"
    assert states[-1]["finalization"]["card_deferred"] is False
    assert persisted[0][1]["finalization"]["card_deferred"] is False


def test_start_new_meeting_defers_then_starts(runtime, monkeypatch):
    rt, controller = runtime
    meeting = _ended_meeting("m_card")
    fake_repo = MagicMock()
    fake_repo.get_meeting.return_value = meeting
    persisted = []
    fake_repo.persist_state.side_effect = lambda meeting_id, data: persisted.append(
        (meeting_id, data)
    )
    rt._repo = fake_repo
    rt._card_meeting_id = "m_card"
    rt._finalization = {"status": "failed", "message": "boom"}
    started = []
    monkeypatch.setattr(
        rt, "_begin_start", lambda cloud, demo=False: started.append((cloud, demo))
    )

    rt.start_new_meeting(True)

    assert persisted[0][1]["finalization"]["card_deferred"] is True
    assert started == [(True, False)]
    assert rt._card_meeting_id is None

