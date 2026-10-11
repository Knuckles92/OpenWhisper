"""recent_events treats a position from a previous app session like the event stream does."""

import pytest
from fastapi.testclient import TestClient

from services.agent_mcp.app import create_app
from services.automation import catalog, events
from services.database import DatabaseManager
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings

TOKEN = "mcp-since-token-" + "s" * 32
MODERN = "2026-07-28"


@pytest.fixture
def bus():
    fresh = events.EventBus()
    previous = events.set_bus(fresh)
    yield fresh
    events.set_bus(previous)
    fresh.close()


@pytest.fixture
def client(tmp_path, bus):
    path = tmp_path / "history.db"
    DatabaseManager(str(path)).close()
    settings = InMemorySettings({SettingsKey.MCP_ENABLED: True})
    app = create_app(path, TOKEN, enabled=lambda: True, settings=settings)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as test_client:
        yield test_client


def recent_events(client, **arguments):
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "recent_events", "arguments": arguments,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": MODERN,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        }},
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MODERN,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "recent_events",
        },
    )
    result = response.json()["result"]
    assert result["isError"] is False, result
    return result["structuredContent"]


def test_a_position_from_a_previous_session_reports_a_gap_and_returns_everything(client, bus):
    # A new session numbers past every old position; a bus that has numbered
    # and forgotten six events stands in for it.
    for n in range(6):
        bus.publish(catalog.RECORDING_CANCELED, {"job_id": f"old{n}"})
    bus.clear()
    new_session = [bus.publish(catalog.RECORDING_STARTED, {"job_id": f"new{n}"}) for n in range(2)]

    page = recent_events(client, since_seq=3)
    assert page["gap"] is True
    assert [item["seq"] for item in page["events"]] == [e.seq for e in new_session]
    assert page["last_seq"] == new_session[-1].seq


def test_large_positions_are_accepted(client, bus):
    published = bus.publish(catalog.RECORDING_STARTED, {"job_id": "a"})
    page = recent_events(client, since_seq=10 ** 18 - 1)
    assert isinstance(page["gap"], bool)
    assert page["last_seq"] == published.seq
