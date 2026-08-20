"""Focused tests for MeetingRuntime finalization bridging and guards."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.settings import SettingsKey

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
def runtime(qapp, monkeypatch):
    monkeypatch.setattr(
        "services.runtime.meeting.settings_manager.save_setting",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "services.runtime.meeting.settings_manager.get",
        lambda key, default=False: default,
    )
    controller = _Controller()
    rt = MeetingRuntime(controller)
    return rt, controller


def _record_launch(rt, monkeypatch):
    launched = []

    def fake_launch(cloud, *, demo=False):
        launched.append({"cloud": cloud, "demo": demo})

    monkeypatch.setattr(rt, "_launch", fake_launch)
    return launched


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


def test_cloud_start_without_consent_emits_and_does_not_launch(runtime, monkeypatch):
    rt, controller = runtime
    launched = _record_launch(rt, monkeypatch)
    consents = []
    controller.meeting_consent_requested.connect(lambda: consents.append(True))
    monkeypatch.setattr(rt, "_cloud_consent_given", lambda: False)

    rt.start_meeting(cloud_enabled=True)

    assert consents == [True]
    assert launched == []
    assert rt._consent_pending_kind == "start"
    assert rt.is_claimed is True
    assert controller.meeting_active is False


def test_demo_start_without_consent_uses_start_demo_kind(runtime, monkeypatch):
    rt, controller = runtime
    _record_launch(rt, monkeypatch)
    monkeypatch.setattr(rt, "_cloud_consent_given", lambda: False)

    rt.start_demo_meeting(cloud_enabled=True)

    assert rt._consent_pending_kind == "start_demo"
    assert rt.is_claimed is True


def test_declined_consent_still_starts_transcript_only(runtime, monkeypatch):
    rt, controller = runtime
    launched = _record_launch(rt, monkeypatch)
    rt._starting = True
    rt._consent_pending_kind = "start"

    rt.on_consent_result(False)

    assert launched == [{"cloud": False, "demo": False}]
    assert rt._consent_pending_kind is None


def test_granted_consent_starts_with_cloud(runtime, monkeypatch):
    rt, controller = runtime
    launched = _record_launch(rt, monkeypatch)
    rt._starting = True
    rt._consent_pending_kind = "start"

    rt.on_consent_result(True)

    assert launched == [{"cloud": True, "demo": False}]


def test_granted_demo_consent_starts_demo_with_cloud(runtime, monkeypatch):
    rt, controller = runtime
    launched = _record_launch(rt, monkeypatch)
    rt._consent_pending_kind = "start_demo"

    rt.on_consent_result(True)

    assert launched == [{"cloud": True, "demo": True}]


def test_start_without_cloud_skips_consent(runtime, monkeypatch):
    rt, controller = runtime
    launched = _record_launch(rt, monkeypatch)
    consents = []
    controller.meeting_consent_requested.connect(lambda: consents.append(True))

    rt.start_meeting(cloud_enabled=False)

    assert consents == []
    assert launched == [{"cloud": False, "demo": False}]
    assert rt._consent_pending_kind is None


def test_start_with_consent_already_given_launches_cloud(runtime, monkeypatch):
    rt, controller = runtime
    launched = _record_launch(rt, monkeypatch)
    consents = []
    controller.meeting_consent_requested.connect(lambda: consents.append(True))
    monkeypatch.setattr(rt, "_cloud_consent_given", lambda: True)

    rt.start_meeting(cloud_enabled=True)

    assert consents == []
    assert launched == [{"cloud": True, "demo": False}]


def test_remembered_cloud_on_prompts_when_consent_missing(runtime, monkeypatch):
    rt, controller = runtime
    launched = _record_launch(rt, monkeypatch)
    consents = []
    controller.meeting_consent_requested.connect(lambda: consents.append(True))

    def fake_get(key, default=False):
        return key == SettingsKey.MEETING_CLOUD_LAST_ENABLED

    monkeypatch.setattr(
        "services.runtime.meeting.settings_manager.get", fake_get
    )
    monkeypatch.setattr(rt, "_cloud_consent_given", lambda: False)

    rt.start_meeting()

    assert consents == [True]
    assert launched == []
    assert rt._consent_pending_kind == "start"


def test_second_start_during_consent_is_blocked(runtime, monkeypatch):
    rt, controller = runtime
    launched = _record_launch(rt, monkeypatch)
    monkeypatch.setattr(rt, "_cloud_consent_given", lambda: False)
    rt.start_meeting(cloud_enabled=True)

    statuses = []
    controller.meeting_status_update.connect(statuses.append)
    rt.start_meeting(cloud_enabled=False)

    assert launched == []
    assert any("already in progress" in s for s in statuses)
    assert rt._consent_pending_kind == "start"


def test_toggle_cloud_without_consent_reprompts(runtime, monkeypatch):
    rt, controller = runtime
    consents = []
    states = []
    controller.meeting_consent_requested.connect(lambda: consents.append(True))
    controller.meeting_state_changed.connect(lambda p: states.append(dict(p)))
    monkeypatch.setattr(rt, "_cloud_consent_given", lambda: False)

    rt.toggle_cloud(True)

    assert consents == [True]
    assert rt._consent_pending_kind == "toggle"
    assert states == []


def test_toggle_cloud_with_consent_applies(runtime, monkeypatch):
    rt, controller = runtime
    states = []
    controller.meeting_state_changed.connect(lambda p: states.append(dict(p)))
    monkeypatch.setattr(rt, "_cloud_consent_given", lambda: True)

    rt.toggle_cloud(True)

    assert rt._consent_pending_kind is None
    assert any(s.get("cloud_enabled") is True for s in states)


def test_recovery_scan_emits_dead_sessions(runtime, monkeypatch):
    rt, controller = runtime
    found = []
    controller.meeting_recovery_found.connect(found.append)
    meetings = [{"id": "m_dead", "status": "active"}]
    monkeypatch.setattr(rt, "_repository", lambda: object())
    monkeypatch.setattr(
        "meeting.recovery.find_recoverable_meetings",
        lambda repository: meetings,
    )

    rt._recovery_scan_worker()

    assert found == [meetings]


def test_recovery_scan_silent_when_none(runtime, monkeypatch):
    rt, controller = runtime
    found = []
    controller.meeting_recovery_found.connect(found.append)
    monkeypatch.setattr(rt, "_repository", lambda: object())
    monkeypatch.setattr(
        "meeting.recovery.find_recoverable_meetings",
        lambda repository: [],
    )

    rt._recovery_scan_worker()

    assert found == []


def test_finalize_recovered_passes_meeting_dict(runtime, monkeypatch):
    rt, controller = runtime
    statuses = []
    errors = []
    controller.meeting_status_update.connect(statuses.append)
    controller.meeting_error.connect(errors.append)
    meeting = {"id": "m_dead"}
    repo = SimpleNamespace(get_meeting=lambda meeting_id: meeting)
    called = []

    def fake_finalize(repository, row, asr_model="auto", asr_language="auto",
                      on_progress=None):
        called.append({
            "repository": repository,
            "meeting_id": row["id"],
            "asr_language": asr_language,
        })
        return True

    monkeypatch.setattr(rt, "_repository", lambda: repo)
    monkeypatch.setattr("meeting.recovery.finalize_meeting", fake_finalize)
    monkeypatch.setattr(
        "services.runtime.meeting.resolve_meeting_language",
        lambda settings: "en",
    )

    rt._finalize_recovered_worker("m_dead")

    assert called[0]["meeting_id"] == "m_dead"
    assert called[0]["repository"] is repo
    assert called[0]["asr_language"] == "en"
    assert "finalized" in statuses[-1].lower()
    assert errors == []


def test_finalize_recovered_reports_failure(runtime, monkeypatch):
    rt, controller = runtime
    errors = []
    controller.meeting_error.connect(errors.append)
    repo = SimpleNamespace(get_meeting=lambda meeting_id: {"id": meeting_id})
    monkeypatch.setattr(rt, "_repository", lambda: repo)
    monkeypatch.setattr(
        "meeting.recovery.finalize_meeting",
        lambda *args, **kwargs: False,
    )

    rt._finalize_recovered_worker("m_dead")

    assert errors
    assert "could not finalize" in errors[-1].lower()


def test_discard_recovered_deletes_meeting_data(runtime, monkeypatch):
    rt, controller = runtime
    statuses = []
    controller.meeting_status_update.connect(statuses.append)
    repo = object()
    deleted = []

    def fake_delete(repository, meeting_id, root):
        deleted.append((repository, meeting_id, root))

    monkeypatch.setattr(rt, "_repository", lambda: repo)
    monkeypatch.setattr(
        "meeting.persist.data_lifecycle.delete_meeting_data", fake_delete
    )

    rt._discard_recovered_worker("m_dead")

    assert deleted[0][0] is repo
    assert deleted[0][1] == "m_dead"
    assert "discarded" in statuses[-1].lower()

