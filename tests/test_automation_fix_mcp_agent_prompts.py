"""MCP reports the agent-prompts refusal plainly and counts it as a write capability."""

import pytest
from fastapi.testclient import TestClient

from services.agent_mcp.app import create_app
from services.agent_mcp.controls import WRITE_CAPABILITIES
from services.automation import permissions
from services.automation.control import set_app_control
from services.database import DatabaseManager
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings
from tests.test_automation_mcp_tools import FakeAppControl

TOKEN = "mcp-agent-prompts-" + "p" * 32
MODERN = "2026-07-28"


@pytest.fixture
def control():
    fake = FakeAppControl()
    set_app_control(fake)
    yield fake
    set_app_control(None)


@pytest.fixture
def settings():
    return InMemorySettings({
        SettingsKey.MCP_ENABLED: True,
        SettingsKey.API_RUN_ACTIONS: True,
        SettingsKey.API_CONTROL_RECORDING: True,
    })


@pytest.fixture
def client(tmp_path, settings):
    path = tmp_path / "history.db"
    DatabaseManager(str(path)).close()
    app = create_app(path, TOKEN, enabled=lambda: True, settings=settings)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as test_client:
        yield test_client


def call(client, name, arguments=None):
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": name, "arguments": arguments or {},
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
            "Mcp-Name": name,
        },
    )
    return response.json()["result"]


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("run_action", {"action_id": "agent", "text": "push to main"}),
        ("start_dictation", {"action_id": "agent"}),
    ],
)
def test_the_app_refusing_for_agent_prompts_reads_as_permission_denied(
    client, control, name, arguments
):
    control.fail = permissions.PermissionDenied(permissions.AGENT_PROMPTS)
    result = call(client, name, arguments)
    assert result["isError"] is True
    assert result["content"][0]["text"].endswith(
        "permission_denied: Allow sending words to your coding agents in "
        "OpenWhisper Settings > API & MCP."
    )


def test_mcp_events_describe_who_started_a_job():
    from services.agent_mcp import events_ext
    from services.automation import catalog

    for name in (catalog.CHANNEL_MESSAGE, catalog.ACTION_RAN):
        assert "source" in events_ext.PAYLOAD_SCHEMAS[name]["required"]
    ran = events_ext.PAYLOAD_SCHEMAS[catalog.ACTION_RAN]
    assert "text" in ran["properties"] and "text" not in ran["required"]


def test_agent_prompts_is_a_write_capability(client, settings):
    assert permissions.PUBLIC_NAMES[permissions.AGENT_PROMPTS] in WRITE_CAPABILITIES
    for key in (SettingsKey.API_RUN_ACTIONS, SettingsKey.API_CONTROL_RECORDING):
        settings.values[key] = False
    assert call(client, "get_status")["structuredContent"]["read_only"] is True
    settings.values[SettingsKey.API_AGENT_PROMPTS] = True
    status = call(client, "get_status")["structuredContent"]
    assert status["capabilities"]["agent_prompts"] is True
    assert status["read_only"] is False
