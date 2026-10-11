"""The live API's request/response routes on the real in-app listener app."""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from services.agent_mcp.app import create_app
from services.automation import api, events, filters, permissions
from services.automation.control import ControlError, LiveState, set_app_control
from services.database import DatabaseManager

TOKEN = "live-api-test-token-" + "b" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeSettings:
    """The SettingsManager surface the router reads: every request rereads it."""

    def __init__(self, **values):
        self.values = dict(values)

    def load_all_settings(self):
        return dict(self.values)


class FakeControl:
    def __init__(self):
        self.calls = []
        self.error = None
        self.state_value = LiveState(False, False, False, "", "", "", "9.9.9")

    def _maybe_fail(self):
        if self.error is not None:
            raise self.error

    def state(self):
        self._maybe_fail()
        return self.state_value

    def list_actions(self):
        self._maybe_fail()
        return [{"id": "a1", "name": "Ask Claude", "kind": "open_link", "enabled": True}]

    def run_action(self, action_id, text, *, source):
        self.calls.append(("run", action_id, text, source))
        self._maybe_fail()
        return "job-run"

    def start_recording(self, *, action_id="", source):
        self.calls.append(("start", action_id, source))
        self._maybe_fail()
        return "job-rec"

    def stop_recording(self):
        self.calls.append(("stop",))
        self._maybe_fail()

    def cancel(self):
        self.calls.append(("cancel",))
        self._maybe_fail()

    def notify(self, text, *, title=""):
        self.calls.append(("notify", text, title))
        self._maybe_fail()


@pytest.fixture
def bus():
    fresh = events.EventBus()
    previous = events.set_bus(fresh)
    yield fresh
    events.set_bus(previous)
    fresh.close()


@pytest.fixture
def control():
    fake = FakeControl()
    set_app_control(fake)
    yield fake
    set_app_control(None)


@pytest.fixture
def settings():
    return FakeSettings()


@pytest.fixture
def client(tmp_path, settings, control, bus):
    path = tmp_path / "live.db"
    DatabaseManager(str(path)).close()
    app = create_app(path, TOKEN, enabled=lambda: True, settings=settings)
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def grant(settings, *names):
    for name in names:
        settings.values[name] = True


def error(response):
    return response.json()["error"]


def test_every_route_needs_the_token(client):
    for method, path in (
        ("GET", "/v1/live/state"),
        ("GET", "/v1/events"),
        ("GET", "/v1/actions"),
        ("POST", "/v1/actions/a1/run"),
        ("POST", "/v1/recording/start"),
        ("POST", "/v1/recording/stop"),
        ("POST", "/v1/recording/cancel"),
        ("POST", "/v1/filters"),
        ("POST", "/v1/filters/l1/renew"),
        ("DELETE", "/v1/filters/l1"),
        ("POST", "/v1/notify"),
    ):
        response = client.request(method, path)
        assert response.status_code == 401, path
        assert error(response)["code"] == "unauthorized"


def test_state_has_control_state_permissions_and_listeners(client, control, bus):
    control.state_value = LiveState(True, False, False, "action", "a1", "job-1", "9.9.9")
    bus.subscribe(lambda event: None, label=api.SSE_LABEL)
    bus.subscribe(lambda event: None, label=api.CHANNEL_LABEL)
    bus.subscribe(lambda event: None, label=api.CHANNEL_LABEL)
    bus.subscribe(lambda event: None, label="webhooks")

    body = client.get("/v1/live/state", headers=AUTH).json()

    assert body == {
        "recording": True,
        "busy": False,
        "meeting": False,
        "mode": "action",
        "action_id": "a1",
        "job_id": "job-1",
        "version": "9.9.9",
        "permissions": {
            "live_events": True,
            "run_actions": False,
            "control_recording": False,
            "text_filters": False,
            "agent_prompts": False,
        },
        "listeners": {"events": 1, "claude_channel": 2},
    }


