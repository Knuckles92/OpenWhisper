"""Tests for opening persisted meetings in the archive web dashboard."""
import json
from unittest.mock import patch

from meeting.state.schema import MeetingState
from meeting.web.archive import ArchivedMeetingDashboard


class FakeSignal:
    def __init__(self):
        self.values = []

    def emit(self, value):
        self.values.append(value)


class FakeRepository:
    def __init__(self):
        self.tokens = None

    def segment_exists(self, meeting_id, segment_id):
        return False

    def get_segment(self, meeting_id, segment_id):
        return None

    def get_segments(self, meeting_id, after_start_s=-1.0, limit=None):
        return [{"id": "sg_1", "meeting_id": meeting_id, "start_s": 1.0}]

    def replace_tokens(self, meeting_id, host_token, guest_token):
        self.tokens = (meeting_id, host_token, guest_token)


class FakeServer:
    host_url = "http://127.0.0.1:9000/m/new-host"
    guest_url = "http://127.0.0.1:9000/m/new-guest"

    def __init__(self):
        self.invalidated = False
        self.stopped = False

    def invalidate_connections(self):
        self.invalidated = True

    def stop(self):
        self.stopped = True


def _meeting():
    state = MeetingState(
        meeting_id="m_archive",
        title="Old snapshot title",
        status="active",
    )
    return {
        "id": "m_archive",
        "title": "Quarterly review",
        "status": "ended",
        "state_json": json.dumps(state.to_dict()),
        "state_seq": 4,
        "cloud_enabled": False,
        "agent_provider": "openrouter",
        "agent_model": "test-model",
    }


def test_archive_adapter_normalizes_persisted_lifecycle_and_transcript():
    repository = FakeRepository()
    archive = ArchivedMeetingDashboard(
        repository,
        _meeting(),
        spool_root="meetings",
    )

    snapshot = archive.store.snapshot()
    assert snapshot["meeting_id"] == "m_archive"
    assert snapshot["title"] == "Quarterly review"
    assert snapshot["status"] == "ended"
    assert archive.is_active() is False
    assert archive.get_transcript() == [
        {"id": "sg_1", "meeting_id": "m_archive", "start_s": 1.0}
    ]


def test_archive_adapter_rotates_links_and_stops_server():
    repository = FakeRepository()
    archive = ArchivedMeetingDashboard(repository, _meeting(), spool_root="meetings")
    server = FakeServer()
    archive.attach_server(server)

    urls = archive.regenerate_tokens()

    assert repository.tokens is not None
    assert repository.tokens[0] == "m_archive"
    assert server.invalidated is True
    assert urls == {"host_url": server.host_url, "guest_url": server.guest_url}

    archive.shutdown()
    assert server.stopped is True


def test_archive_adapter_serves_an_authenticated_session(tmp_path):
    from fastapi.testclient import TestClient

    from meeting.persist.repository import SqlMeetingRepository
    from meeting.web.server import MeetingWebServer
    from services.database import DatabaseManager

    database = DatabaseManager(db_path=str(tmp_path / "archive.db"))
    repository = SqlMeetingRepository(db=database)
    meeting = _meeting()
    repository.create_meeting(
        id=meeting["id"],
        title=meeting["title"],
        status=meeting["status"],
        started_at="2025-01-02T09:30:00",
        ended_at="2025-01-02T10:00:00",
        host_token="archive-host",
        guest_token="archive-guest",
        cloud_enabled=False,
        spool_dir=str(tmp_path / "spool"),
        state_json=meeting["state_json"],
        state_seq=meeting["state_seq"],
    )
    archive = ArchivedMeetingDashboard(
        repository,
        repository.get_meeting("m_archive"),
        spool_root=str(tmp_path),
    )
    server = MeetingWebServer(archive, repository)
    archive.attach_server(server)
    try:
        with TestClient(server.app) as client:
            response = client.get(
                "/api/session", params={"token": "archive-host"}
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["role"] == "host"
        assert payload["state"]["meeting_id"] == "m_archive"
        assert payload["state"]["status"] == "ended"
    finally:
        archive.shutdown()
        database.close()


def test_runtime_reuses_dashboard_and_opens_selected_history():
    from services.runtime.meeting import MeetingRuntime

    controller = type("Controller", (), {
        "meeting_status_update": FakeSignal(),
        "meeting_error": FakeSignal(),
    })()
    runtime = MeetingRuntime(controller)
    runtime._repo = type("Repository", (), {
        "get_meeting": lambda self, meeting_id: {"id": meeting_id},
    })()
    runtime._host_url = "http://127.0.0.1:9000/m/host-token"

    with patch("services.runtime.meeting.webbrowser.open") as browser_open:
        runtime._open_past_meeting_worker("m_selected")

    browser_open.assert_called_once_with(
        "http://127.0.0.1:9000/m/host-token?history=m_selected"
    )
    assert controller.meeting_error.values == []
    assert controller.meeting_status_update.values == ["Opening past meeting"]


def test_history_target_preserves_existing_query_and_capability_path():
    from services.runtime.meeting import _with_history_target

    url = _with_history_target(
        "http://127.0.0.1:9000/m/secret-token?source=qt",
        "m_review/one",
    )

    assert url == (
        "http://127.0.0.1:9000/m/secret-token"
        "?source=qt&history=m_review%2Fone"
    )
