"""The live event stream over real sockets: the listener's app under uvicorn.

httpx's ASGITransport buffers a streaming body until it ends, so these tests
run a real server in a thread and read the stream as a client would.
"""

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from services.agent_mcp.app import create_app
from services.agent_mcp.runtime import McpRuntime
from services.automation import api, catalog, events, permissions
from services.automation.control import LiveState, set_app_control
from services.credentials import CredentialStore, memory_backend
from services.database import DatabaseManager

TOKEN = "live-sse-test-token-" + "c" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeSettings:
    def __init__(self, **values):
        self.values = dict(values)

    def load_all_settings(self):
        return dict(self.values)


class IdleControl:
    def state(self):
        return LiveState(False, False, False, "", "", "", "9.9.9")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class LiveServer:
    """The in-app listener's app on a real port, in a thread."""

    def __init__(self, app):
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_config=None, log_level="critical",
            lifespan="on", ws="none", access_log=False, timeout_graceful_shutdown=1,
        ))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.url = f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        assert wait_until(lambda: self.server.started, 10)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)


class Reader:
    """Parses an SSE response into messages: field dicts, or {"comment": …}."""

    def __init__(self, response):
        self.response = response
        self.lines = response.iter_lines()

    def message(self):
        fields = {}
        for line in self.lines:
            if line == "":
                if fields:
                    return fields
                continue
            if line.startswith(":"):
                return {"comment": line[1:].strip()}
            name, _, value = line.partition(":")
            fields[name] = value[1:] if value.startswith(" ") else value
        return fields or None

    def event(self):
        """The next message with an event type; None when the stream ended."""
        while True:
            message = self.message()
            if message is None or "event" in message:
                return message

    def rest(self):
        messages = []
        while (message := self.message()) is not None:
            messages.append(message)
        return messages


@pytest.fixture
def bus():
    fresh = events.EventBus()
    previous = events.set_bus(fresh)
    yield fresh
    events.set_bus(previous)
    fresh.close()


@pytest.fixture
def control():
    set_app_control(IdleControl())
    yield
    set_app_control(None)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "live.db"
    DatabaseManager(str(path)).close()
    return path


@pytest.fixture
def settings():
    return FakeSettings()


@pytest.fixture
def enabled():
    return {"on": True}


@pytest.fixture
def server(database, settings, enabled, bus, control):
    app = create_app(database, TOKEN, enabled=lambda: enabled["on"], settings=settings)
    with LiveServer(app) as live:
        yield live


@pytest.fixture
def client(server):
    with httpx.Client(base_url=server.url, trust_env=False, timeout=5.0) as http:
        yield http


def open_stream(client, path="/v1/events", **headers):
    return client.stream("GET", path, headers={**AUTH, **headers})


def payload(message):
    return json.loads(message["data"])


def test_events_arrive_live_as_server_sent_events(client, bus):
    with open_stream(client) as response:
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
        assert response.headers["cache-control"] == "no-store"
        reader = Reader(response)
        assert reader.message() == {"retry": "2000"}
        published = bus.publish(catalog.RECORDING_STARTED, {"job_id": "j1", "mode": "dictation"})
        message = reader.event()
    assert message["id"] == str(published.seq)
    assert message["event"] == "recording.started"
    assert payload(message) == published.to_dict()


def test_events_published_as_the_stream_opens_are_not_missed(client, bus):
    with open_stream(client) as response:
        # The client sees the headers before the stream's subscription exists;
        # an event published now must still arrive.
        published = bus.publish(catalog.HISTORY_SAVED, {"job_id": "j", "record_id": "1", "kind": "x"})
        message = Reader(response).event()
    assert message["id"] == str(published.seq)


def test_line_break_characters_in_text_survive_line_readers(client, bus):
    text = "one\u2028two\u2029three\u0085four\nfive"
    with open_stream(client) as response:
        reader = Reader(response)
        bus.publish(catalog.TRANSCRIPTION_COMPLETED, {"job_id": "j", "text": text})
        message = reader.event()
    assert payload(message)["data"]["text"] == text


