"""Title contracts and multiple dashboard snapshots over one disposable database."""

import asyncio
import json
import threading
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from meeting.interfaces import OpResult
from meeting.state.schema import CustomReport, MeetingState
from meeting.stored import open_store
from meeting.web.archive import ArchivedMeetingDashboard
from meeting.web.server import MeetingWebServer
from services.agent_mcp.controls import AgentControls, ControlError
from services.runtime.meeting import MeetingRuntime
from services.settings import SettingsKey, SettingsManager
from services.titles import MAX_TITLE_LENGTH
from tests.helpers import make_meeting


@pytest.fixture
def title_case(repo, db, tmp_path):
    meeting_id = make_meeting(repo)
    state = MeetingState(meeting_id=meeting_id, title="Test meeting", status="ended")
    state.custom_reports = [CustomReport(id=f"r_{i}", request="Summary", status="completed")
                            for i in range(4)]
    repo.persist_state(meeting_id, state.to_dict())
    other_id = make_meeting(repo, "m_other")
    settings = SettingsManager(str(tmp_path / "title-settings.json"))
    settings.save_setting(SettingsKey.MCP_RETITLE_MEETINGS, True)
    agent = AgentControls(Path(db.db_path), settings)
    return repo, agent, meeting_id, other_id


class InProcessDashboard:
    """Exercise each dashboard's own event loop without opening sockets."""

    def __init__(self, repository, meeting_id, stack):
        self.engine = ArchivedMeetingDashboard(
            repository, repository.get_meeting(meeting_id), spool_root="unused"
        )
        self.server = MeetingWebServer(self.engine, repository)
        self.client = stack.enter_context(TestClient(self.server.app))
        self.server._loop = self.client.portal.call(asyncio.get_running_loop)
        self.engine.attach_server(self)

    def is_running(self):
        return True

    def retitle_saved_meeting(self, meeting_id, title):
        return self.server.retitle_saved_meeting(meeting_id, title)

    def refresh_saved_meeting_title(self, meeting_id):
        self.server.refresh_saved_meeting_title(meeting_id)

    def delete_report(self, meeting_id, report_id):
        response = self.client.delete(
            f"/api/meetings/{meeting_id}/reports/{report_id}", params={"token": "host-token"}
        )
        assert response.status_code == 200, response.text
        return response.json()

    def detail(self, meeting_id):
        response = self.client.get(
            f"/api/meetings/{meeting_id}", params={"token": "host-token"}
        )
        assert response.status_code == 200, response.text
        return response.json()


def runtime_for(repo, *dashboards):
    runtime = SimpleNamespace(
        _lock=threading.RLock(), _archive_starting=False,
        meeting_busy=lambda _: False, _repository=lambda: repo,
        _engine=dashboards[0].engine if dashboards else None,
        _archive_dashboard=dashboards[1].engine if len(dashboards) > 1 else None,
        _background_engines={str(i): d.engine for i, d in enumerate(dashboards[2:])},
    )
    return lambda mid, title: MeetingRuntime.retitle_saved_meeting(runtime, mid, title)


def assert_saved_title(repo, meeting_id, title):
    row = repo.get_meeting(meeting_id)
    assert row["title"] == title
    assert json.loads(row["state_json"])["title"] == title
    assert open_store(repo, meeting_id, row, historical=True).snapshot()["title"] == title


@pytest.mark.parametrize("selected_is_owner", [False, True])
def test_mcp_rename_refreshes_all_dashboard_caches(title_case, selected_is_owner):
    repo, agent, meeting_id, other_id = title_case
    with ExitStack() as stack:
        primary = InProcessDashboard(repo, meeting_id if selected_is_owner else other_id, stack)
        secondary = InProcessDashboard(repo, other_id, stack)
        background = InProcessDashboard(repo, other_id, stack)
        secondary.delete_report(meeting_id, "r_0")
        background.delete_report(meeting_id, "r_1")
        agent.meeting_renamer = runtime_for(repo, primary, secondary, background)

        result = agent.retitle("meeting", meeting_id, "Renamed through MCP")
        assert result["title"] == "Renamed through MCP"
        for dashboard in (primary, secondary, background):
            detail = dashboard.detail(meeting_id)
            assert detail["meeting"]["title"] == detail["state"]["title"] == result["title"]

        for dashboard, report_id in ((secondary, "r_2"), (background, "r_3")):
            assert dashboard.delete_report(meeting_id, report_id)["state"]["title"] == result["title"]
            assert_saved_title(repo, meeting_id, result["title"])
            assert dashboard.detail(meeting_id)["state"]["title"] == result["title"]

        reopened = InProcessDashboard(repo, meeting_id, stack)
        assert reopened.detail(meeting_id)["state"]["title"] == result["title"]


