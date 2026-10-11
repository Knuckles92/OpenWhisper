"""Text that isn't valid Unicode never reaches the app, and never breaks a stream.

A JSON body may carry a lone UTF-16 surrogate ("\\ud83d", half an emoji): it
parses, but it can't be encoded as UTF-8, so one such event used to fail
every event stream that tried to send it.
"""

import json

import pytest
from fastapi.testclient import TestClient

from services.agent_mcp.app import create_app
from services.automation import api, catalog, events, permissions
from services.automation.control import LiveState, set_app_control
from services.database import DatabaseManager

TOKEN = "live-api-unicode-token-" + "u" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
HALF_EMOJI = '"fix this \\ud83d"'


class FakeSettings:
    def __init__(self, **values):
        self.values = dict(values)

    def load_all_settings(self):
        return dict(self.values)


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
    path = tmp_path / "live.db"
    DatabaseManager(str(path)).close()
    settings = FakeSettings(**{
        permissions.RUN_ACTIONS: True,
        permissions.CONTROL_RECORDING: True,
        permissions.TEXT_FILTERS: True,
    })
    app = create_app(path, TOKEN, enabled=lambda: True, settings=settings)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "path, body",
    [
        ("/v1/actions/a1/run", '{"text": %s}' % HALF_EMOJI),
        ("/v1/notify", '{"text": %s}' % HALF_EMOJI),
        ("/v1/notify", '{"text": "ok", "title": %s}' % HALF_EMOJI),
        ("/v1/recording/start", '{"action_id": %s}' % HALF_EMOJI),
        ("/v1/filters", '{"url": "http://127.0.0.1:9/\\udc00", "stage": "after_cleanup"}'),
    ],
    ids=["run-text", "notice-text", "notice-title", "start-action-id", "filter-url"],
)
def test_text_that_is_not_valid_unicode_is_refused(client, control, path, body):
    response = client.post(path, content=body.encode("ascii"), headers=AUTH)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_parameters"
    assert "valid Unicode text" in error["message"]
    assert control.calls == []


def test_whole_emoji_and_escaped_text_still_pass(client, control):
    response = client.post(
        "/v1/actions/a1/run", content='{"text": "fix this \\ud83d\\ude00 caf\\u00e9"}'.encode(),
        headers=AUTH,
    )
    assert response.status_code == 202
    assert control.calls == [("run", "a1", "fix this \U0001f600 café")]


def _event(text):
    return events.Event(
        id="evt_" + "0" * 24, seq=7, type=catalog.TRANSCRIPTION_COMPLETED,
        time="2026-10-10T12:00:00.000Z", data={"job_id": "j", "text": text},
    )


def test_a_frame_with_a_lone_surrogate_is_still_sent():
    """Whatever slipped into an event, framing it never raises."""
    event = _event("half \ud83d an emoji,   a line separator, café")
    frame = api.sse_frame(event)
    assert frame.isascii()
    lines = frame.decode("ascii").split("\n")
    assert lines[:2] == ["id: 7", "event: transcription.completed"]
    assert json.loads(lines[2][len("data: "):]) == event.to_dict()
    assert lines[3:] == ["", ""]


def test_frames_are_ascii_so_line_readers_never_split_them():
    text = "one two three\u0085four\nfive\r café \U0001f600"
    lines = api.sse_frame(_event(text)).decode("ascii").splitlines()
    # id, event, data and the blank line that ends the message.
    assert len(lines) == 4 and lines[3] == ""
    assert json.loads(lines[2][len("data: "):])["data"]["text"] == text
