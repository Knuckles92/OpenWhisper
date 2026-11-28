"""What paired clients are doing, and live tools routed to them, through the host's MCP."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from services.agent_mcp.app import INSTRUCTIONS, create_app
from services.agent_mcp.client_tools import ClientTools
from services.agent_mcp.controls import CONTROLS_BY_KEY
from services.agent_mcp.runtime import McpRuntime
from services.automation import permissions
from services.automation.control import LiveState, set_app_control
from services.backup import _sanitize_settings
from services.credentials import CredentialStore, memory_backend
from services.database import DatabaseManager
from services.remote_asr import protocol, tailscale
from services.remote_asr.client import RemoteConnection, pair_with_host
from services.remote_asr.host import DeviceRegistry, SpeechHost
from services.remote_asr.settings import ClientPairing, client_shares_live
from services.remote_asr.tls import ensure_host_identity
from services.remote_history.channel import (
    ClientToolError,
    ClientUnavailable,
    HistoryBroker,
)
from services.remote_history.client import HistoryClient
from services.settings import SettingsKey, SettingsManager
from tests.fakes.settings import InMemorySettings
from tests.test_agent_mcp import free_port, wait_for
from tests.test_automation_mcp_tools import TOKEN, call, error_text, rpc
from tests.test_remote_engine import FakeEngine, ListStore

MEETING = {"active": True, "paused": False, "meeting_id": "m_live", "status": "active",
           "elapsed_s": 42.0, "title": "Launch review"}


def wait_until(check):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    assert check()


class ClientApp:
    """The paired client's running app: in a meeting, and recording what it is asked."""

    def __init__(self):
        self.calls = []

    def state(self):
        self.calls.append("state")
        return LiveState(False, False, True, "", "", "", "3.1.0", meeting_state=MEETING)

    def meeting_status(self):
        self.calls.append("meeting_status")
        return MEETING

    def pause_meeting(self):
        self.calls.append("pause_meeting")
        return {"paused": True, "meeting_id": "m_live"}

    def notify(self, text, *, title=""):
        self.calls.append(("notify", text, title))


@pytest.fixture
def paired(tmp_path, monkeypatch):
    """A host's MCP and one paired client, joined by the real TLS side connection."""
    monkeypatch.setattr(
        tailscale, "status", lambda timeout=4: tailscale.TailscaleStatus("not_installed")
    )
    devices = ListStore()
    host = SpeechHost(
        engine_provider=FakeEngine,
        registry=DeviceRegistry(devices.load, devices.save),
        identity=ensure_host_identity(str(tmp_path / "identity")),
    )
    host.start(port=0, bind="127.0.0.1")
    pairing = pair_with_host("127.0.0.1", host.port, host.open_pairing(), "laptop")
    client_db = tmp_path / "client.db"
    DatabaseManager(str(client_db)).close()
    host_db = tmp_path / "host.db"
    DatabaseManager(str(host_db)).close()

    app = ClientApp()
    set_app_control(app)
    client_settings = InMemorySettings({})
    host_settings = InMemorySettings({SettingsKey.MCP_ENABLED: True})
    live = [False]
    forwarded = []
    tools = ClientTools(settings=client_settings)

    def run(tool, arguments):
        forwarded.append((tool, arguments))
        return tools.call(tool, arguments)

    client = HistoryClient(
        client_db,
        pairing=lambda: ClientPairing("127.0.0.1", host.port, pairing.fingerprint, "host"),
        token=lambda: pairing.token,
        enabled=lambda: False,
        live_enabled=lambda: live[0],
        live_tools=run,
    )
    client.start()
    wait_until(lambda: host.history.live_clients()[0]["live"] == "disabled")
    mcp_app = create_app(
        host_db, TOKEN, enabled=lambda: True, settings=host_settings,
        client_history=host.history, client_presence=host.connected_clients,
    )

    def allow_live(allowed=True):
        live[0] = allowed
        client.refresh()
        wait_until(
            lambda: host.history.live_clients()[0]["live"]
            == ("available" if allowed else "disabled")
        )

    with TestClient(mcp_app, base_url="http://127.0.0.1") as mcp:
        yield SimpleNamespace(
            mcp=mcp, host=host, client=client, app=app, device=pairing.device_id,
            pairing=pairing, live=live, forwarded=forwarded, allow_live=allow_live,
            host_settings=host_settings, client_settings=client_settings,
        )
    client.stop()
    host.stop()
    set_app_control(None)