def test_without_the_app_behind_it_state_and_actions_are_unavailable(client):
    set_app_control(None)
    for path in ("/v1/live/state", "/v1/actions"):
        response = client.get(path, headers=AUTH)
        assert response.status_code == 503
        assert error(response) == {
            "code": "unavailable",
            "message": "OpenWhisper isn't ready for this yet.",
        }


def test_actions_are_listed_with_the_token_alone(client):
    response = client.get("/v1/actions", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "actions": [{"id": "a1", "name": "Ask Claude", "kind": "open_link", "enabled": True}]
    }


@pytest.mark.parametrize(
    "method, path, body, permission",
    [
        ("POST", "/v1/actions/a1/run", {"text": "hello"}, permissions.RUN_ACTIONS),
        ("POST", "/v1/notify", {"text": "hello"}, permissions.RUN_ACTIONS),
        ("POST", "/v1/recording/start", {}, permissions.CONTROL_RECORDING),
        ("POST", "/v1/recording/stop", None, permissions.CONTROL_RECORDING),
        ("POST", "/v1/recording/cancel", None, permissions.CONTROL_RECORDING),
        ("POST", "/v1/filters", {"url": "http://127.0.0.1:9/f", "stage": "after_cleanup"},
         permissions.TEXT_FILTERS),
        ("POST", "/v1/filters/l1/renew", None, permissions.TEXT_FILTERS),
        ("DELETE", "/v1/filters/l1", None, permissions.TEXT_FILTERS),
    ],
)
def test_powerful_routes_are_off_by_default(client, control, method, path, body, permission):
    response = client.request(method, path, json=body, headers=AUTH)
    assert response.status_code == 403
    assert error(response) == {
        "code": "permission_denied",
        "message": permissions.PermissionDenied(permission).message,
    }
    assert control.calls == []


def test_permissions_are_reread_on_every_request(client, settings, control):
    grant(settings, permissions.RUN_ACTIONS)
    assert client.post("/v1/actions/a1/run", json={"text": "hi"}, headers=AUTH).status_code == 202
    settings.values[permissions.RUN_ACTIONS] = False
    assert client.post("/v1/actions/a1/run", json={"text": "hi"}, headers=AUTH).status_code == 403
    # A malformed saved value means the default, which is off.
    settings.values[permissions.RUN_ACTIONS] = "yes"
    assert client.post("/v1/actions/a1/run", json={"text": "hi"}, headers=AUTH).status_code == 403
    assert len(control.calls) == 1


def test_run_action_returns_the_job(client, settings, control):
    grant(settings, permissions.RUN_ACTIONS)
    response = client.post("/v1/actions/a1/run", json={"text": "make it so"}, headers=AUTH)
    assert response.status_code == 202
    assert response.json() == {"job_id": "job-run"}
    assert control.calls == [("run", "a1", "make it so", "api")]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"text": ""},
        {"text": 5},
        {"text": ["hi"]},
        {"text": "x" * (api.MAX_ACTION_TEXT + 1)},
    ],
)
def test_run_action_text_must_be_1_to_20000_characters(client, settings, control, body):
    grant(settings, permissions.RUN_ACTIONS)
    response = client.post("/v1/actions/a1/run", json=body, headers=AUTH)
    assert response.status_code == 422
    assert error(response) == {
        "code": "invalid_parameters",
        "message": "text must be text of 1 to 20,000 characters.",
    }
    assert control.calls == []