def test_types_limit_the_stream(client, bus):
    with open_stream(client, "/v1/events?types=history.saved,recording.stopped") as response:
        reader = Reader(response)
        bus.publish(catalog.RECORDING_STARTED, {"job_id": "j"})
        stopped = bus.publish(catalog.RECORDING_STOPPED, {"job_id": "j", "duration_s": 1.0})
        bus.publish(catalog.TRANSCRIPTION_PREVIEW, {"job_id": "j", "text": "x"}, replay=False)
        saved = bus.publish(catalog.HISTORY_SAVED, {"job_id": "j", "record_id": "1", "kind": "x"})
        received = [reader.event()["id"], reader.event()["id"]]
    assert received == [str(stopped.seq), str(saved.seq)]


def test_reconnecting_with_last_event_id_replays_what_was_missed(client, bus):
    with open_stream(client) as response:
        reader = Reader(response)
        first = bus.publish(catalog.RECORDING_STARTED, {"job_id": "j"})
        assert reader.event()["id"] == str(first.seq)
    missed = [
        bus.publish(catalog.RECORDING_STOPPED, {"job_id": "j", "duration_s": 2.0}),
        bus.publish(catalog.TRANSCRIPTION_COMPLETED, {"job_id": "j", "text": "hello"}),
    ]
    # EventSource reconnects to its original URL; the header is the newer position.
    with open_stream(client, "/v1/events?since=0", **{"Last-Event-ID": str(first.seq)}) as response:
        reader = Reader(response)
        replayed = [reader.event(), reader.event()]
        live = bus.publish(catalog.HISTORY_SAVED, {"job_id": "j", "record_id": "1", "kind": "x"})
        after = reader.event()
    assert [m["id"] for m in replayed] == [str(e.seq) for e in missed]
    assert [payload(m) for m in replayed] == [e.to_dict() for e in missed]
    assert after["id"] == str(live.seq)


def test_since_replays_buffered_events_of_the_wanted_types(client, bus):
    started = bus.publish(catalog.RECORDING_STARTED, {"job_id": "j"})
    bus.publish(catalog.RECORDING_STOPPED, {"job_id": "j", "duration_s": 1.0})
    saved = bus.publish(catalog.HISTORY_SAVED, {"job_id": "j", "record_id": "1", "kind": "x"})
    with open_stream(client, "/v1/events?since=0&types=recording.started,history.saved") as response:
        reader = Reader(response)
        received = [reader.event()["id"], reader.event()["id"]]
    assert received == [str(started.seq), str(saved.seq)]


def test_a_position_the_buffer_no_longer_holds_starts_with_a_gap(client):
    small = events.EventBus(buffer_size=3)
    previous = events.set_bus(small)
    try:
        published = [small.publish(catalog.RECORDING_STARTED, {"job_id": str(n)}) for n in range(6)]
        with open_stream(client, "/v1/events?since=1") as response:
            reader = Reader(response)
            gap = reader.event()
            replayed = [reader.event()["id"] for _ in range(3)]
    finally:
        events.set_bus(previous)
        small.close()
    assert "id" not in gap  # synthetic: a reconnect keeps its real position
    assert gap["event"] == "events.gap"
    assert payload(gap)["seq"] == 0
    assert payload(gap)["data"] == {"missed_from_seq": 2, "reason": catalog.GAP_EVICTED}
    assert replayed == [str(e.seq) for e in published[3:]]


def test_a_position_from_an_earlier_app_session_replays_this_one_after_a_gap(client, bus):
    published = [bus.publish(catalog.RECORDING_STARTED, {"job_id": str(n)}) for n in range(2)]
    with open_stream(client, **{"Last-Event-ID": "900"}) as response:
        reader = Reader(response)
        gap = reader.event()
        replayed = [reader.event()["id"] for _ in range(2)]
    assert payload(gap)["data"] == {"missed_from_seq": 1, "reason": catalog.GAP_RESTARTED}
    assert replayed == [str(e.seq) for e in published]


def test_channel_clients_are_counted_apart_and_released_on_disconnect(client, bus):
    def listeners():
        return client.get("/v1/live/state", headers=AUTH).json()["listeners"]

    # Keep each Reader: dropping one closes its line iterator, which makes
    # httpx close the connection.
    with open_stream(client, **{api.CLIENT_HEADER: api.CHANNEL_CLIENT}) as channel:
        channel_reader = Reader(channel)
        channel_reader.message()
        with open_stream(client) as plain:
            plain_reader = Reader(plain)
            plain_reader.message()
            assert listeners() == {"events": 1, "claude_channel": 1}
        assert wait_until(lambda: bus.subscribers(api.SSE_LABEL) == 0)
        assert listeners() == {"events": 0, "claude_channel": 1}
    assert wait_until(lambda: bus.subscribers() == 0)


