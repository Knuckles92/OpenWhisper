"""Focused tests for MeetingRuntime finalization bridging and guards."""

from __future__ import annotations

import json
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

_RUNTIME_GLOBALS = MeetingRuntime.__init__.__globals__


class _Controller(QObject):
    """Minimal ApplicationController stand-in with meeting signals."""

    meeting_status_update = pyqtSignal(str)
    meeting_state_changed = pyqtSignal(object)
    meeting_error = pyqtSignal(str)
    meeting_server_started = pyqtSignal(object)
    meeting_guest_link_ready = pyqtSignal(str)
    meeting_consent_requested = pyqtSignal()
    meeting_recovery_found = pyqtSignal(object)
    past_meetings_refresh_requested = pyqtSignal()
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
    runtime_settings = _RUNTIME_GLOBALS["settings_manager"]
    monkeypatch.setattr(runtime_settings, "save_setting", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runtime_settings, "get", lambda key, default=False: default
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


def test_launch_reports_starting_without_claiming_active(runtime, monkeypatch):
    rt, controller = runtime
    states = []
    controller.meeting_state_changed.connect(lambda p: states.append(dict(p)))
    worker = MagicMock()
    monkeypatch.setattr(
        _RUNTIME_GLOBALS["threading"],
        "Thread",
        lambda *args, **kwargs: worker,
    )
    rt._starting = True

    rt._launch(False)

    assert controller.meeting_active is False
    assert rt.is_claimed is True
    assert states[-1]["status"] == "starting"
    assert states[-1]["active"] is False
    worker.start.assert_called_once()


def test_start_worker_failure_rolls_back_active_state(runtime, monkeypatch):
    rt, controller = runtime
    states = []
    errors = []
    controller.meeting_state_changed.connect(lambda p: states.append(dict(p)))
    controller.meeting_error.connect(errors.append)

    class FailingEngine:
        def __init__(self, *args, **kwargs):
            pass

        def add_listener(self, listener):
            self.listener = listener

        def start(self):
            raise RuntimeError("No audio devices could be opened.")

    monkeypatch.setattr("meeting.engine.MeetingEngine", FailingEngine)
    monkeypatch.setattr(rt, "_build_options", lambda *args, **kwargs: object())
    monkeypatch.setitem(
        _RUNTIME_GLOBALS, "speaker_model_path", lambda: "/cached/model"
    )
    rt._starting = True
    rt._card_meeting_id = "m_partial"
    rt._finalization = {"status": "pending"}

    rt._start_worker(False)

    assert rt._starting is False
    assert controller.meeting_active is False
    assert rt._engine is None
    assert states[-1] == {
        "active": False,
        "paused": False,
        "status": "failed",
        "dashboard_available": False,
        "finalization": None,
        "meeting_id": None,
    }
    assert rt._card_meeting_id is None
    assert rt._finalization is None
    assert "No audio devices" in errors[-1]


def test_nonfatal_engine_error_is_deferred_during_start(runtime):
    rt, controller = runtime
    errors = []
    controller.meeting_error.connect(errors.append)
    rt._starting = True

    rt._on_engine_event(
        "error",
        {"code": "asr_unavailable", "message": "Model is unavailable"},
    )

    assert errors == []
    assert rt._deferred_start_errors == ["Model is unavailable"]


def test_open_dashboard_without_url_surfaces_error(runtime):
    rt, controller = runtime
    errors = []
    statuses = []
    controller.meeting_error.connect(errors.append)
    controller.meeting_status_update.connect(statuses.append)

    rt.open_dashboard()

    assert errors == [
        "No meeting dashboard is available. Start a meeting or open one "
        "from Past Meetings."
    ]
    assert statuses == []


def test_open_dashboard_for_retained_card_uses_archive_path(runtime, monkeypatch):
    rt, _controller = runtime
    opened = []
    rt._card_meeting_id = "m_saved"
    monkeypatch.setattr(rt, "open_past_meeting", opened.append)

    rt.open_dashboard()

    assert opened == ["m_saved"]


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


def test_start_meeting_blocked_while_whisper_loading(runtime, monkeypatch):
    rt, controller = runtime
    statuses = []
    controller.meeting_status_update.connect(statuses.append)
    controller.local_whisper_loading_message = (
        lambda: "Whisper engine is still loading..."
    )
    launched = _record_launch(rt, monkeypatch)

    rt.start_meeting(cloud_enabled=False)

    assert launched == []
    assert any("still loading" in s.lower() for s in statuses)


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


def test_restore_deferred_latest_leaves_idle_and_does_not_hydrate_older(runtime):
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

    assert rt._card_meeting_id is None
    assert rt._finalization is None
    assert states == []


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


def test_defer_completed_card_uses_saved_status(runtime):
    rt, controller = runtime
    statuses = []
    controller.meeting_status_update.connect(statuses.append)
    meeting = _ended_meeting("m_done", status="completed")
    fake_repo = MagicMock()
    fake_repo.get_meeting.return_value = meeting
    persisted = []
    fake_repo.persist_state.side_effect = lambda meeting_id, data: persisted.append(
        (meeting_id, data)
    )
    rt._repo = fake_repo
    rt._card_meeting_id = "m_done"
    rt._finalization = {"status": "completed", "message": "ready"}

    assert rt.defer_finalization_card() is True
    assert rt._finalization is None
    assert persisted[0][1]["finalization"]["card_deferred"] is True
    assert any(
        status == "Meeting saved. Open it from Past Meetings."
        for status in statuses
    )
    assert not any("for later" in status for status in statuses)


def test_begin_start_files_leftover_card(runtime, monkeypatch):
    rt, controller = runtime
    meeting = _ended_meeting("m_card", status="completed")
    fake_repo = MagicMock()
    fake_repo.get_meeting.return_value = meeting
    persisted = []
    fake_repo.persist_state.side_effect = lambda meeting_id, data: persisted.append(
        (meeting_id, data)
    )
    rt._repo = fake_repo
    rt._card_meeting_id = "m_card"
    rt._finalization = {"status": "completed", "message": "ready"}
    launched = _record_launch(rt, monkeypatch)
    monkeypatch.setattr(rt, "_cloud_consent_given", lambda: True)

    rt.start_meeting(False)

    assert persisted[0][1]["finalization"]["card_deferred"] is True
    assert launched == [{"cloud": False, "demo": False}]
    assert rt._card_meeting_id is None


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


def test_refresh_past_meetings_from_worker_uses_signal(runtime):
    """Opening a deferred meeting must not rebuild Qt widgets on the worker."""
    import threading

    rt, controller = runtime
    refreshed = []
    controller.past_meetings_refresh_requested.connect(lambda: refreshed.append(True))

    def worker():
        rt._refresh_past_meetings()

    thread = threading.Thread(target=worker, name="meeting-history-dashboard")
    thread.start()
    thread.join(timeout=2)
    assert thread.is_alive() is False
    assert refreshed == []
    qapp = QCoreApplication.instance()
    assert qapp is not None
    qapp.processEvents()
    assert refreshed == [True]


def test_retry_finalization_from_worker_uses_signal(runtime, monkeypatch):
    """Retry must not rebuild Qt widgets on the finalization worker."""
    import time

    rt, controller = runtime
    controller.meeting_active = False
    rt._engine = None
    rt._card_meeting_id = "m_card"
    fake_repo = MagicMock()
    fake_repo.get_meeting.return_value = {"id": "m_card", "asr_model": "base"}
    rt._repo = fake_repo
    refreshed = []
    controller.past_meetings_refresh_requested.connect(lambda: refreshed.append(True))

    def fake_rerun(repository, meeting_id, **kwargs):
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

    assert rt.is_finalizing is False
    assert refreshed == []
    qapp = QCoreApplication.instance()
    assert qapp is not None
    qapp.processEvents()
    assert refreshed == [True]


def test_show_past_meeting_hydrates_without_opening_browser(runtime, monkeypatch):
    rt, controller = runtime
    states = []
    controller.meeting_state_changed.connect(lambda p: states.append(dict(p)))
    meeting = _ended_meeting("m_saved", deferred=True)
    meeting["title"] = ""
    meeting["started_at"] = "2025-01-02T09:30:00"
    meeting["ended_at"] = "2025-01-02T10:12:00"
    meeting["paused_total_s"] = 120
    fake_repo = MagicMock()
    fake_repo.get_meeting.return_value = meeting
    fake_repo.persist_state.side_effect = lambda *args, **kwargs: None
    rt._repo = fake_repo
    opened = []
    monkeypatch.setattr(
        "services.runtime.meeting.webbrowser.open",
        lambda url: opened.append(url),
    )

    rt.show_past_meeting("m_saved")

    assert opened == []
    assert rt._card_meeting_id == "m_saved"
    assert states[-1]["meeting_id"] == "m_saved"
    assert states[-1]["display_title"]
    assert states[-1]["started_at"] == "2025-01-02T09:30:00"


def test_open_report_targets_history_report_view(runtime, monkeypatch):
    rt, _controller = runtime
    called = []

    def fake_open(meeting_id, *, view=""):
        called.append((meeting_id, view))

    rt._card_meeting_id = "m_done"
    monkeypatch.setattr(rt, "open_past_meeting", fake_open)

    rt.open_report()

    assert called == [("m_done", "report")]


def test_show_past_meeting_refuses_while_live(runtime, monkeypatch):
    rt, controller = runtime
    statuses = []
    controller.meeting_status_update.connect(statuses.append)
    controller.meeting_active = True
    fake_repo = MagicMock()
    rt._repo = fake_repo
    opened = []
    monkeypatch.setattr(
        "services.runtime.meeting.webbrowser.open",
        lambda url: opened.append(url),
    )

    rt.show_past_meeting("m_saved")

    assert opened == []
    fake_repo.get_meeting.assert_not_called()
    assert statuses == ["Finish the current meeting first."]


def test_open_past_meeting_reveals_without_persisting_deferral(runtime):
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
    assert persisted == []


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

    monkeypatch.setattr(_RUNTIME_GLOBALS["settings_manager"], "get", fake_get)
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
    monkeypatch.setitem(
        _RUNTIME_GLOBALS, "resolve_meeting_language", lambda settings: "en"
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


def test_copy_past_meeting_transcript_exports_text(runtime, monkeypatch):
    rt, controller = runtime
    statuses = []
    controller.meeting_status_update.connect(statuses.append)
    meeting = {
        "id": "m_copy",
        "title": "Planning",
        "started_at": "2025-01-02T09:30:00",
        "state_json": json.dumps({"title": "Planning", "participants": {}}),
    }
    repo = SimpleNamespace(
        get_meeting=lambda meeting_id: meeting,
        get_segments=lambda meeting_id: [
            {"text": "Hello team", "start_s": 0.0, "channel": "mic"},
        ],
    )
    monkeypatch.setattr(rt, "_repository", lambda: repo)

    text = rt.copy_past_meeting_transcript("m_copy")

    assert "Hello team" in text
    assert "Planning" in text


def test_copy_past_meeting_transcript_empty(runtime, monkeypatch):
    rt, controller = runtime
    statuses = []
    controller.meeting_status_update.connect(statuses.append)
    repo = SimpleNamespace(
        get_meeting=lambda meeting_id: {"id": meeting_id, "state_json": "{}"},
        get_segments=lambda meeting_id: [],
    )
    monkeypatch.setattr(rt, "_repository", lambda: repo)

    assert rt.copy_past_meeting_transcript("m_empty") is None
    assert any("no transcript" in s.lower() for s in statuses)


def test_delete_past_meeting_refuses_while_live(runtime):
    rt, controller = runtime
    statuses = []
    controller.meeting_status_update.connect(statuses.append)
    controller.meeting_active = True
    rt._engine = SimpleNamespace(meeting_id="m_live")

    rt.delete_past_meeting("m_live")

    assert any("finish the current meeting" in s.lower() for s in statuses)


def test_delete_past_meeting_hides_card_and_deletes(runtime, monkeypatch):
    rt, controller = runtime
    statuses = []
    refreshed = []
    controller.meeting_status_update.connect(statuses.append)
    controller.past_meetings_refresh_requested.connect(lambda: refreshed.append(True))
    deleted = []

    def fake_delete(repository, meeting_id, root):
        deleted.append(meeting_id)

    monkeypatch.setattr(rt, "_repository", lambda: object())
    monkeypatch.setattr(
        "meeting.persist.data_lifecycle.delete_meeting_data", fake_delete
    )
    rt._card_meeting_id = "m_old"

    rt._delete_past_meeting_worker("m_old")

    assert deleted == ["m_old"]
    assert rt._card_meeting_id is None
    assert any("deleted" in s.lower() for s in statuses)