def test_the_longest_text_is_accepted_even_fully_escaped(client, settings, control):
    grant(settings, permissions.RUN_ACTIONS)
    text = "é" * api.MAX_ACTION_TEXT
    response = client.post(
        "/v1/actions/a1/run",
        content=('{"text": "' + "\\u00e9" * api.MAX_ACTION_TEXT + '"}').encode(),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert response.status_code == 202
    assert control.calls == [("run", "a1", text, "api")]


@pytest.mark.parametrize(
    "content, status, code",
    [
        (b"not json", 422, "invalid_parameters"),
        (b"[1, 2]", 422, "invalid_parameters"),
        (b'"text"', 422, "invalid_parameters"),
        (b"\xff\xfe", 422, "invalid_parameters"),
        (b"{" + b" " * (api.MAX_RUN_BODY + 1) + b"}", 413, "request_too_large"),
    ],
    ids=["not-json", "array", "string", "not-utf8", "too-large"],
)
def test_bodies_must_be_small_json_objects(client, settings, control, content, status, code):
    grant(settings, permissions.RUN_ACTIONS)
    response = client.post(
        "/v1/actions/a1/run", content=content,
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert response.status_code == status
    assert error(response)["code"] == code
    assert control.calls == []


def test_a_body_without_a_length_is_still_limited(client, settings, control):
    grant(settings, permissions.RUN_ACTIONS)

    def chunks():
        for _ in range(40):
            yield b" " * 8192

    response = client.post(
        "/v1/actions/a1/run", content=chunks(),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert control.calls == []


def test_action_ids_are_bounded(client, settings, control):
    grant(settings, permissions.RUN_ACTIONS)
    response = client.post(f"/v1/actions/{'a' * 65}/run", json={"text": "hi"}, headers=AUTH)
    assert response.status_code == 422
    assert error(response)["code"] == "invalid_parameters"
    assert control.calls == []


@pytest.mark.parametrize(
    "code, status",
    [("busy", 409), ("not_found", 404), ("refused", 422), ("unavailable", 503)],
)
def test_control_errors_keep_their_code_and_message(client, settings, control, code, status):
    grant(settings, permissions.RUN_ACTIONS, permissions.CONTROL_RECORDING)
    control.error = ControlError(code, "Said plainly.")
    for method, path, body in (
        ("POST", "/v1/actions/a1/run", {"text": "hi"}),
        ("POST", "/v1/recording/start", {"action_id": "a1"}),
        ("POST", "/v1/recording/stop", None),
        ("POST", "/v1/recording/cancel", None),
        ("POST", "/v1/notify", {"text": "hi"}),
        ("GET", "/v1/live/state", None),
        ("GET", "/v1/actions", None),
    ):
        response = client.request(method, path, json=body, headers=AUTH)
        assert response.status_code == status, path
        assert error(response) == {"code": code, "message": "Said plainly."}


def test_unexpected_failures_are_reported_without_their_message(
    client, settings, control, caplog
):
    grant(settings, permissions.RUN_ACTIONS)
    control.error = RuntimeError("the secret words")
    with caplog.at_level(logging.DEBUG, logger="services.automation.api"):
        response = client.post("/v1/actions/a1/run", json={"text": "the secret words"}, headers=AUTH)
    assert response.status_code == 503
    assert error(response) == {
        "code": "unavailable",
        "message": "OpenWhisper couldn't do that right now. Try again.",
    }
    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text


def test_recording_routes(client, settings, control):
    grant(settings, permissions.CONTROL_RECORDING)
    plain = client.post("/v1/recording/start", headers=AUTH)
    assert (plain.status_code, plain.json()) == (202, {"job_id": "job-rec"})
    action = client.post("/v1/recording/start", json={"action_id": "a1"}, headers=AUTH)
    assert (action.status_code, action.json()) == (202, {"job_id": "job-rec"})
    stop = client.post("/v1/recording/stop", headers=AUTH)
    assert (stop.status_code, stop.json()) == (202, {})
    cancel = client.post("/v1/recording/cancel", headers=AUTH)
    assert (cancel.status_code, cancel.json()) == (202, {})
    assert control.calls == [
        ("start", "", "api"), ("start", "a1", "api"), ("stop",), ("cancel",),
    ]


@pytest.mark.parametrize("body", [{"action_id": 3}, {"action_id": "a" * 65}])
def test_recording_start_checks_the_action_id(client, settings, control, body):
    grant(settings, permissions.CONTROL_RECORDING)
    response = client.post("/v1/recording/start", json=body, headers=AUTH)
    assert response.status_code == 422
    assert error(response)["code"] == "invalid_parameters"
    assert control.calls == []


def test_notify_shows_short_messages(client, settings, control):
    grant(settings, permissions.RUN_ACTIONS)
    response = client.post("/v1/notify", json={"text": "Build passed", "title": "CI"}, headers=AUTH)
    assert (response.status_code, response.json()) == (202, {})
    for body in (
        {"text": "   "},
        {"text": "x" * (api.MAX_NOTICE_TEXT + 1)},
        {"text": "ok", "title": "t" * (api.MAX_NOTICE_TITLE + 1)},
        {"text": "ok", "title": 5},
    ):
        response = client.post("/v1/notify", json=body, headers=AUTH)
        assert response.status_code == 422, body
        assert error(response)["code"] == "invalid_parameters"
    assert control.calls == [("notify", "Build passed", "CI")]


def test_filter_leases(client, settings, monkeypatch):
    grant(settings, permissions.TEXT_FILTERS)
    lease = filters.Lease("lease1", "http://127.0.0.1:9/f", "after_cleanup", 900,
                          ("dictation", "action"), 1_800_000_000.0)
    seen = []

    def register(url, stage, **options):
        seen.append((url, stage, options))
        if stage == "bad":
            raise ValueError("Use a filter address on this computer.")
        return lease

    def renew(lease_id):
        if lease_id != "lease1":
            raise KeyError(lease_id)
        return lease

    monkeypatch.setattr(filters, "register_lease", register)
    monkeypatch.setattr(filters, "renew_lease", renew)
    monkeypatch.setattr(filters, "drop_lease", lambda lease_id: lease_id == "lease1")
    expected = {
        "id": "lease1", "url": "http://127.0.0.1:9/f", "stage": "after_cleanup",
        "timeout_ms": 900, "modes": ["dictation", "action"],
        "expires_at": 1_800_000_000.0, "ttl_s": filters.LEASE_TTL_S,
    }

    created = client.post(
        "/v1/filters",
        json={"url": "http://127.0.0.1:9/f", "stage": "after_cleanup", "timeout_ms": 900},
        headers=AUTH,
    )
    assert (created.status_code, created.json()) == (201, expected)
    default = client.post(
        "/v1/filters", json={"url": "http://127.0.0.1:9/f", "stage": "after_cleanup"}, headers=AUTH
    )
    assert default.status_code == 201
    assert seen == [
        ("http://127.0.0.1:9/f", "after_cleanup", {"timeout_ms": 900}),
        ("http://127.0.0.1:9/f", "after_cleanup", {}),
    ]

    refused = client.post("/v1/filters", json={"url": "http://x/f", "stage": "bad"}, headers=AUTH)
    assert refused.status_code == 422
    assert error(refused) == {
        "code": "invalid_parameters", "message": "Use a filter address on this computer.",
    }
    for body in (
        {"stage": "after_cleanup"},
        {"url": "http://127.0.0.1:9/f"},
        {"url": "http://127.0.0.1:9/f", "stage": "after_cleanup", "timeout_ms": True},
        {"url": "http://127.0.0.1:9/f", "stage": "after_cleanup", "timeout_ms": "900"},
        {"url": "x" * (api.MAX_URL_CHARS + 1), "stage": "after_cleanup"},
    ):
        assert client.post("/v1/filters", json=body, headers=AUTH).status_code == 422, body
    assert len(seen) == 3

    renewed = client.post("/v1/filters/lease1/renew", headers=AUTH)
    assert (renewed.status_code, renewed.json()) == (200, expected)
    expired = client.post("/v1/filters/other/renew", headers=AUTH)
    assert expired.status_code == 404
    assert error(expired)["code"] == "not_found"
    deleted = client.delete("/v1/filters/lease1", headers=AUTH)
    assert (deleted.status_code, deleted.content) == (204, b"")
    assert client.delete("/v1/filters/other", headers=AUTH).status_code == 404


def test_event_stream_requests_are_checked_before_streaming(client, settings):
    unknown = client.get("/v1/events?types=recording.started,not.a.type", headers=AUTH)
    assert unknown.status_code == 422
    assert error(unknown)["code"] == "invalid_parameters"
    bad_resume = client.get("/v1/events", headers={**AUTH, "Last-Event-ID": "evt_1"})
    assert bad_resume.status_code == 422
    negative = client.get("/v1/events?since=-1", headers=AUTH)
    assert negative.status_code == 422
    settings.values[permissions.LIVE_EVENTS] = False
    denied = client.get("/v1/events", headers=AUTH)
    assert denied.status_code == 403
    assert error(denied) == {
        "code": "permission_denied",
        "message": "Allow live events in OpenWhisper Settings > API & MCP.",
    }


def test_without_settings_the_defaults_apply(tmp_path, control, bus):
    path = tmp_path / "live.db"
    DatabaseManager(str(path)).close()
    app = create_app(path, TOKEN)
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        state = test_client.get("/v1/live/state", headers=AUTH).json()
        assert state["permissions"] == {
            "live_events": True, "run_actions": False,
            "control_recording": False, "text_filters": False,
            "agent_prompts": False,
        }
        denied = test_client.post("/v1/actions/a1/run", json={"text": "hi"}, headers=AUTH)
        assert denied.status_code == 403


def test_the_contract_lists_the_live_routes(client):
    schema = client.get("/openapi.json", headers=AUTH).json()
    operations = {
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
    }
    assert {
        "get_live_state", "stream_events", "list_actions", "run_action", "start_recording",
        "stop_recording", "cancel_recording", "create_filter_lease", "renew_filter_lease",
        "delete_filter_lease", "notify_user",
    } <= operations
    run = schema["paths"]["/v1/actions/{action_id}/run"]["post"]["requestBody"]
    text = run["content"]["application/json"]["schema"]["properties"]["text"]
    assert (run["required"], text["maxLength"]) == (True, api.MAX_ACTION_TEXT)


def test_types_and_resume_positions_parse():
    assert api.parse_types(None) is None
    assert api.parse_types(" , ") is None
    assert api.parse_types("recording.started, history.saved") == {
        "recording.started", "history.saved",
    }
    assert api.parse_last_event_id(None) is None
    assert api.parse_last_event_id(" 42 ") == 42
    with pytest.raises(api._Invalid):
        api.parse_types("recording.started,nope")
    for raw in ("-1", "1.5", "9" * 19):
        with pytest.raises(api._Invalid):
            api.parse_last_event_id(raw)


def test_frames_are_one_line_of_json_per_event():
    text = "two\nlines\r\nand \u2028 more \u2029 and \u0085 \x0b \x1c café \U0001f600"
    event = events.EventBus().publish("transcription.completed", {"text": text})
    frame = api.sse_frame(event).decode("utf-8")
    # Readers that split like str.splitlines() still see three lines.
    lines = frame.splitlines()
    assert lines[0] == f"id: {event.seq}"
    assert lines[1] == "event: transcription.completed"
    assert lines[2].startswith("data: {")
    assert lines[3:] == [""]
    assert json.loads(lines[2][len("data: "):]) == event.to_dict()
    # ASCII JSON: the words arrive as escapes and decode to the same text.
    assert frame.isascii()
    gap = api.sse_frame(api.gap_event(7), with_id=False).decode("utf-8")
    assert gap.startswith("event: events.gap\ndata: ")
    assert '"missed_from_seq":7' in gap and '"seq":0' in gap


def test_stopping_the_listener_drops_leased_filters(tmp_path):
    from fastapi.testclient import TestClient

    from services.agent_mcp.app import create_app
    from services.automation import filters

    filters.drop_all_leases()
    path = tmp_path / "history.db"
    DatabaseManager(str(path)).close()
    app = create_app(path, "t" * 40)
    with TestClient(app, base_url="http://127.0.0.1"):
        filters.register_lease("http://127.0.0.1:9/filter", "after_cleanup")
        assert len(filters.leases()) == 1
    assert filters.leases() == []
