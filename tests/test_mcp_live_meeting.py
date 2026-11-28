"""Tests for live Meeting Mode MCP & REST tools, full dashboard state, and job progress."""

from __future__ import annotations

from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from meeting.persist.repository import SqlMeetingRepository as MeetingRepository
from meeting.state.schema import CustomReport, FinalizationState, MeetingState, Participant
from meeting.state.store import MeetingStateStore
from services.agent_mcp.app import create_app
from services.automation import app_control_ext
from services.automation.control import ControlError, set_app_control
from services.database import DatabaseManager
from services.settings import SettingsKey, settings_manager
from tests.fakes.settings import InMemorySettings

TOKEN = "mcp-live-meeting-" + "d" * 32
MODERN = "2026-07-28"


class _FakeClock:
    def __init__(self, seconds: float = 123.45):
        self._seconds = seconds

    def now_s(self) -> float:
        return self._seconds


class _FakeScheduler:
    def __init__(self):
        self._pending_segments = 4
        self._guidance_pending = True
        self._notes_guidance_pending = False
        self._notes_resume_pending = False
        self._note_requests = ["Focus on Q3 budget"]


class _LiveEngineHarness:
    """Minimal live engine backed by a real MeetingStateStore + MeetingRepository."""

    def __init__(self, repo: MeetingRepository, meeting_id: str, *, active: bool = True):
        self.meeting_id = meeting_id
        self.repository = repo
        self._active = active
        self._paused = False
        self._me_participant_id = "p_me"
        self.clock = _FakeClock(185.6)
        self._scheduler = _FakeScheduler()
        self.options = SimpleNamespace(
            agent_core_kind="direct",
            llm_provider="openrouter",
            llm_model="gpt-4o-mini",
            llm_endpoint=None,
        )
        self.note_requests: list[str] = []
        self.next_note_future: Future | None = None

        from meeting.interfaces import TranscriptSegment

        repo.create_meeting(
            id=meeting_id,
            title="Weekly Sync",
            status="active" if active else "ended",
            started_at="2026-11-28T05:00:00Z",
            host_token=f"h_{meeting_id}",
            guest_token=f"g_{meeting_id}",
            spool_dir="",
        )
        from services.models import MeetingParticipant

        me_pid = "p_me" if active else f"p_me_{meeting_id}"
        spk_pid = "p_spk1" if active else f"p_spk1_{meeting_id}"
        self._me_participant_id = me_pid
        with repo._db.get_session() as session:
            session.add(MeetingParticipant(id=me_pid, meeting_id=meeting_id, display_name="Me", kind="me", name_source="default", created_at="2026-11-28T05:00:00Z", updated_at="2026-11-28T05:00:00Z"))
            session.add(MeetingParticipant(id=spk_pid, meeting_id=meeting_id, display_name="Speaker 1", kind="others_cluster", name_source="default", created_at="2026-11-28T05:00:00Z", updated_at="2026-11-28T05:00:00Z"))
        repo.add_segments([
            TranscriptSegment(
                segment_id=f"sg_{meeting_id}_1",
                meeting_id=meeting_id,
                chunk_id=None,
                channel="loopback",
                start_s=10.0,
                end_s=14.0,
                text="We should ship by Friday.",
                speaker_participant_id="p_spk1",
            )
        ])
        state = MeetingState(
            meeting_id=meeting_id,
            status="active" if active else "ended",
            cloud_enabled=True,
            intelligence_online=True,
            diarization_available=True,
            title="Weekly Sync",
            participants={
                "p_me": Participant(id="p_me", display_name="Me", kind="me"),
                "p_spk1": Participant(id="p_spk1", display_name="Speaker 1", kind="others_cluster"),
            },
            finalization=FinalizationState(
                status="running" if not active else "pending",
                message="Polishing transcript" if not active else "",
                stage="polish" if not active else "",
                current_step=2 if not active else 0,
                total_steps=4 if not active else 0,
                step_details="2/4 steps" if not active else "",
                steps=[
                    {"key": "redecode", "status": "completed", "detail": "done"},
                    {"key": "polish", "status": "running", "detail": "polishing"},
                ]
                if not active
                else [],
            ),
            live_highlights=[{"text": "Friday launch target", "start_s": 10.0}],
            live_highlights_status="active",
            capture={
                "mic_available": True,
                "loopback_available": True,
                "message": "",
                "audio_incomplete": False,
                "integrity_error": "",
            },
            custom_reports=[
                CustomReport(
                    id="rep_in_flight",
                    request="Executive summary",
                    status="running",
                    title="Executive Summary",
                    message="Reading transcript...",
                )
            ],
        )
        from meeting.state.store import repository_segment_lookup

        def _segment_handler(result):
            effect = result.effect or {}
            if result.op.get("op") == "reassign_segment_speaker":
                repo.update_segment_speaker(
                    meeting_id,
                    effect["segment_id"],
                    effect["participant_id"],
                    source=effect.get("source", "human"),
                    pinned=bool(effect.get("pinned", True)),
                )
            return None

        self.store = MeetingStateStore(
            state,
            repository=repo,
            segment_handler=_segment_handler,
            segment_lookup=repository_segment_lookup(repo, meeting_id),
        )
        self.store.update_runtime_fields(title="Weekly Sync")

    def is_paused(self) -> bool:
        return self._paused

    def is_active(self) -> bool:
        return self._active

    def set_cloud_enabled(self, enabled: bool) -> None:
        self.store.apply("host", self._me_participant_id, [{"op": "set_cloud_enabled", "enabled": bool(enabled)}])

    def apply_client_action(self, actor_type: str, actor_id, op: dict):
        return self.store.apply(actor_type, actor_id, [op])

    def undo(self, seq: int, actor_id):
        return self.store.undo(seq, actor_id)

    def request_note_adjustment(self, text: str):
        self.note_requests.append(text)
        if self.next_note_future is not None:
            fut = self.next_note_future
            self.next_note_future = None
            return fut
        fut: Future = Future()
        fut.set_result(
            SimpleNamespace(
                ok=True,
                op_results=[SimpleNamespace(ok=True), SimpleNamespace(ok=True)],
                error=None,
            )
        )
        return fut