@pytest.mark.parametrize("write", ["op", "runtime", "repository"])
def test_stale_snapshot_cannot_retitle_without_an_explicit_title_op(title_case, write):
    repo, agent, meeting_id, _ = title_case
    stale = open_store(repo, meeting_id, repo.get_meeting(meeting_id), historical=True)
    agent.retitle("meeting", meeting_id, "Canonical title")
    if write == "op":
        assert stale.apply("host", None, [{"op": "remove_custom_report", "report_id": "r_0"}])[0].ok
        assert stale.snapshot()["title"] == "Canonical title"
    elif write == "runtime":
        assert stale.update_runtime_fields(status="ended")
        assert stale.snapshot()["title"] == "Canonical title"
    else:
        snapshot = stale.snapshot()
        persisted = repo.persist_state(meeting_id, snapshot)
        assert_saved_title(repo, meeting_id, "Canonical title")
        assert persisted["title"] == "Canonical title"
        assert snapshot["title"] == "Test meeting"
    assert_saved_title(repo, meeting_id, "Canonical title")


def test_explicit_title_op_and_undo_still_update_both_title_fields(title_case):
    repo, _, meeting_id, _ = title_case
    store = open_store(repo, meeting_id, repo.get_meeting(meeting_id), historical=True)
    result = store.apply("host", None, [{"op": "set_title", "text": "New title"}])[0]
    assert result.ok
    assert_saved_title(repo, meeting_id, "New title")
    assert store.undo(result.seq, None)[0].ok
    assert_saved_title(repo, meeting_id, "Test meeting")


def test_cache_refresh_failure_does_not_report_committed_rename_as_failed(title_case):
    repo, agent, meeting_id, other_id = title_case
    with ExitStack() as stack:
        primary = InProcessDashboard(repo, meeting_id, stack)
        secondary = InProcessDashboard(repo, other_id, stack)
        secondary.delete_report(meeting_id, "r_0")
        secondary.refresh_saved_meeting_title = MagicMock(side_effect=RuntimeError("closed"))
        agent.meeting_renamer = runtime_for(repo, primary, secondary)
        agent.retitle("meeting", meeting_id, "Committed")
        assert secondary.delete_report(meeting_id, "r_1")["state"]["title"] == "Committed"
        assert_saved_title(repo, meeting_id, "Committed")


VALID_TITLES = ["x", "x" * 120, "x" * 121, "x" * 200, "📝" * 200, "  New title  "]
INVALID_TITLES = [None, 123, "", "   ", "x" * 201, "📝" * 201, "bad\nname", "bad\x00name", "bad\x7fname"]


@pytest.mark.parametrize("title", VALID_TITLES + INVALID_TITLES)
@pytest.mark.parametrize("route", ["repository", "state", "mcp", "rest_current", "rest_cached", "rest_uncached", "mcp_current", "mcp_cached", "mcp_uncached"])
def test_every_title_write_path_uses_the_same_contract(title_case, route, title):
    repo, agent, meeting_id, other_id = title_case
    valid = title in VALID_TITLES
    with ExitStack() as stack:
        dashboard = None
        if "current" in route or "cached" in route:
            dashboard = InProcessDashboard(repo, meeting_id if "current" in route else other_id, stack)
            if route.endswith("_cached"):
                dashboard.delete_report(meeting_id, "r_0")
            agent.meeting_renamer = runtime_for(repo, dashboard)
        if route.startswith("rest"):
            response = dashboard.client.post(f"/api/meetings/{meeting_id}/rename",
                                             params={"token": "host-token"}, json={"title": title})
            assert response.status_code == (200 if valid else 400), response.text
            if valid:
                assert response.json() == {"ok": True, "title": title.strip()}
        elif route == "state":
            store = open_store(repo, meeting_id, repo.get_meeting(meeting_id), historical=True)
            result = store.apply("host", None, [{"op": "set_title", "text": title}])[0]
            assert result.ok == valid
        else:
            action = repo.rename_meeting if route == "repository" else lambda mid, value: agent.retitle("meeting", mid, value)
            if valid:
                action(meeting_id, title)
            else:
                with pytest.raises((ValueError, ControlError), match="1–200"):
                    action(meeting_id, title)
        assert_saved_title(repo, meeting_id, title.strip() if valid else "Test meeting")


