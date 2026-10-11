"""A program needs "send words to your coding agents" to start an agent or channel action.

Running such an action hands the words to the user's coding agent, which acts
on them, so the live API and MCP need api_agent_prompts on top of running
actions or starting recordings. The runtime refuses run_action_text itself;
AppControl refuses a recording started for such an action; the API answers
either refusal with 403.
"""

import pytest
from fastapi.testclient import TestClient

from services.actions.model import Action
from services.agent_mcp.app import create_app
from services.automation import app_control, permissions
from services.automation.control import app_control as registered
from services.automation.control import set_app_control
from services.database import DatabaseManager
from services.settings import settings_manager
from tests.test_automation_api_app_control import FakeController, in_worker, save_actions
from tests.test_automation_api_routes import FakeControl, FakeSettings

TOKEN = "agent-prompts-token-" + "p" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
AGENT_PROMPTS_MESSAGE = (
    "Allow sending words to your coding agents in OpenWhisper Settings > API & MCP."
)


@pytest.fixture
def controller(_session_qt_application):
    fake = FakeController()
    app_control.install(fake)
    yield fake
    set_app_control(None)


def actions():
    return (
        Action("agent", "Agent hotkey", "agent", {"agent": "claude_code", "mode": "handoff"}),
        Action("chan", "Tell my session", "channel"),
        Action("link", "Ask Claude", "open_link", {"template": "https://x/?q={text}"}),
    )


@pytest.mark.parametrize("action_id", ["agent", "chan"])
@pytest.mark.parametrize("source", ["api", "mcp"])
def test_recording_for_an_agent_or_channel_action_needs_agent_prompts(
    controller, action_id, source
):
    save_actions(*actions())
    result = in_worker(lambda: registered().start_recording(action_id=action_id, source=source))
    error = result["error"]
    assert isinstance(error, permissions.PermissionDenied)
    assert error.permission == permissions.AGENT_PROMPTS
    assert error.message == AGENT_PROMPTS_MESSAGE
    assert controller.calls == []


def test_with_agent_prompts_allowed_the_recording_starts(controller):
    save_actions(*actions())
    settings_manager.update_settings({permissions.AGENT_PROMPTS: True})
    result = in_worker(lambda: registered().start_recording(action_id="chan", source="api"))
    assert result == {"value": "job-action"}
    assert controller.calls == [("start_action", "chan", "api")]


def test_other_actions_and_plain_dictation_need_no_agent_prompts(controller):
    save_actions(*actions())
    assert in_worker(
        lambda: registered().start_recording(action_id="link", source="mcp")
    ) == {"value": "job-action"}
    controller.recorder.is_recording = False
    assert in_worker(lambda: registered().start_recording(source="api")) == {"value": "job-new"}


@pytest.fixture
def control():
    fake = FakeControl()
    set_app_control(fake)
    yield fake
    set_app_control(None)


@pytest.fixture
def client(tmp_path, control):
    path = tmp_path / "live.db"
    DatabaseManager(str(path)).close()
    settings = FakeSettings(**{
        permissions.RUN_ACTIONS: True, permissions.CONTROL_RECORDING: True,
    })
    app = create_app(path, TOKEN, enabled=lambda: True, settings=settings)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "path, body",
    [("/v1/actions/agent/run", {"text": "delete the tests"}), ("/v1/recording/start", {"action_id": "agent"})],
)
def test_the_app_refusing_for_agent_prompts_is_a_403(client, control, path, body):
    control.error = permissions.PermissionDenied(permissions.AGENT_PROMPTS)
    response = client.post(path, json=body, headers=AUTH)
    assert response.status_code == 403
    assert response.json()["error"] == {
        "code": "permission_denied",
        "message": AGENT_PROMPTS_MESSAGE,
    }