class _RuntimeHarness:
    def __init__(self, repo: MeetingRepository, engine: _LiveEngineHarness | None, bg_engines=None):
        self._repo = repo
        self._engine = engine
        self._background_engines = dict(bg_engines or {})
        self._starting = False
        self._finalization = None
        self._card_meeting_id = None

    @property
    def is_active(self) -> bool:
        return bool(self._engine is not None and self._engine.is_active())

    @property
    def is_finalizing(self) -> bool:
        return bool(self._engine is not None and not self._engine.is_active())

    def _repository(self) -> MeetingRepository:
        return self._repo


def _make_controller(tmp_path):
    db = DatabaseManager(str(tmp_path / "live_meeting.db"))
    repo = MeetingRepository(db)
    live_engine = _LiveEngineHarness(repo, "m_live_1", active=True)
    bg_engine = _LiveEngineHarness(repo, "m_bg_1", active=False)
    runtime = _RuntimeHarness(repo, live_engine, bg_engines={"m_bg_1": bg_engine})
    controller = SimpleNamespace(meeting_runtime=runtime, database=db)
    return controller, db, repo, live_engine, bg_engine


class _DirectControlBridge:
    """AppControl adapter delegating directly to app_control_ext for MCP/REST tests."""

    def __init__(self, controller):
        self._controller = controller

    def meeting_status(self):
        return app_control_ext.meeting_status(self._controller)

    def get_meeting_dashboard_state(self, *, meeting_id=None):
        return app_control_ext.get_meeting_dashboard_state(self._controller, meeting_id=meeting_id)

    def update_live_meeting(self, *, title=None, intent=None, topic=None, cloud_enabled=None):
        return app_control_ext.update_live_meeting(
            self._controller, title=title, intent=intent, topic=topic, cloud_enabled=cloud_enabled
        )

    def manage_meeting_card_item(self, *, action, card=None, item_id=None, text=None, data=None, evidence=None):
        return app_control_ext.manage_meeting_card_item(
            self._controller,
            action=action,
            card=card,
            item_id=item_id,
            text=text,
            data=data,
            evidence=evidence,
        )

    def manage_meeting_question(self, *, action, text=None, question_id=None, answer_text=None, evidence=None):
        return app_control_ext.manage_meeting_question(
            self._controller,
            action=action,
            text=text,
            question_id=question_id,
            answer_text=answer_text,
            evidence=evidence,
        )

    def update_meeting_transcript_or_speaker(
        self, *, action, participant_id=None, display_name=None, segment_id=None, text=None
    ):
        return app_control_ext.update_meeting_transcript_or_speaker(
            self._controller,
            action=action,
            participant_id=participant_id,
            display_name=display_name,
            segment_id=segment_id,
            text=text,
        )

    def request_meeting_note_adjustment(self, *, text, wait=True, timeout_s=60.0):
        return app_control_ext.request_meeting_note_adjustment(
            self._controller, text=text, wait=wait, timeout_s=timeout_s
        )

    def request_meeting_custom_report(self, *, request_text, meeting_id=None):
        return app_control_ext.request_meeting_custom_report(
            self._controller, request_text=request_text, meeting_id=meeting_id
        )

    def undo_live_meeting_action(self, *, event_seq):
        return app_control_ext.undo_live_meeting_action(self._controller, event_seq=event_seq)


