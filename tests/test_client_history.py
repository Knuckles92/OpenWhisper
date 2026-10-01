"""Direct history retrieval through real authenticated TLS and MCP connections."""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from services.agent_api.app import create_app
from services.agent_api.federation import FederatedHistory
from services.agent_api.store import HistoryStore, InvalidQuery
from services.agent_mcp.runtime import McpRuntime
from services.credentials import CredentialStore, memory_backend
from services.database import DatabaseManager
from services.models import MeetingSegment, MeetingSession, TranscriptionHistory
from services.remote_asr import tailscale
from services.remote_asr.client import (
    RemoteConnection,
    RemoteEngineError,
    pair_with_host,
)
from services.remote_asr.host import DeviceRegistry, SpeechHost
from services.remote_asr.settings import ClientPairing, client_shares_history
from services.remote_asr.tls import ensure_host_identity
from services.remote_history.channel import ClientUnavailable
from services.remote_history.client import HistoryClient, HistoryReader
from services.settings import SettingsManager
from tests.test_agent_mcp import free_port, wait_for
from tests.test_remote_engine import FakeEngine, ListStore

TOKEN = "client-history-test-" + "a" * 32
DATE = "2026-09-01T12:00:00+00:00"


def wait_until(check):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    assert check()


@pytest.fixture
def live_history(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tailscale,
        "status",
        lambda timeout=4: tailscale.TailscaleStatus("not_installed"),
    )
    devices = ListStore()
    host = SpeechHost(
        engine_provider=FakeEngine,
        registry=DeviceRegistry(devices.load, devices.save),
        identity=ensure_host_identity(str(tmp_path / "identity")),
    )
    host.start(port=0, bind="127.0.0.1")
    pairing = pair_with_host("127.0.0.1", host.port, host.open_pairing(), "laptop")
    client_path = tmp_path / "client.db"
    client_db = DatabaseManager(str(client_path))
    host_path = tmp_path / "host.db"
    host_db = DatabaseManager(str(host_path))
    with client_db.get_session() as session:
        for number in range(4):
            session.add(
                TranscriptionHistory(
                    id=f"h{number}",
                    text=f"Launch {number}",
                    timestamp=DATE,
                    model="test",
                    audio_file="private-marker",
                )
            )
        session.add(
            TranscriptionHistory(
                id="other_origin",
                text="Launch from elsewhere",
                timestamp=DATE,
                model="test",
                origin_device_id="third-device",
            )
        )
        session.add(
            MeetingSession(
                id="meeting",
                title="Launch meeting",
                started_at=DATE,
                status="ended",
                host_token="private-marker",
                guest_token="private-marker",
                spool_dir="private-marker",
                state_seq=1,
                state_json=json.dumps(
                    {
                        "rolling_summary": "Launch on Friday",
                        "rolling_summary_evidence": ["segment"],
                    }
                ),
            )
        )
        session.flush()
        session.add(
            MeetingSegment(
                id="segment",
                meeting_id="meeting",
                text="Launch Friday",
                start_s=0,
                end_s=3,
                channel="mic",
                created_at=DATE,
            )
        )
    enabled = [True]
    client = HistoryClient(
        client_path,
        pairing=lambda: ClientPairing(
            "127.0.0.1", host.port, pairing.fingerprint, "host"
        ),
        token=lambda: pairing.token,
        enabled=lambda: enabled[0],
    )
    client.start()
    wait_until(lambda: host.history.clients()[0]["status"] == "online")
    value = SimpleNamespace(
        host=host,
        client=client,
        enabled=enabled,
        device=pairing.device_id,
        pairing=pairing,
        host_db=host_db,
        host_path=host_path,
        client_path=client_path,
    )
    yield value
    client.stop()
    host.stop()
    client_db.close()
    host_db.close()