def test_mcp_advertises_the_shared_length_contract():
    from services.agent_mcp.app import Title

    adapter = TypeAdapter(Title)
    assert adapter.json_schema()["maxLength"] == MAX_TITLE_LENGTH
    assert adapter.validate_python("📝" * MAX_TITLE_LENGTH) == "📝" * MAX_TITLE_LENGTH
    with pytest.raises(ValidationError):
        adapter.validate_python("x" * (MAX_TITLE_LENGTH + 1))


def test_repository_rejects_invalid_explicit_title_without_partial_audit(title_case):
    repo, _, meeting_id, _ = title_case
    state = MeetingState(meeting_id=meeting_id, title="stale").to_dict()
    with pytest.raises(ValueError):
        repo.on_ops_applied(meeting_id, state, [OpResult(ok=True, seq=1, op={"op": "set_title", "text": "x" * 201})], "host", None)
    assert repo.list_events(meeting_id) == []
    assert_saved_title(repo, meeting_id, "Test meeting")


def test_live_engine_title_and_runtime_updates_follow_the_same_contract(title_case):
    from meeting.engine import MeetingEngine, MeetingEngineOptions

    repo, _, meeting_id, _ = title_case
    engine = MeetingEngine(MeetingEngineOptions(), repository=repo)
    engine.meeting_id = meeting_id
    engine.store = open_store(repo, meeting_id, repo.get_meeting(meeting_id))
    title = "📝" * MAX_TITLE_LENGTH
    assert engine.apply_client_action("host", None, {"op": "set_title", "text": title})[0].ok
    assert_saved_title(repo, meeting_id, title)
    repo.rename_meeting(meeting_id, "External rename")
    assert engine.store.update_runtime_fields(status="ended")
    assert engine.store.snapshot()["title"] == "External rename"
    assert_saved_title(repo, meeting_id, "External rename")


def test_refresh_of_current_dashboard_reconnects_without_writing_state(title_case):
    repo, _, meeting_id, _ = title_case
    with ExitStack() as stack:
        dashboard = InProcessDashboard(repo, meeting_id, stack)
        invalidate = dashboard.server._hub.schedule_invalidate_connections = MagicMock()
        original = dashboard.engine.store.snapshot()
        repo.rename_meeting(meeting_id, "External rename")
        dashboard.refresh_saved_meeting_title(meeting_id)
        assert dashboard.engine.store.snapshot() == {**original, "title": "External rename"}
        assert repo.list_events(meeting_id) == []
        invalidate.assert_called_once_with(resync=True)
        dashboard.refresh_saved_meeting_title(meeting_id)
        invalidate.assert_called_once_with(resync=True)


def test_cached_report_delete_after_mcp_rename_does_not_restore_old_title(title_case):
    repo, agent, meeting_id, other_id = title_case
    with ExitStack() as stack:
        primary = InProcessDashboard(repo, other_id, stack)
        secondary = InProcessDashboard(repo, other_id, stack)
        secondary.delete_report(meeting_id, "r_0")
        agent.meeting_renamer = runtime_for(repo, primary, secondary)
        assert agent.retitle("meeting", meeting_id, "Renamed through MCP")["title"] == "Renamed through MCP"
        secondary.delete_report(meeting_id, "r_1")
        assert_saved_title(repo, meeting_id, "Renamed through MCP")
        assert secondary.detail(meeting_id)["state"]["title"] == "Renamed through MCP"


def test_refreshed_dashboard_socket_resyncs_without_expiring_the_host_token(title_case):
    from starlette.websockets import WebSocketDisconnect
    from meeting.web.ws import WS_CLOSE_RESYNC

    repo, _, meeting_id, _ = title_case
    with ExitStack() as stack:
        dashboard = InProcessDashboard(repo, meeting_id, stack)
        with dashboard.client.websocket_connect("/ws?token=host-token") as socket:
            assert socket.receive_json()["state"]["title"] == "Test meeting"
            repo.rename_meeting(meeting_id, "External rename")
            dashboard.refresh_saved_meeting_title(meeting_id)
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == WS_CLOSE_RESYNC
        with dashboard.client.websocket_connect("/ws?token=host-token") as socket:
            assert socket.receive_json()["state"]["title"] == "External rename"