def _rpc(client: TestClient, method: str, params: dict | None = None, *, req_id: int = 1):
    params = dict(params or {})
    params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MODERN,
        "Mcp-Method": method,
    }
    if method in ("tools/call", "prompts/get"):
        headers["Mcp-Name"] = params["name"]
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _call_tool(client: TestClient, name: str, arguments: dict | None = None):
    res = _rpc(client, "tools/call", {"name": name, "arguments": arguments or {}})
    assert "result" in res, res
    return res["result"]


def test_meeting_status_and_dashboard_state_expose_live_and_job_progress(tmp_path):
    controller, _db, repo, _live_engine, _bg_engine = _make_controller(tmp_path)

    status = app_control_ext.meeting_status(controller)
    assert status["active"] is True
    assert status["meeting_id"] == "m_live_1"
    assert status["elapsed_s"] == 185.6
    assert status["title"] == "Weekly Sync"
    assert status["cloud_enabled"] is True
    assert status["intelligence_online"] is True
    assert status["capture"]["mic_available"] is True
    assert status["counts"]["participants"] == 2
    assert status["counts"]["live_highlights"] == 1
    assert status["scheduler"]["pending_segments"] == 4
    assert status["scheduler"]["note_requests_pending"] == 1
    assert status["running_reports"][0]["id"] == "rep_in_flight"
    assert status["background_count"] == 1
    assert status["background_jobs"][0]["meeting_id"] == "m_bg_1"
    assert status["background_jobs"][0]["finalization"]["stage"] == "polish"
    assert status["background_jobs"][0]["finalization"]["current_step"] == 2
    assert status["background_jobs"][0]["finalization"]["total_steps"] == 4
    assert len(status["background_jobs"][0]["finalization"]["steps"]) == 2

    # Active meeting dashboard snapshot (no meeting_id needed)
    dash = app_control_ext.get_meeting_dashboard_state(controller)
    assert dash["meeting_id"] == "m_live_1"
    assert dash["is_live"] is True
    assert dash["elapsed_s"] == 185.6
    assert dash["state"]["live_highlights"][0]["text"] == "Friday launch target"
    assert dash["jobs"]["scheduler"]["pending_segments"] == 4
    assert dash["jobs"]["running_reports"][0]["id"] == "rep_in_flight"

    # Background finalizing meeting dashboard snapshot
    bg_dash = app_control_ext.get_meeting_dashboard_state(controller, meeting_id="m_bg_1")
    assert bg_dash["is_live"] is False
    assert bg_dash["finalizing"] is True
    assert bg_dash["jobs"]["finalization"]["stage"] == "polish"

    # Archived meeting snapshot normalizes historical finalization
    archived = MeetingState(
        meeting_id="m_old_1",
        status="ended",
        cloud_enabled=True,
        title="Old Sync",
        finalization=FinalizationState(status="running", message="Interrupted"),
    )
    repo.create_meeting(
        id="m_old_1",
        title="Old Sync",
        status="ended",
        started_at="2026-11-27T05:00:00Z",
        host_token="h_old_1",
        guest_token="g_old_1",
        spool_dir="",
    )
    import json

    repo.update_meeting("m_old_1", status="ended", state_json=json.dumps(archived.to_dict()))
    old_dash = app_control_ext.get_meeting_dashboard_state(controller, meeting_id="m_old_1")
    assert old_dash["is_live"] is False
    assert old_dash["jobs"]["finalization"]["status"] == "failed"