def test_direct_queries_and_mcp_citations_without_uploads(live_history, tmp_path):
    live = live_history
    server = McpRuntime(
        live.host_path,
        credentials=CredentialStore(memory_backend),
        settings=SettingsManager(str(tmp_path / "settings.json")),
    )
    server.configure_controls(client_history=live.host.history)
    server.start(free_port())
    wait_for(server, "running")

    async def exercise():
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {server.token()}"}, trust_env=False
        ) as http:
            async with Client(
                streamable_http_client(server.status().url, http_client=http)
            ) as client:
                local = await client.call_tool("list_transcriptions", {})
                assert local.structured_content["items"] == []
                page = await client.call_tool(
                    "list_transcriptions", {"include_clients": True}
                )
                assert [item["id"] for item in page.structured_content["items"]] == [
                    "h0",
                    "h1",
                    "h2",
                    "h3",
                ]
                assert page.structured_content["clients"][0]["status"] == "online"
                record = await client.call_tool(
                    "get_transcription", {"record_id": "h0", "device_id": live.device}
                )
                assert record.structured_content["text"] == "Launch 0"
                assert record.structured_content["origin_device_id"] == live.device
                hits = await client.call_tool(
                    "search_history", {"q": "Friday", "include_clients": True}
                )
                hit = hits.structured_content["items"][0]
                assert hit["kind"] == "segment" and hit["device_id"] == live.device
                assert f"device_id={live.device}" in hit["resource"]
                for tool, params in [
                    ("list_meetings", {"include_clients": True}),
                    (
                        "get_meeting",
                        {"meeting_id": "meeting", "device_id": live.device},
                    ),
                    (
                        "list_meeting_segments",
                        {"meeting_id": "meeting", "device_id": live.device},
                    ),
                    (
                        "get_meeting_segment",
                        {
                            "meeting_id": "meeting",
                            "segment_id": "segment",
                            "device_id": live.device,
                        },
                    ),
                    (
                        "get_meeting_insights",
                        {"meeting_id": "meeting", "device_id": live.device},
                    ),
                ]:
                    result = await client.call_tool(tool, params)
                    assert not result.is_error, result
                    assert "private-marker" not in result.model_dump_json()
                status = await client.call_tool("get_status", {})
                assert (
                    status.structured_content["clients"][0]["device_id"] == live.device
                )

    try:
        asyncio.run(exercise())
        assert live.host_db.get_history_entries() == []
        speech = RemoteConnection(
            "127.0.0.1", live.host.port, live.pairing.token, live.pairing.fingerprint
        )
        try:
            speech.connect()
            assert (
                speech.request("transcribe", audio=np.zeros(1600, dtype=np.float32))[
                    "text"
                ]
                == "heard 1600"
            )
        finally:
            speech.close()
    finally:
        server.stop(wait=True)


def test_pagination_duplicates_filters_revocation_and_offline_copies(live_history):
    live = live_history
    with live.host_db.get_session() as session:
        session.add(
            TranscriptionHistory(
                id="h0",
                text="Stale host copy",
                timestamp=DATE,
                model="test",
                origin_device_id=live.device,
            )
        )
    history = FederatedHistory(HistoryStore(live.host_path), live.host.history)
    try:
        params = dict(include_clients=True, include_remote=True, limit=1)
        page = history.transcriptions(**params)
        assert page.items[0].id == "h0"
        assert page.items[0].preview == "Launch 0"
        assert (
            history.transcription("h0", device_id=live.device, include_remote=True).text
            == "Launch 0"
        )
        first_cursor = page.next_cursor
        found = [page.items[0].id]
        while page.next_cursor:
            page = history.transcriptions(**params, cursor=page.next_cursor)
            found.extend(item.id for item in page.items)
        assert found == ["h0", "h1", "h2", "h3"]
        with pytest.raises(InvalidQuery):
            history.transcriptions(**params, cursor=first_cursor, q="changed")
        filtered = history.transcriptions(
            include_clients=True, q="Launch 2", device_id=live.device
        )
        assert [item.id for item in filtered.items] == ["h2"]
        live.enabled[0] = False
        # Consent is checked on the client even before its UI reconnects the channel.
        revoked = history.transcriptions(**params, cursor=first_cursor)
        assert not revoked.items
        assert revoked.clients[0].status == "sharing_disabled"
        with pytest.raises(ClientUnavailable, match="disabled"):
            history.transcription("h1", device_id=live.device)
        live.client.refresh()
        wait_until(
            lambda: live.host.history.clients()[0]["status"] == "sharing_disabled"
        )
        live.client.stop()
        wait_until(lambda: live.host.history.clients()[0]["status"] == "unavailable")
        assert (
            history.transcription("h0", device_id=live.device, include_remote=True).text
            == "Stale host copy"
        )
        offline = history.transcriptions(**params)
        assert [item.id for item in offline.items] == ["h0"]
        assert offline.clients[0].status == "unavailable"
        targeted = history.transcriptions(**params, device_id=live.device)
        assert [item.id for item in targeted.items] == ["h0"]
    finally:
        history.close()


def test_client_reader_is_allowlisted_and_permission_defaults_off(live_history):
    assert client_shares_history({}) is False
    assert ClientUnavailable("private-path-from-peer").code == "unavailable"
    reader = HistoryReader(live_history.client_path, lambda: True)
    try:
        for operation, params in [
            ("delete", {}),
            ("transcriptions", {"include_remote": True}),
            ("search", {"q": "Launch", "limit": 101}),
            ("meetings", {"since": "2026-09-01"}),
            ("transcription", {}),
            ("transcription", {"record_id": "../x"}),
            ("segments", {"meeting_id": "meeting", "start_s": 5, "end_s": 1}),
        ]:
            assert reader.query(operation, params) == {"error": "invalid_query"}
        assert reader.query("transcription", {"record_id": "missing"}) == {
            "error": "not_found"
        }
        assert (
            reader.query("transcriptions", {"limit": 1})["result"]["items"][0]["id"]
            == "h0"
        )
    finally:
        reader.store.close()