def only_client(result):
    (client,) = result["structuredContent"]["clients"]
    return client


def test_live_access_defaults_off_and_clients_say_so(paired):
    assert client_shares_live({}) is False
    # No agent can turn it on, and a restored backup comes back with it off.
    assert SettingsKey.REMOTE_CLIENT_LIVE not in CONTROLS_BY_KEY
    assert _sanitize_settings({"remote_client_live": True})["remote_client_live"] is False
    client = only_client(call(paired.mcp, "list_clients"))
    assert client == {
        "device_id": paired.device, "name": "laptop", "connected": False,
        "history": "sharing_disabled", "live": "disabled",
    }
    refused = call(paired.mcp, "get_meeting_live_state", {"device_id": paired.device})
    assert error_text(refused).startswith("live_disabled: This client does not allow live access")
    # Nothing was sent to the client, and nothing ran on its app.
    assert paired.forwarded == [] and paired.app.calls == []


def test_list_clients_reports_what_an_opted_in_client_is_doing(paired):
    paired.allow_live()
    client = only_client(call(paired.mcp, "list_clients"))
    assert client["live"] == "available"
    state = client["state"]
    assert state["meeting"] is True and state["recording"] is False
    assert state["meeting_state"]["title"] == "Launch review"
    assert state["meeting_state"]["elapsed_s"] == 42.0
    # The permissions are the client's own, which the host's settings don't change.
    assert state["permissions"]["control_meetings"] is False
    assert paired.forwarded == [("get_live_state", {})]

    listed = call(paired.mcp, "list_clients", {"include_state": False})
    assert "state" not in only_client(listed)
    assert only_client(call(paired.mcp, "list_clients", {"device_id": paired.device}))["state"]
    missing = call(paired.mcp, "list_clients", {"device_id": "nobody"})
    assert error_text(missing) == "not_found: No paired client has that device_id."


def test_list_clients_includes_what_the_host_sees_of_a_speech_connection(paired):
    speech = RemoteConnection(
        "127.0.0.1", paired.host.port, paired.pairing.token, paired.pairing.fingerprint,
        reports_recording=True,
    )
    speech.connect()
    try:
        wait_until(lambda: paired.host.connected_clients())
        client = only_client(call(paired.mcp, "list_clients"))
        assert client["connected"] is True
        assert client["recording"] is False and client["transcribing"] is False
        assert client["connected_since"].endswith("+00:00")
        # Seeing the connection is the host's own knowledge; live state still needs consent.
        assert client["live"] == "disabled" and "state" not in client
    finally:
        speech.close()


def test_a_read_tool_runs_on_the_client_with_its_device_id(paired):
    paired.allow_live()
    result = call(paired.mcp, "get_meeting_live_state", {"device_id": paired.device})
    assert result["structuredContent"] == MEETING
    assert paired.forwarded == [("get_meeting_live_state", {})]
    assert paired.app.calls == ["meeting_status"]


def test_a_routed_change_needs_the_permission_on_both_computers(paired):
    paired.allow_live()
    arguments = {"device_id": paired.device}

    refused = error_text(call(paired.mcp, "pause_meeting", arguments))
    assert refused == "permission_denied: Allow controlling Meeting Mode in OpenWhisper Settings > API & MCP."
    assert paired.forwarded == []  # the host's own switch is off: never sent

    paired.host_settings.save_setting(permissions.CONTROL_MEETINGS, True)
    refused = error_text(call(paired.mcp, "pause_meeting", arguments))
    assert refused == (
        "permission_denied: Allow controlling Meeting Mode in OpenWhisper Settings > API & MCP."
        f" [paired client {paired.device}]"
    )
    assert paired.forwarded == [("pause_meeting", {})] and paired.app.calls == []

    paired.client_settings.save_setting(permissions.CONTROL_MEETINGS, True)
    result = call(paired.mcp, "pause_meeting", arguments)
    assert result["structuredContent"] == {"paused": True, "meeting_id": "m_live"}
    assert paired.app.calls == ["pause_meeting"]


