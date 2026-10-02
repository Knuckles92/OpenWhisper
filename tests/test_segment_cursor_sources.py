"""Segment pagination across in-process readers and disposable saved copies."""

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from services.agent_api.federation import FederatedHistory
from services.agent_api.store import HistoryStore, InvalidQuery, NotFound
from services.database import DatabaseManager
from services.models import MeetingSegment, MeetingSession
from services.remote_history.channel import ClientUnavailable
from services.remote_history.client import HistoryReader

DATE = "2026-09-01T12:00:00+00:00"
DEVICE = "fixture-device"


class InProcessClient:
    def __init__(self, reader):
        self.reader = reader
        self.online = True

    def clients(self):
        return [{
            "device_id": DEVICE,
            "name": "Fixture laptop",
            "status": "online" if self.online else "unavailable",
        }]

    def query(self, device, operation, params):
        if device != DEVICE or not self.online:
            raise ClientUnavailable()
        reply = self.reader.query(operation, params)
        if "error" in reply:
            raise ClientUnavailable(reply["error"])
        return reply["result"]


@pytest.fixture
def history_copies(tmp_path):
    databases = []
    for source, origin in (("client", None), ("host", DEVICE)):
        path = tmp_path / f"{source}.db"
        db = DatabaseManager(str(path))
        databases.append(db)
        with db.get_session() as session:
            for meeting in ("meeting", "other-meeting"):
                session.add(MeetingSession(
                    id=meeting,
                    title="Fixture meeting",
                    status="ended",
                    started_at=DATE,
                    host_token="fixture",
                    guest_token="fixture",
                    spool_dir="unused",
                    origin_device_id=origin,
                ))
            session.flush()
            for number, start in enumerate((0, 0, 10, 20, 30)):
                session.add(MeetingSegment(
                    id=f"segment-{number}",
                    meeting_id="meeting",
                    start_s=start,
                    end_s=start + 3,
                    channel="mic",
                    text=f"{source} segment {number}",
                    created_at=DATE,
                ))
    reader = HistoryReader(tmp_path / "client.db", lambda: True)
    client = InProcessClient(reader)
    history = FederatedHistory(HistoryStore(tmp_path / "host.db"), client)
    try:
        yield SimpleNamespace(history=history, client=client, host_db=databases[1])
    finally:
        history.close()
        reader.store.close()
        for db in databases:
            db.close()


@pytest.mark.parametrize("initially_online", [False, True])
def test_segment_cursor_survives_switching_sources(history_copies, initially_online):
    history, client = history_copies.history, history_copies.client
    client.online = initially_online
    params = dict(device_id=DEVICE, include_remote=True, limit=1)
    page = history.segments("meeting", **params)
    ids = [item.id for item in page.items]
    for number in range(1, 5):
        assert page.next_cursor is not None
        client.online = not client.online
        page = history.segments("meeting", cursor=page.next_cursor, **params)
        assert page.items[0].text == f"{'client' if client.online else 'host'} segment {number}"
        ids.extend(item.id for item in page.items)
    assert ids == [f"segment-{number}" for number in range(5)]
    assert page.next_cursor is None


@pytest.mark.parametrize("legacy_include_remote", [False, True])
def test_existing_segment_cursor_is_accepted_on_either_source(
    history_copies, legacy_include_remote
):
    scope = ["segments", "meeting", None, None, legacy_include_remote]
    cursor = base64.urlsafe_b64encode(json.dumps({
        "scope": hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:24],
        "keys": [0.0, "segment-0"],
    }).encode()).decode()
    for online in (True, False):
        history_copies.client.online = online
        page = history_copies.history.segments(
            "meeting", device_id=DEVICE, include_remote=True, cursor=cursor, limit=1
        )
        assert [item.id for item in page.items] == ["segment-1"]
        assert page.items[0].text == f"{'client' if online else 'host'} segment 1"
        scope[-1] = False
        canonical = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:24]
        assert json.loads(base64.urlsafe_b64decode(page.next_cursor))["scope"] == canonical


@pytest.mark.parametrize("changed", [
    {"start_s": 1}, {"end_s": 25}, {"meeting_id": "other-meeting"},
])
def test_fallback_segment_cursor_remains_bound_to_meeting_and_range(history_copies, changed):
    history = history_copies.history
    params = dict(meeting_id="meeting", device_id=DEVICE, include_remote=True, limit=1)
    first = history.segments(**params)
    history_copies.client.online = False
    with pytest.raises(InvalidQuery, match="different filters"):
        history.segments(**(params | changed), cursor=first.next_cursor)


def test_offline_segment_fallback_still_requires_opt_in_and_matching_origin(history_copies):
    history = history_copies.history
    first = history.segments("meeting", device_id=DEVICE, include_remote=True, limit=1)
    history_copies.client.online = False
    with pytest.raises(ClientUnavailable):
        history.segments("meeting", device_id=DEVICE, cursor=first.next_cursor)
    with pytest.raises(ClientUnavailable):
        history.segments(
            "meeting", device_id="other-device", include_remote=True, cursor=first.next_cursor
        )
    with pytest.raises(NotFound):
        history.store.segments("meeting", cursor=first.next_cursor)


def test_fallback_segment_cursor_keeps_time_range_and_deleted_boundary(history_copies):
    history = history_copies.history
    params = dict(device_id=DEVICE, include_remote=True, limit=1, start_s=0.0, end_s=30.0)
    first = history.segments("meeting", **params)
    with history_copies.host_db.get_session() as session:
        session.delete(session.get(MeetingSegment, "segment-0"))
    history_copies.client.online = False
    ids = [item.id for item in first.items]
    page = first
    while page.next_cursor:
        page = history.segments("meeting", cursor=page.next_cursor, **params)
        ids.extend(item.id for item in page.items)
    assert ids == [f"segment-{number}" for number in range(4)]