def test_streams_end_when_mcp_is_switched_off(client, bus, enabled):
    with open_stream(client) as response:
        reader = Reader(response)
        reader.message()
        started = time.monotonic()
        enabled["on"] = False
        assert reader.rest() == []
        elapsed = time.monotonic() - started
    assert elapsed < api.ENABLED_CHECK_S + 1.0
    assert wait_until(lambda: bus.subscribers() == 0)


def test_streams_end_when_live_events_are_disallowed(client, bus, settings, monkeypatch):
    monkeypatch.setattr(api, "PERMISSION_CHECK_S", 0.1)
    with open_stream(client) as response:
        reader = Reader(response)
        reader.message()
        started = time.monotonic()
        settings.values[permissions.LIVE_EVENTS] = False
        assert reader.rest() == []
        elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert wait_until(lambda: bus.subscribers() == 0)


def test_an_idle_stream_pings(client, monkeypatch):
    monkeypatch.setattr(api, "PING_INTERVAL_S", 0.2)
    with open_stream(client) as response:
        reader = Reader(response)
        assert reader.message() == {"retry": "2000"}
        assert reader.message() == {"comment": "ping"}


def test_stopping_the_listener_ends_open_streams_promptly(database, settings, bus, control):
    runtime = McpRuntime(database, credentials=CredentialStore(memory_backend), settings=settings)
    port = free_port()
    runtime.start(port)
    try:
        assert wait_until(lambda: runtime.status().state == "running", 10)
        token = runtime.token()
        with httpx.Client(trust_env=False, timeout=5.0) as http:
            with http.stream(
                "GET", f"http://127.0.0.1:{port}/v1/events",
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                reader = Reader(response)
                reader.message()
                started = time.monotonic()
                runtime.stop(wait=True)
                stopped = time.monotonic() - started
                assert reader.rest() == []
    finally:
        runtime.stop(wait=True)
    # McpRuntime switches enabled() off before uvicorn shuts down, so the
    # stream ends on its own instead of waiting out the graceful timeout.
    assert stopped < 2.0
    assert runtime.status().state == "stopped"
    assert bus.subscribers() == 0


def test_a_server_stopped_without_disabling_still_releases_its_streams(
    database, settings, bus, control
):
    app = create_app(database, TOKEN, settings=settings)
    live = LiveServer(app).__enter__()
    with httpx.Client(trust_env=False, timeout=5.0) as http:
        with http.stream("GET", f"{live.url}/v1/events", headers=AUTH) as response:
            reader = Reader(response)
            reader.message()
            assert bus.subscribers() == 1
            live.server.should_exit = True
            # uvicorn gives in-flight requests no shutdown signal: the stream
            # is cancelled when the graceful timeout runs out.
            live.thread.join(10)
            assert not live.thread.is_alive()
            assert bus.subscribers() == 0
            with pytest.raises(httpx.HTTPError):
                reader.rest()


def test_a_reader_too_slow_for_the_queue_gets_a_gap_and_the_end(bus, monkeypatch):
    monkeypatch.setattr(api, "STREAM_QUEUE_SIZE", 5)

    async def scenario():
        stream = api.EventStream(
            types=None, replay_from=bus.last_seq(), label=api.SSE_LABEL,
            enabled=lambda: True, allowed=lambda: True,
        )
        body = stream.body()
        assert await body.__anext__() == b"retry: 2000\n\n"
        waiting = asyncio.ensure_future(body.__anext__())
        await asyncio.sleep(0.05)
        first = bus.publish(catalog.RECORDING_STARTED, {"job_id": "first"})
        delivered = await asyncio.wait_for(waiting, 5)
        # The client now stops reading while more arrive than the queue holds.
        later = [bus.publish(catalog.RECORDING_STARTED, {"job_id": str(n)}) for n in range(7)]
        deadline = time.monotonic() + 5
        while not stream._overflow_seq and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        rest = [chunk async for chunk in body]
        return first, delivered, later, rest

    first, delivered, later, rest = asyncio.run(scenario())
    assert delivered == api.sse_frame(first)
    assert len(rest) == 1
    gap = rest[0].decode()
    assert gap.startswith("event: events.gap\n")
    assert json.loads(gap.split("data: ", 1)[1])["data"] == {
        "missed_from_seq": later[0].seq, "reason": catalog.GAP_SLOW,
    }
    assert bus.subscribers() == 0