def test_arguments_reach_the_client_validated(paired):
    paired.allow_live()
    for settings in (paired.host_settings, paired.client_settings):
        settings.save_setting(permissions.RUN_ACTIONS, True)
    arguments = {"text": "Your meeting notes are ready.", "title": "OpenWhisper", "device_id": paired.device}
    assert call(paired.mcp, "notify_user", arguments)["structuredContent"] == {"shown": True}
    assert paired.forwarded == [
        ("notify_user", {"text": "Your meeting notes are ready.", "title": "OpenWhisper"})
    ]
    assert paired.app.calls == [("notify", "Your meeting notes are ready.", "OpenWhisper")]
    invalid = rpc(paired.mcp, "tools/call", {
        "name": "notify_user", "arguments": {"text": "", "device_id": paired.device},
    })
    assert "error" in invalid or invalid["result"]["isError"] is True
    assert len(paired.forwarded) == 1


def test_turning_live_access_off_stops_the_very_next_call(paired):
    paired.allow_live()
    # Off in Settings, before the connection has been renewed to say so.
    paired.live[0] = False
    refused = call(paired.mcp, "get_meeting_live_state", {"device_id": paired.device})
    assert error_text(refused).startswith("live_disabled: ")
    assert paired.forwarded == [] and paired.app.calls == []
    assert only_client(call(paired.mcp, "list_clients"))["live"] == "disabled"


def test_offline_unknown_and_removed_clients_are_unavailable(paired):
    paired.allow_live()
    unknown = call(paired.mcp, "get_live_state", {"device_id": "nobody"})
    assert error_text(unknown).startswith("unavailable: ")
    paired.client.stop()
    wait_until(lambda: paired.host.history.live_clients()[0]["live"] == "offline")
    assert only_client(call(paired.mcp, "list_clients"))["live"] == "offline"
    offline = call(paired.mcp, "get_live_state", {"device_id": paired.device})
    assert error_text(offline).startswith("unavailable: ")
    paired.host.remove_device(paired.device)
    assert call(paired.mcp, "list_clients")["structuredContent"] == {"clients": []}


def test_a_tool_without_device_id_runs_on_the_host(paired):
    paired.allow_live()
    assert call(paired.mcp, "get_meeting_live_state")["structuredContent"] == MEETING
    assert paired.forwarded == []


def test_an_older_client_is_unsupported_and_never_asked(paired):
    paired.client.stop()
    old = RemoteConnection(
        "127.0.0.1", paired.host.port, paired.pairing.token, paired.pairing.fingerprint,
        history_enabled=True,
    )
    old.connect()
    try:
        wait_until(lambda: paired.host.history.live_clients()[0]["live"] == "unsupported")
        client = only_client(call(paired.mcp, "list_clients"))
        assert client["live"] == "unsupported" and client["history"] == "online"
        refused = call(paired.mcp, "get_live_state", {"device_id": paired.device})
        assert error_text(refused).startswith("unsupported: ")
        with pytest.raises(TimeoutError):
            old.receive_history(timeout=0.2)
    finally:
        old.close()


def test_an_agent_on_the_real_listener_finds_a_meeting_and_reads_it(paired, tmp_path):
    """The path an assistant takes: the listener, the official client, then the client's app."""
    paired.allow_live()
    host_db = tmp_path / "listener.db"
    DatabaseManager(str(host_db)).close()
    server = McpRuntime(
        host_db,
        credentials=CredentialStore(memory_backend),
        settings=SettingsManager(str(tmp_path / "settings.json")),
    )
    server.configure_controls(
        client_history=paired.host.history, client_presence=paired.host.connected_clients
    )
    server.start(free_port())
    wait_for(server, "running")

    async def exercise():
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {server.token()}"}, trust_env=False
        ) as http:
            async with Client(
                streamable_http_client(server.status().url, http_client=http)
            ) as agent:
                listed = await agent.call_tool("list_clients", {})
                (client,) = listed.structured_content["clients"]
                assert client["live"] == "available" and client["state"]["meeting"] is True
                meeting = await agent.call_tool(
                    "get_meeting_live_state", {"device_id": client["device_id"]}
                )
                assert meeting.structured_content == MEETING

    try:
        asyncio.run(exercise())
    finally:
        server.stop(wait=True)
    assert paired.forwarded == [("get_live_state", {}), ("get_meeting_live_state", {})]