def test_live_meeting_interactive_edits_and_undo(tmp_path, monkeypatch):
    controller, _db, repo, live_engine, _bg_engine = _make_controller(tmp_path)
    mem = InMemorySettings()
    mem.save_setting(SettingsKey.MEETING_CLOUD_CONSENT_GIVEN, False)
    monkeypatch.setattr(settings_manager, "load_all_settings", mem.load_all_settings)
    monkeypatch.setattr(settings_manager, "save_setting", mem.save_setting)

    # Cloud toggle refuses when consent not given
    with pytest.raises(ControlError) as exc_info:
        app_control_ext.update_live_meeting(controller, cloud_enabled=True)
    assert exc_info.value.code == "refused"

    # Turning cloud off works, then giving consent allows turning it back on
    res_off = app_control_ext.update_live_meeting(
        controller, title="Q3 Planning", intent="Align on Q3 launch dates", topic="Timeline", cloud_enabled=False
    )
    assert res_off["ok"] is True
    assert set(res_off["updated"]) == {"title", "intent", "topic", "cloud_enabled"}
    assert live_engine.store.snapshot()["title"] == "Q3 Planning"
    assert live_engine.store.snapshot()["intent"]["text"] == "Align on Q3 launch dates"
    assert live_engine.store.snapshot()["topic"]["current"] == "Timeline"
    assert live_engine.store.snapshot()["cloud_enabled"] is False

    mem.save_setting(SettingsKey.MEETING_CLOUD_CONSENT_GIVEN, True)
    app_control_ext.update_live_meeting(controller, cloud_enabled=True)
    assert live_engine.store.snapshot()["cloud_enabled"] is True

    # Card lifecycle: add -> update -> pin -> unpin -> confirm -> remove -> undo
    added = app_control_ext.manage_meeting_card_item(
        controller,
        action="add",
        card="action_items",
        text="Send launch checklist",
        data={"owner_participant_id": "p_me"},
        evidence=["sg_m_live_1_1"],
    )
    item_id = added["item_id"]
    assert added["ok"] is True
    assert added["item"]["text"] == "Send launch checklist"

    updated = app_control_ext.manage_meeting_card_item(
        controller, action="update", item_id=item_id, text="Send launch checklist by Thursday"
    )
    assert updated["item"]["text"] == "Send launch checklist by Thursday"

    pinned = app_control_ext.manage_meeting_card_item(controller, action="pin", item_id=item_id)
    assert pinned["item"]["pinned"] is True
    unpinned = app_control_ext.manage_meeting_card_item(controller, action="unpin", item_id=item_id)
    assert unpinned["item"]["pinned"] is False
    confirmed = app_control_ext.manage_meeting_card_item(controller, action="confirm", item_id=item_id)
    assert confirmed["item"]["status"] == "confirmed"

    removed = app_control_ext.manage_meeting_card_item(controller, action="remove", item_id=item_id)
    assert removed["seq"] is not None
    undone = app_control_ext.undo_live_meeting_action(controller, event_seq=removed["seq"])
    assert undone["ok"] is True
    assert live_engine.store.with_state(lambda s: s.find_item(item_id).status) != "removed"

    # Questions lifecycle: ask -> answer -> reopen -> dismiss
    asked = app_control_ext.manage_meeting_question(controller, action="ask", text="Is QA signed off?")
    qid = asked["question_id"]
    assert asked["question"]["status"] == "open"
    answered = app_control_ext.manage_meeting_question(
        controller, action="answer", question_id=qid, answer_text="Yes, sign-off completed yesterday."
    )
    assert answered["question"]["status"] == "resolved"
    reopened = app_control_ext.manage_meeting_question(controller, action="reopen", question_id=qid)
    assert reopened["question"]["status"] == "open"
    dismissed = app_control_ext.manage_meeting_question(controller, action="dismiss", question_id=qid)
    assert dismissed["question"]["status"] == "dismissed"

    # Participant rename + segment speaker reassignment + segment revision
    renamed = app_control_ext.update_meeting_transcript_or_speaker(
        controller, action="rename_participant", participant_id="p_spk1", display_name="Sarah"
    )
    assert renamed["ok"] is True
    assert live_engine.store.snapshot()["participants"]["p_spk1"]["display_name"] == "Sarah"

    reassigned = app_control_ext.update_meeting_transcript_or_speaker(
        controller, action="reassign_speaker", segment_id="sg_m_live_1_1", participant_id="p_me"
    )
    assert reassigned["ok"] is True
    seg = repo.get_segment("m_live_1", "sg_m_live_1_1")
    assert seg["speaker_participant_id"] == "p_me"
    assert seg["speaker_pinned"] is True

    # Note adjustment: wait=True, wait=False, and timeout
    note_done = app_control_ext.request_meeting_note_adjustment(controller, text="Add a budget section", wait=True)
    assert note_done["ok"] is True
    assert note_done["status"] == "completed"
    assert note_done["applied"] == 2

    note_queued = app_control_ext.request_meeting_note_adjustment(controller, text="Track risks", wait=False)
    assert note_queued["status"] == "queued"

    pending_future: Future = Future()
    live_engine.next_note_future = pending_future
    note_running = app_control_ext.request_meeting_note_adjustment(
        controller, text="Slow request", wait=True, timeout_s=0.01
    )
    assert note_running["ok"] is True
    assert note_running["status"] == "running"


