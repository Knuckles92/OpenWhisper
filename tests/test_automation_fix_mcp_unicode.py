"""MCP refuses text that isn't valid Unicode, and MCP Events bodies never fail on it."""

import json

import pytest
from fastapi.testclient import TestClient

from services.agent_mcp import events_ext
from services.agent_mcp.app import create_app
from services.automation import catalog, events
from services.automation.control import LiveState, set_app_control
from services.database import DatabaseManager
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings

TOKEN = "mcp-unicode-token-" + "u" * 32
MODERN = "2026-07-28"


class FakeControl:
    def __init__(self):
        self.calls = []

    def state(self):
        return LiveState(False, False, False, "", "", "", "9.9.9")

    def run_action(self, action_id, text, *, source):
        self.calls.append(("run", action_id, text))
        return "job"

    def start_recording(self, *, action_id="", source):
        self.calls.append(("start", action_id))
        return "job"

    def notify(self, text, *, title=""):
        self.calls.append(("notify", text, title))


@pytest.fixture
def control():
    fake = FakeControl()
    set_app_control(fake)
    yield fake
    set_app_control(None)


@pytest.fixture
def client(tmp_path, control):
    path = tmp_path / "history.db"
    DatabaseManager(str(path)).close()
    settings = InMemorySettings({
        SettingsKey.MCP_ENABLED: True,
        SettingsKey.API_RUN_ACTIONS: True,
        SettingsKey.API_CONTROL_RECORDING: True,
    })
    app = create_app(path, TOKEN, enabled=lambda: True, settings=settings)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as test_client:
        yield test_client


def call_raw(client, name, arguments_json):
    """tools/call with arguments written as raw JSON, so they can carry a lone surrogate."""
    body = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"%s","arguments":%s,'
        '"_meta":{"io.modelcontextprotocol/protocolVersion":"%s",'
        '"io.modelcontextprotocol/clientCapabilities":{}}}}' % (name, arguments_json, MODERN)
    )
    response = client.post("/mcp", content=body.encode("ascii"), headers={
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MODERN,
        "Mcp-Method": "tools/call",
        "Mcp-Name": name,
    })
    return response.json()["result"]


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("run_action", '{"action_id": "a1", "text": "fix this \\ud83d"}'),
        ("run_action", '{"action_id": "a\\udc00", "text": "fine"}'),
        ("start_dictation", '{"action_id": "a\\ud800"}'),
        ("notify_user", '{"text": "done \\ud83d"}'),
        ("notify_user", '{"text": "done", "title": "\\ud83d"}'),
    ],
)
def test_tools_refuse_text_that_is_not_valid_unicode(client, control, name, arguments):
    # The SDK's argument validation (pydantic's string_unicode) refuses it
    # before the tool runs.
    result = call_raw(client, name, arguments)
    assert result["isError"] is True
    assert "valid string" in result["content"][0]["text"]
    assert control.calls == []


def test_whole_emoji_still_run(client, control):
    result = call_raw(client, "run_action", '{"action_id": "a1", "text": "ok \\ud83d\\ude00"}')
    assert result["isError"] is False
    assert control.calls == [("run", "a1", "ok \U0001f600")]


def test_an_event_body_with_a_lone_surrogate_is_still_built():
    event = events.Event(
        id="evt_" + "1" * 24, seq=3, type=catalog.CHANNEL_MESSAGE,
        time="2026-10-10T12:00:00.000Z",
        data={"job_id": "j", "action_id": "a", "text": "half \ud83d", "app": None},
    )
    body = events_ext._event_body(event)
    assert body.isascii()
    assert json.loads(body)["data"]["text"] == "half \ud83d"