# ---- the routing itself, without a network ----


#: Tools whose device_id picks whose saved records to read, or which client to describe.
READS_BY_DEVICE = {
    "search_history", "list_transcriptions", "get_transcription", "list_meetings",
    "get_meeting", "list_meeting_segments", "get_meeting_segment", "get_meeting_insights",
    "list_clients",
}


class RecordingClients:
    """Stands in for the broker: every tool call is recorded and answered."""

    def __init__(self):
        self.calls = []

    def clients(self):
        return [{"device_id": "dev", "name": "Laptop", "status": "online"}]

    def live_clients(self):
        return [{"device_id": "dev", "name": "Laptop", "live": "available"}]

    def call_tool(self, device_id, tool, arguments):
        self.calls.append((device_id, tool, arguments))
        return {"ran_on": device_id}


class UntouchableApp:
    """The host's app, which a routed call must never reach."""

    def __getattr__(self, name):
        raise AssertionError(f"A routed tool called the host's app: {name}")


def minimal(schema, definitions):
    """The smallest value a tool's JSON schema accepts."""
    if "$ref" in schema:
        return minimal(definitions[schema["$ref"].rsplit("/", 1)[1]], definitions)
    if "anyOf" in schema:
        return minimal(next(s for s in schema["anyOf"] if s.get("type") != "null"), definitions)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "string":
        return "x" * max(1, schema.get("minLength", 1))
    if kind in ("integer", "number"):
        return schema.get("minimum", 0)
    if kind == "boolean":
        return False
    if kind == "array":
        return [minimal(schema["items"], definitions) for _ in range(schema.get("minItems", 0))]
    assert kind == "object", schema
    return {
        name: minimal(schema["properties"][name], definitions)
        for name in schema.get("required", ())
    }


@pytest.fixture
def routed(tmp_path):
    path = tmp_path / "history.db"
    DatabaseManager(str(path)).close()
    clients = RecordingClients()
    settings = InMemorySettings(
        {SettingsKey.MCP_ENABLED: True, **{key: True for key in permissions.ALL}}
    )
    set_app_control(UntouchableApp())
    app = create_app(path, TOKEN, enabled=lambda: True, settings=settings, client_history=clients)
    with TestClient(app, base_url="http://127.0.0.1") as mcp:
        yield SimpleNamespace(mcp=mcp, clients=clients, settings=settings)
    set_app_control(None)


def test_every_app_tool_takes_a_device_id_and_runs_only_on_that_client(routed):
    tools = {tool["name"]: tool for tool in rpc(routed.mcp, "tools/list")["result"]["tools"]}
    routable = {
        name: tool for name, tool in tools.items()
        if "device_id" in tool["inputSchema"]["properties"] and name not in READS_BY_DEVICE
    }
    # The same tools a client can run for its host: none can be reached only one way.
    client_side = ClientTools(settings=InMemorySettings({}))
    assert set(routable) == {tool.name for tool in client_side._mcp._tool_manager.list_tools()}
    assert len(routable) > 60
    for name, tool in routable.items():
        schema = tool["inputSchema"]
        arguments = minimal(schema, schema.get("$defs", {}))
        result = call(routed.mcp, name, {**arguments, "device_id": "dev"})
        assert result.get("isError") is not True, (name, result)
        assert result["structuredContent"] == {"ran_on": "dev"}, name
        device, sent_tool, sent = routed.clients.calls[-1]
        assert (device, sent_tool) == ("dev", name)
        assert "device_id" not in sent and json.dumps(sent)
    assert len(routed.clients.calls) == len(routable)