def test_api_unavailable_client_and_pairing_revocation(live_history):
    live = live_history
    with TestClient(
        create_app(live.host_path, TOKEN, client_history=live.host.history),
        base_url="http://127.0.0.1",
    ) as api:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        denied = api.get(
            "/v1/transcriptions/h0", params={"device_id": "unknown"}, headers=headers
        )
        assert denied.status_code == 503
        live.host.remove_device(live.device)
        assert live.host.history.clients() == []
        removed = api.get(
            "/v1/transcriptions/h0", params={"device_id": live.device}, headers=headers
        )
        assert removed.status_code == 503


def test_multiple_clients_same_ids_and_cursor_device_set(live_history):
    live = live_history
    device, token = live.host.registry.add("second laptop")
    second = HistoryClient(
        live.client_path,
        pairing=lambda: ClientPairing(
            "127.0.0.1", live.host.port, live.pairing.fingerprint, "host"
        ),
        token=lambda: token,
        enabled=lambda: True,
    )
    second.start()
    history = FederatedHistory(HistoryStore(live.host_path), live.host.history)
    try:
        wait_until(
            lambda: all(c["status"] == "online" for c in live.host.history.clients())
        )
        page = history.transcriptions(include_clients=True, limit=1)
        found = [(i.origin_device_id, i.id) for i in page.items]
        while page.next_cursor:
            page = history.transcriptions(
                include_clients=True, limit=1, cursor=page.next_cursor
            )
            found.extend((i.origin_device_id, i.id) for i in page.items)
        assert set(found) == {
            (d, f"h{n}") for d in (live.device, device["id"]) for n in range(4)
        }
        assert len(found) == 8
        record = history.transcription("h0", device_id=device["id"])
        assert record.origin_device_id == device["id"]
    finally:
        second.stop()
        history.close()


def test_query_timeout_keeps_local_results_and_host_copies(live_history, monkeypatch):
    from services.remote_history import channel

    monkeypatch.setattr(channel, "QUERY_TIMEOUT", 0.02)
    original = HistoryReader.query

    def slow_query(reader, operation, params):
        if operation == "transcriptions":
            time.sleep(0.15)
        return original(reader, operation, params)

    monkeypatch.setattr(HistoryReader, "query", slow_query)
    live = live_history
    with live.host_db.get_session() as session:
        session.add(
            TranscriptionHistory(
                id="host", text="Host history", timestamp=DATE, model="test"
            )
        )
    history = FederatedHistory(HistoryStore(live.host_path), live.host.history)
    try:
        page = history.transcriptions(include_clients=True)
        assert [i.id for i in page.items] == ["host"]
        assert page.clients[0].status == "unavailable"
    finally:
        history.close()


def test_history_channel_requires_authentication(live_history):
    live = live_history
    connection = RemoteConnection(
        "127.0.0.1",
        live.host.port,
        "invalid-token",
        live.pairing.fingerprint,
        history_enabled=True,
    )
    try:
        with pytest.raises(RemoteEngineError, match="paired"):
            connection.connect()
    finally:
        connection.close()


def test_offline_meeting_copies_and_expired_cursor(live_history):
    live = live_history
    with live.host_db.get_session() as session:
        session.add(
            MeetingSession(
                id="meeting",
                title="Launch meeting",
                started_at=DATE,
                status="ended",
                host_token="private",
                guest_token="private",
                spool_dir="private",
                origin_device_id=live.device,
                state_seq=1,
                state_json=json.dumps({"rolling_summary": "Stored summary"}),
            )
        )
        session.flush()
        session.add(
            MeetingSegment(
                id="segment",
                meeting_id="meeting",
                text="Stored speech",
                start_s=0,
                end_s=3,
                channel="mic",
                created_at=DATE,
            )
        )
    history = FederatedHistory(HistoryStore(live.host_path), live.host.history)
    try:
        page = history.transcriptions(include_clients=True, limit=1)
        key = page.next_cursor
        _, scope, state = history._cursors[key]
        history._cursors[key] = (0, scope, state)
        with pytest.raises(InvalidQuery, match="expired"):
            history.transcriptions(include_clients=True, limit=1, cursor=key)
        live.client.stop()
        wait_until(lambda: live.host.history.clients()[0]["status"] == "unavailable")
        params = dict(device_id=live.device, include_remote=True)
        assert history.meeting("meeting", **params).title == "Launch meeting"
        assert history.segments("meeting", **params).items[0].text == "Stored speech"
        assert history.segment("meeting", "segment", **params).text == "Stored speech"
        assert history.insights("meeting", **params).rolling_summary == "Stored summary"
    finally:
        history.close()


def test_standalone_api_reports_direct_queries_unsupported(live_history):
    with TestClient(
        create_app(live_history.host_path, TOKEN), base_url="http://127.0.0.1"
    ) as api:
        response = api.get(
            "/v1/transcriptions",
            params={"include_clients": True, "include_remote": True},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "unsupported"
