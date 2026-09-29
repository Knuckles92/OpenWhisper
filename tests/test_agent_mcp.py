"""Exercise MCP over HTTP with the official client and disposable saved history."""

import asyncio
import json
import socket
import time

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from services.agent_mcp.app import create_app
from services.agent_mcp.runtime import CREDENTIAL_NAME, McpRuntime
from services.credentials import CredentialStore, memory_backend
from services.database import DatabaseManager
from services.models import MeetingSegment, MeetingSession, TranscriptionHistory

TOKEN = "mcp-test-token-" + "a" * 32
DATE = "2026-09-01T12:00:00+00:00"


@pytest.fixture
def mcp_database(tmp_path):
    path = tmp_path / "mcp history.db"
    db = DatabaseManager(str(path))
    with db.get_session() as session:
        session.add_all(
            [
                TranscriptionHistory(
                    id="h_local",
                    text="Launch Friday",
                    raw_text="raw launch",
                    timestamp=DATE,
                    model="test",
                    audio_file="private-marker",
                ),
                TranscriptionHistory(
                    id="h_remote",
                    text="Launch remote",
                    timestamp=DATE,
                    model="test",
                    origin_device_id="other-computer",
                ),
                MeetingSession(
                    id="m_a",
                    title="Launch plan",
                    status="ended",
                    started_at=DATE,
                    host_token="private-marker",
                    guest_token="private-marker",
                    spool_dir="private-marker",
                    state_seq=1,
                    state_json=json.dumps(
                        {
                            "rolling_summary": "Launch Friday",
                            "rolling_summary_evidence": ["sg_a"],
                        }
                    ),
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                MeetingSegment(
                    id=f"sg_{letter}",
                    meeting_id="m_a",
                    text=f"Launch {letter}",
                    start_s=number * 5,
                    end_s=number * 5 + 3,
                    channel="mic",
                    created_at=DATE,
                    speaker_participant_id="p_a",
                )
                for number, letter in enumerate("abc")
            ]
        )
    yield path
    db.close()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(server, state):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = server.status()
        if status.state == state:
            return status
        if status.state == "error" and state != "error":
            pytest.fail(status.message)
        time.sleep(0.02)
    pytest.fail(f"Expected {state}, got {server.status()}")


@pytest.fixture
def running_server(mcp_database):
    credentials = CredentialStore(memory_backend)
    server = McpRuntime(mcp_database, credentials=credentials)
    server.start(free_port())
    try:
        wait_for(server, "running")
        yield server, credentials
    finally:
        server.stop(wait=True)


def test_authenticated_boundary_and_disable(mcp_database):
    enabled = True
    app = create_app(mcp_database, TOKEN, enabled=lambda: enabled)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
        headers = {
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json, text/event-stream",
        }
        assert client.post("/mcp", json=payload).status_code == 401
        assert (
            client.post(
                "/mcp",
                json=payload,
                headers={**headers, "Origin": "https://example.com"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/mcp", json=payload, headers={**headers, "Host": "evil.example"}
            ).status_code
            == 403
        )
        response = client.post("/mcp", json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "OpenWhisper"
        assert response.headers["cache-control"] == "no-store"
        enabled = False
        assert client.post("/mcp", json=payload, headers=headers).status_code == 503
        assert client.get("/v1/transcriptions", headers=headers).status_code == 503


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_official_client_discovery_retrieval_and_validation(running_server, mode):
    server, _ = running_server

    async def exercise():
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {server.token()}"}, trust_env=False
        ) as http:
            transport = streamable_http_client(server.status().url, http_client=http)
            async with Client(transport, mode=mode) as client:
                tools = (await client.list_tools()).tools
                assert {tool.name for tool in tools} == {
                    "get_status",
                    "search_history",
                    "list_transcriptions",
                    "get_transcription",
                    "list_meetings",
                    "get_meeting",
                    "list_meeting_segments",
                    "get_meeting_segment",
                    "get_meeting_insights",
                }
                assert all(tool.annotations.read_only_hint for tool in tools)
                status = await client.call_tool("get_status", {})
                assert not status.is_error
                search = await client.call_tool(
                    "search_history", {"q": "launch", "limit": 1}
                )
                assert not search.is_error
                page = search.structured_content
                assert len(page["items"]) == 1
                assert page["next_cursor"]
                next_page = await client.call_tool(
                    "search_history",
                    {
                        "q": "launch",
                        "limit": 1,
                        "cursor": page["next_cursor"],
                    },
                )
                assert next_page.structured_content["items"] != page["items"]
                local = await client.call_tool("list_transcriptions", {})
                assert [item["id"] for item in local.structured_content["items"]] == [
                    "h_local"
                ]
                remote = await client.call_tool(
                    "get_transcription", {"record_id": "h_remote"}
                )
                assert remote.is_error
                remote = await client.call_tool(
                    "get_transcription",
                    {"record_id": "h_remote", "include_remote": True},
                )
                assert not remote.is_error
                for name, args in [
                    ("get_transcription", {"record_id": "h_local"}),
                    ("list_meetings", {}),
                    ("get_meeting", {"meeting_id": "m_a"}),
                    (
                        "list_meeting_segments",
                        {"meeting_id": "m_a", "start_s": 5, "end_s": 10},
                    ),
                    (
                        "get_meeting_segment",
                        {"meeting_id": "m_a", "segment_id": "sg_a"},
                    ),
                    ("get_meeting_insights", {"meeting_id": "m_a"}),
                ]:
                    result = await client.call_tool(name, args)
                    assert not result.is_error, result
                    assert "private-marker" not in result.model_dump_json()
                for name, args in [
                    ("search_history", {"q": " "}),
                    ("search_history", {"q": "launch", "limit": 101}),
                    ("search_history", {"q": "launch", "since": "2026-09-01T00:00:00"}),
                    ("search_history", {"q": "launch", "cursor": "broken"}),
                    (
                        "list_meeting_segments",
                        {"meeting_id": "m_a", "start_s": 10, "end_s": 1},
                    ),
                    ("get_meeting", {"meeting_id": "missing"}),
                    ("get_meeting", {"meeting_id": "../status"}),
                ]:
                    assert (await client.call_tool(name, args)).is_error

    asyncio.run(exercise())


def test_runtime_restart_uses_saved_token_and_releases_port(running_server):
    server, credentials = running_server
    token, port = server.token(), server.status().port
    assert credentials.get(CREDENTIAL_NAME) == token
    server.stop(wait=True)
    assert server.status().state == "stopped"
    assert not server.token()
    server.start(port)
    wait_for(server, "running")
    assert server.token() == token


def test_occupied_port_and_missing_database_are_recoverable(mcp_database):
    credentials = CredentialStore(memory_backend)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        server = McpRuntime(mcp_database, credentials=credentials)
        server.start(occupied.getsockname()[1])
        assert "port" in wait_for(server, "error").message
        assert not server.token()
    server.start(free_port())
    try:
        wait_for(server, "running")
    finally:
        server.stop(wait=True)
    missing = McpRuntime(mcp_database.parent / "missing.db", credentials=credentials)
    missing.start(free_port())
    assert "database" in wait_for(missing, "error").message


def test_failed_credentials_do_not_start_listener(mcp_database):
    def unavailable():
        raise RuntimeError("locked")

    server = McpRuntime(mcp_database, credentials=CredentialStore(unavailable))
    server.start(free_port())
    assert "credential store" in wait_for(server, "error").message
    assert not server.token()