def test_routed_tools_keep_their_schema_and_annotations(routed):
    tools = {tool["name"]: tool for tool in rpc(routed.mcp, "tools/list")["result"]["tools"]}
    pause = tools["pause_meeting"]
    assert pause["description"] == "Pause the active meeting; needs Control Meeting Mode."
    assert pause["annotations"]["readOnlyHint"] is False
    assert "device_id" not in pause["inputSchema"].get("required", [])
    device = pause["inputSchema"]["properties"]["device_id"]
    assert "list_clients" in device["description"] and device["default"] is None
    start = tools["start_meeting"]["inputSchema"]
    assert list(start["properties"]) == ["title", "cloud", "device_id"]
    assert tools["list_clients"]["annotations"]["readOnlyHint"] is True
    # Tools that change this computer's saved data or settings stay here.
    for name in ("update_settings", "retitle_meeting", "set_record_annotation"):
        assert "device_id" not in tools[name]["inputSchema"]["properties"]
    assert "list_clients" in INSTRUCTIONS and "device_id" in INSTRUCTIONS


def test_a_host_permission_that_is_off_never_reaches_the_client(routed):
    routed.settings.save_setting(permissions.CONTROL_RECORDING, False)
    refused = call(routed.mcp, "start_dictation", {"device_id": "dev"})
    assert error_text(refused).startswith("permission_denied: Allow starting recordings")
    assert routed.clients.calls == []


def test_a_server_without_paired_clients_says_routing_is_unsupported(tmp_path):
    path = tmp_path / "history.db"
    DatabaseManager(str(path)).close()
    settings = InMemorySettings({SettingsKey.MCP_ENABLED: True})
    set_app_control(UntouchableApp())
    try:
        app = create_app(path, TOKEN, enabled=lambda: True, settings=settings)
        with TestClient(app, base_url="http://127.0.0.1") as mcp:
            for name in ("list_clients", "get_live_state"):
                refused = call(mcp, name, {"device_id": "dev"})
                assert error_text(refused).startswith("unsupported: "), name
    finally:
        set_app_control(None)


# ---- the channel and the client's side ----


class Socket:
    """A client's connection as the broker holds it: what was sent, and frames to read."""

    def __init__(self):
        self.sent = []
        self.frames = []
        self.closed = threading.Event()

    def send(self, frame):
        self.sent.append(json.loads(frame))

    def close(self, *args):
        self.closed.set()

    def __iter__(self):
        while not self.closed.is_set():
            if self.frames:
                yield self.frames.pop(0)
            else:
                time.sleep(0.005)


@pytest.fixture
def channel():
    device = {"id": "dev", "name": "Laptop"}
    registry = SimpleNamespace(list=lambda: [device], authenticate=lambda token: device)
    broker = HistoryBroker(registry)
    broker.start()
    socket = Socket()
    threading.Thread(
        target=broker.serve, args=(socket, device, "token", True), kwargs={"live": True},
        daemon=True,
    ).start()
    wait_until(lambda: socket.sent and socket.sent[0]["type"] == "ready")
    yield SimpleNamespace(broker=broker, socket=socket)
    broker.remove()


def answer(channel, **reply):
    """Answer the next live_call the broker sends, from another thread."""

    def run():
        wait_until(lambda: len(channel.socket.sent) > 1)
        request = channel.socket.sent[-1]
        channel.socket.frames.append(
            json.dumps({"type": "live_result", "id": request["id"], **reply})
        )

    threading.Thread(target=run, daemon=True).start()


def test_the_host_sends_a_live_call_and_returns_the_clients_result(channel):
    answer(channel, result={"active": True})
    assert channel.broker.call_tool("dev", "get_meeting_live_state", {}) == {"active": True}
    sent = channel.socket.sent[-1]
    assert (sent["type"], sent["tool"], sent["arguments"]) == ("live_call", "get_meeting_live_state", {})


@pytest.mark.parametrize("reply, raised, text", [
    ({"error": "busy: A recording is already running."}, ClientToolError,
     "busy: A recording is already running."),
    ({"error": "no code\nhere " + "x" * 900}, ClientToolError,
     "unavailable: The client could not do that right now."),
    ({"result": ["not", "an", "object"]}, ClientToolError,
     "unavailable: The client could not do that right now."),
    ({"error": "live_disabled"}, ClientUnavailable, "This client does not allow live access"),
    ({"error": "unavailable"}, ClientUnavailable, "Client history is unavailable"),
])
def test_a_clients_refusal_is_kept_short_and_in_shape(channel, reply, raised, text):
    answer(channel, **reply)
    with pytest.raises(raised) as caught:
        channel.broker.call_tool("dev", "start_dictation", {})
    assert str(caught.value).startswith(text)