def test_request_custom_report_and_mcp_rest_wiring(tmp_path, monkeypatch):
    controller, db, _repo, _live_engine, _bg_engine = _make_controller(tmp_path)
    bridge = _DirectControlBridge(controller)
    set_app_control(bridge)

    started_calls = []

    def _fake_start_custom_report(store, repository, text, *, provider, model, endpoint):
        started_calls.append((text, provider, model))
        return {"ok": True, "report_id": "rep_new_1", "run_id": "run_1"}

    monkeypatch.setattr("meeting.custom_report.start_custom_report", _fake_start_custom_report)

    settings = InMemorySettings()
    settings.save_setting(SettingsKey.API_CONTROL_MEETINGS, False)
    app = create_app(str(tmp_path / "live_meeting.db"), TOKEN, settings=settings)

    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            # Read-only tools work without CONTROL_MEETINGS
            live_state = _call_tool(client, "get_meeting_live_state")
            assert not live_state["isError"]
            assert live_state["structuredContent"]["elapsed_s"] == 185.6

            dash = _call_tool(client, "get_meeting_dashboard_state")
            assert not dash["isError"]
            assert dash["structuredContent"]["state"]["title"] == "Weekly Sync"

            # Mutating tools are refused when CONTROL_MEETINGS is False
            denied = _call_tool(client, "update_live_meeting", {"topic": "Blocked"})
            assert denied["isError"] is True

            # Enable CONTROL_MEETINGS and verify MCP + REST parity
            settings.save_setting(SettingsKey.API_CONTROL_MEETINGS, True)

            upd = _call_tool(client, "update_live_meeting", {"topic": "Launch Plan"})
            assert not upd["isError"]
            assert upd["structuredContent"]["ok"] is True

            card = _call_tool(
                client,
                "manage_meeting_card_item",
                {"action": "add", "card": "decisions", "text": "Freeze code Thursday"},
            )
            assert not card["isError"]
            assert card["structuredContent"]["item"]["text"] == "Freeze code Thursday"

            q = _call_tool(
                client,
                "manage_meeting_question",
                {"action": "ask", "text": "Who owns release notes?"},
            )
            assert not q["isError"]
            assert q["structuredContent"]["question"]["status"] == "open"

            rep = _call_tool(
                client,
                "request_meeting_custom_report",
                {"request_text": "List all commitments with owners"},
            )
            assert not rep["isError"]
            assert rep["structuredContent"]["report_id"] == "rep_new_1"
            assert started_calls[0][0] == "List all commitments with owners"

            # REST parity checks
            headers = {"Authorization": f"Bearer {TOKEN}"}
            r_state = client.get("/v1/meetings/live/state", headers=headers)
            assert r_state.status_code == 200
            assert r_state.json()["state"]["topic"]["current"] == "Launch Plan"

            r_note = client.post(
                "/v1/meetings/live/notes/request",
                json={"text": "Highlight launch blockers", "wait": True},
                headers=headers,
            )
            assert r_note.status_code == 200
            assert r_note.json()["status"] == "completed"
    finally:
        set_app_control(None)