def test_arguments_too_large_for_the_connection_are_refused_before_sending(channel):
    with pytest.raises(ClientToolError, match="^invalid_parameters: .* too large"):
        channel.broker.call_tool(
            "dev", "run_action", {"text": "é" * (protocol.MAX_HEADER_BYTES // 6)}
        )
    assert [frame["type"] for frame in channel.socket.sent] == ["ready"]


def test_a_client_that_never_answers_times_out(channel, monkeypatch):
    monkeypatch.setattr("services.remote_history.channel.TOOL_TIMEOUT", 0.05)
    with pytest.raises(ClientUnavailable):
        channel.broker.call_tool("dev", "get_live_state", {})


def live_reply(client, **request):
    return json.loads(client._live_reply({"type": "live_call", "id": "r1", **request}))


def test_the_client_rechecks_its_opt_in_around_every_call():
    allowed = [True]
    ran = []

    def tools(tool, arguments):
        ran.append(tool)
        if tool == "revoke":
            allowed[0] = False
        return {"result": {"ok": True}}

    client = HistoryClient(
        pairing=lambda: None, token=lambda: None, enabled=lambda: False,
        live_enabled=lambda: allowed[0], live_tools=tools,
    )
    assert live_reply(client, tool="get_live_state", arguments={}) == {
        "type": "live_result", "id": "r1", "result": {"ok": True},
    }
    assert live_reply(client, tool=7, arguments={})["error"].startswith("invalid_query: ")
    assert live_reply(client, tool="x", arguments=[])["error"].startswith("invalid_query: ")
    # Turned off while the tool ran: the result stays on the client.
    assert live_reply(client, tool="revoke", arguments={}) == {
        "type": "live_result", "id": "r1", "error": "live_disabled",
    }
    assert live_reply(client, tool="get_live_state", arguments={})["error"] == "live_disabled"
    assert ran == ["get_live_state", "revoke"]


def test_a_client_tool_that_crashes_or_answers_too_much_is_unavailable():
    def crash(tool, arguments):
        raise RuntimeError("dictated text and /a/private/path")

    client = HistoryClient(
        pairing=lambda: None, token=lambda: None, enabled=lambda: False,
        live_enabled=lambda: True, live_tools=crash,
    )
    assert live_reply(client, tool="x", arguments={}) == {
        "type": "live_result", "id": "r1", "error": "unavailable",
    }
    client._live_tools = lambda tool, arguments: {
        "result": {"image": "x" * (protocol.MAX_REPLY_BYTES + 1)}
    }
    assert live_reply(client, tool="x", arguments={})["error"] == "unavailable"


def test_client_tools_apply_this_computers_permissions_and_hide_crashes():
    class App:
        def state(self):
            return LiveState(True, False, False, "dictation", "", "job", "3.1.0")

        def pause_meeting(self):
            raise RuntimeError("private detail")

    settings = InMemorySettings({})
    tools = ClientTools(settings=settings)
    set_app_control(App())
    try:
        state = tools.call("get_live_state", {})["result"]
        assert state["recording"] is True and state["permissions"]["control_meetings"] is False
        assert tools.call("pause_meeting", {}) == {
            "error": "permission_denied: Allow controlling Meeting Mode in OpenWhisper Settings > API & MCP."
        }
        settings.save_setting(permissions.CONTROL_MEETINGS, True)
        crashed = tools.call("pause_meeting", {})["error"]
        assert crashed.startswith("unavailable: ") and "private" not in crashed
        assert tools.call("update_settings", {"changes": {}})["error"].startswith("unsupported: ")
        assert tools.call("list_clients", {})["error"].startswith("unsupported: ")
        assert tools.call("run_action", {"action_id": "", "text": "x"})["error"].startswith(
            "invalid_parameters: "
        )
    finally:
        set_app_control(None)
