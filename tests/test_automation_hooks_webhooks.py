"""Webhook delivery against a real HTTP server on 127.0.0.1.

Saved webhooks come from an in-memory settings store, events from a fresh
EventBus, secrets from an in-memory credential store; the receiver is a real
socket server, so signing, ordering, retries and redirects are checked on
the wire.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

import pytest

from _version import __version__
from services import credentials
from services import settings as settings_module
from services.automation import events, signing, webhooks
from services.automation.models import webhook_secret_name
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings


# -- a real receiver -------------------------------------------------------------

@dataclass
class Answer:
    status: int = 200
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    #: Seconds to wait before answering (ended early when the receiver closes).
    delay: float = 0.0
    #: Wait for this before answering.
    hold: Optional[threading.Event] = None
    #: Builds the body from the request, instead of ``body``.
    reply: Optional[Callable[[Request], bytes]] = None


@dataclass
class Request:
    path: str
    headers: dict
    body: bytes
    at: float

    def json(self):
        return json.loads(self.body)


class Receiver:
    """A threaded HTTP server answering POSTs per path from a script.

    ``answer(path, a, b, c)`` answers the first request with ``a``, the
    second with ``b``, and every later one with ``c``.
    """

    def __init__(self) -> None:
        #: Answered requests, in the order they were answered.
        self.requests: list[Request] = []
        #: Paths of requests as they arrive, before any delay or hold.
        self.arrivals: list[str] = []
        self._cond = threading.Condition()
        self._answers: dict[str, list[Answer]] = {}
        self._closing = threading.Event()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                request = Request(self.path, {k.lower(): v for k, v in self.headers.items()},
                                  body, time.monotonic())
                with receiver._cond:
                    receiver.arrivals.append(self.path)
                    receiver._cond.notify_all()
                answer = receiver._next_answer(self.path)
                if answer.hold is not None:
                    answer.hold.wait(10)
                if answer.delay:
                    receiver._closing.wait(answer.delay)
                with receiver._cond:
                    receiver.requests.append(request)
                    receiver._cond.notify_all()
                body = answer.reply(request) if answer.reply else answer.body
                try:
                    self.send_response(answer.status)
                    for key, value in answer.headers.items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.05,),
                                        daemon=True)
        self._thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def answer(self, path: str, *answers: Answer) -> None:
        with self._cond:
            self._answers[path] = list(answers)

    def _next_answer(self, path: str) -> Answer:
        with self._cond:
            queue = self._answers.get(path) or [Answer()]
            return queue.pop(0) if len(queue) > 1 else queue[0]

    def at(self, path: str) -> list[Request]:
        with self._cond:
            return [r for r in self.requests if r.path == path]

    def wait_for(self, count: int, path: Optional[str] = None, timeout: float = 10.0) -> list[Request]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                got = [r for r in self.requests if path is None or r.path == path]
                if len(got) >= count:
                    return got
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"expected {count} requests at {path}, got {len(got)}")
                self._cond.wait(left)

    def close(self) -> None:
        self._closing.set()
        self._server.shutdown()
        self._server.server_close()


def wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# -- fixtures --------------------------------------------------------------------

@pytest.fixture
def receiver():
    server = Receiver()
    yield server
    server.close()


@pytest.fixture
def bus():
    fresh = events.EventBus()
    previous = events.set_bus(fresh)
    yield fresh
    webhooks.stop()
    fresh.close()
    events.set_bus(previous)


@pytest.fixture
def store(monkeypatch):
    memory = InMemorySettings({})
    monkeypatch.setattr(settings_module, "settings_manager", memory)
    return memory


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    previous = credentials.set_store(
        credentials.CredentialStore(backend_factory=credentials.memory_backend)
    )
    monkeypatch.setattr(webhooks, "RETRY_DELAYS_S", (0.05, 0.1, 0.15))
    webhooks._records.clear()
    yield
    webhooks.stop()
    webhooks._records.clear()
    credentials.set_store(previous)


def save_hooks(store: InMemorySettings, *hooks: dict) -> None:
    rows = []
    for hook in hooks:
        rows.append({"enabled": True, "events": ["recording.started"], **hook})
    store.values[SettingsKey.AUTOMATION_WEBHOOKS] = rows


def hook(hook_id: str, url: str, **extra) -> dict:
    return {"id": hook_id, "name": f"Hook {hook_id}", "url": url, **extra}


# -- delivery --------------------------------------------------------------------

def test_delivers_signed_wire_json_in_order(receiver, bus, store):
    save_hooks(store, hook("a", receiver.url("/a")))
    webhooks.start()
    published = [bus.publish("recording.started", {"job_id": f"job{i}"}) for i in range(5)]

    got = receiver.wait_for(5, "/a")
    secret = webhooks.ensure_secret("a")
    assert [r.json() for r in got] == [e.to_dict() for e in published]
    for request, event in zip(got, published):
        assert signing.verify(secret, request.headers, request.body)
        assert request.headers["webhook-id"] == event.id
        assert request.headers["content-type"] == "application/json"
        assert request.headers["user-agent"] == f"OpenWhisper/{__version__}"
    wait_until(lambda: len(webhooks.deliveries("a")) == 5)
    records = webhooks.deliveries("a")
    assert [r.event_id for r in records] == [e.id for e in reversed(published)]
    assert all(r.ok and r.status == 200 and r.attempt == 1 and not r.error for r in records)


def test_only_enabled_webhooks_get_the_types_they_chose(receiver, bus, store):
    save_hooks(
        store,
        hook("a", receiver.url("/a"), events=["recording.stopped"]),
        hook("b", receiver.url("/b"), events=["recording.started", "recording.stopped"]),
        hook("off", receiver.url("/off"), enabled=False),
    )
    webhooks.start()
    assert bus.subscribers("webhooks") == 1

    bus.publish("recording.started", {"job_id": "1"})
    bus.publish("recording.canceled", {"job_id": "1"})
    bus.publish("recording.stopped", {"job_id": "1", "duration_s": 1.0})

    assert [r.json()["type"] for r in receiver.wait_for(2, "/b")] == [
        "recording.started", "recording.stopped"]
    assert [r.json()["type"] for r in receiver.wait_for(1, "/a")] == ["recording.stopped"]
    time.sleep(0.2)
    assert receiver.at("/off") == []
    assert len(receiver.at("/a")) == 1


def test_start_is_idempotent_and_stop_unsubscribes(receiver, bus, store):
    save_hooks(store, hook("a", receiver.url("/a")))
    webhooks.start()
    webhooks.start()
    assert bus.subscribers("webhooks") == 1
    webhooks.stop()
    webhooks.stop()
    assert bus.subscribers("webhooks") == 0
    bus.publish("recording.started", {"job_id": "1"})
    time.sleep(0.2)
    assert receiver.requests == []


def test_no_webhooks_means_no_subscription(bus, store):
    save_hooks(store)
    webhooks.start()
    assert bus.subscribers("webhooks") == 0


def test_reload_follows_saved_changes(receiver, bus, store):
    save_hooks(store, hook("a", receiver.url("/one")))
    webhooks.start()
    bus.publish("recording.started", {"job_id": "1"})
    receiver.wait_for(1, "/one")

    save_hooks(store, hook("a", receiver.url("/two"), events=["recording.canceled"]))
    webhooks.reload()
    bus.publish("recording.started", {"job_id": "2"})
    bus.publish("recording.canceled", {"job_id": "2"})
    assert [r.json()["type"] for r in receiver.wait_for(1, "/two")] == ["recording.canceled"]

    save_hooks(store, hook("a", receiver.url("/two"), enabled=False))
    webhooks.reload()
    assert bus.subscribers("webhooks") == 0
    bus.publish("recording.canceled", {"job_id": "3"})
    time.sleep(0.2)
    assert len(receiver.at("/two")) == 1
    assert len(receiver.at("/one")) == 1


def test_reload_while_publishing_neither_loses_nor_repeats_events(receiver, bus, store):
    save_hooks(store, hook("a", receiver.url("/a")))
    webhooks.start()
    count = 150

    def publish_all():
        for i in range(count):
            bus.publish("recording.started", {"n": i})

    publisher = threading.Thread(target=publish_all)
    publisher.start()
    for flip in range(30):
        # Changing the types replaces the subscription each time.
        types = ["recording.started"] + (["recording.stopped"] if flip % 2 else [])
        save_hooks(store, hook("a", receiver.url("/a"), events=types))
        webhooks.reload()
    publisher.join()

    got = receiver.wait_for(count, "/a", timeout=30)
    time.sleep(0.3)
    numbers = [r.json()["data"]["n"] for r in receiver.at("/a")]
    assert numbers == list(range(count))
    assert len(got) == count


# -- retries ----------------------------------------------------------------------

def test_5xx_is_retried_with_backoff_and_keeps_the_webhooks_order(receiver, bus, store):
    receiver.answer("/a", Answer(500), Answer(503), Answer(200))
    save_hooks(store, hook("a", receiver.url("/a")))
    webhooks.start()
    first = bus.publish("recording.started", {"job_id": "1"})
    second = bus.publish("recording.started", {"job_id": "2"})

    got = receiver.wait_for(4, "/a")
    assert [r.json()["id"] for r in got] == [first.id, first.id, first.id, second.id]
    assert got[1].at - got[0].at >= 0.05
    assert got[2].at - got[1].at >= 0.1
    wait_until(lambda: len(webhooks.deliveries("a")) == 4)
    attempts = [(r.event_id, r.attempt, r.status, r.ok) for r in reversed(webhooks.deliveries("a"))]
    assert attempts == [
        (first.id, 1, 500, False), (first.id, 2, 503, False),
        (first.id, 3, 200, True), (second.id, 1, 200, True),
    ]


def test_gives_up_after_three_retries(receiver, bus, store):
    receiver.answer("/a", Answer(502))
    save_hooks(store, hook("a", receiver.url("/a")))
    webhooks.start()
    bus.publish("recording.started", {"job_id": "1"})

    receiver.wait_for(4, "/a")
    time.sleep(0.4)
    assert len(receiver.at("/a")) == 4
    newest = webhooks.deliveries("a")[0]
    assert (newest.attempt, newest.status, newest.ok, newest.error) == (4, 502, False, "HTTP 502")


@pytest.mark.parametrize("status", [408, 429])
def test_408_and_429_are_retried(receiver, bus, store, status):
    receiver.answer("/a", Answer(status), Answer(200))
    save_hooks(store, hook("a", receiver.url("/a")))
    webhooks.start()
    bus.publish("recording.started", {"job_id": "1"})
    receiver.wait_for(2, "/a")


@pytest.mark.parametrize("status", [400, 401, 404, 410, 422])
def test_other_4xx_is_never_retried(receiver, bus, store, status):
    receiver.answer("/a", Answer(status), Answer(200))
    save_hooks(store, hook("a", receiver.url("/a")))
    webhooks.start()
    bus.publish("recording.started", {"job_id": "1"})
    receiver.wait_for(1, "/a")
    time.sleep(0.4)
    assert len(receiver.at("/a")) == 1
    record = webhooks.deliveries("a")[0]
    assert (record.ok, record.status, record.error) == (False, status, f"HTTP {status}")


def test_retry_after_is_honoured_up_to_the_cap(receiver, bus, store, monkeypatch):
    monkeypatch.setattr(webhooks, "RETRY_AFTER_MAX_S", 0.6)
    receiver.answer("/a", Answer(429, {"Retry-After": "120"}), Answer(200))
    save_hooks(store, hook("a", receiver.url("/a")))
    webhooks.start()
    bus.publish("recording.started", {"job_id": "1"})

    got = receiver.wait_for(2, "/a")
    waited = got[1].at - got[0].at
    assert 0.55 <= waited < 5


def test_retry_after_parses_seconds_and_dates():
    assert webhooks._retry_after("3") == 3.0
    assert webhooks._retry_after(" 0 ") == 0.0
    assert webhooks._retry_after("") is None
    assert webhooks._retry_after("soon") is None
    assert webhooks._retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
    from email.utils import formatdate

    later = webhooks._retry_after(formatdate(time.time() + 30, usegmt=True))
    assert 25 <= later <= 31


def test_a_redirect_is_a_failure_and_is_not_followed(receiver, bus, store):
    receiver.answer("/a", Answer(307, {"Location": receiver.url("/elsewhere")}))
    save_hooks(store, hook("a", receiver.url("/a")))
    webhooks.start()
    bus.publish("recording.started", {"job_id": "1"})

    receiver.wait_for(1, "/a")
    time.sleep(0.4)
    assert receiver.at("/elsewhere") == []
    assert len(receiver.at("/a")) == 1
    record = webhooks.deliveries("a")[0]
    assert (record.ok, record.status) == (False, 307)
    assert "redirect" in record.error


@pytest.fixture
def hang_up():
    """A server that accepts each connection and closes it without answering."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    accepted = []

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            accepted.append(1)
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}/a", accepted
    server.close()


def test_network_errors_are_retried(bus, store, hang_up):
    url, accepted = hang_up
    save_hooks(store, hook("a", url))
    webhooks.start()
    bus.publish("recording.started", {"job_id": "1"})

    wait_until(lambda: len(webhooks.deliveries("a")) == 4)
    time.sleep(0.3)
    records = webhooks.deliveries("a")
    assert [r.attempt for r in records] == [4, 3, 2, 1]
    # Windows reports a reset socket, Linux a missing answer.
    assert all(r.status is None and r.error in (
        "The server closed the connection", "The connection broke") for r in records)
    assert len(accepted) == 4


def test_a_waiting_retry_does_not_hold_up_other_webhooks(receiver, bus, store, monkeypatch):
    monkeypatch.setattr(webhooks, "RETRY_DELAYS_S", (3.0, 3.0, 3.0))
    receiver.answer("/failing", Answer(500))
    save_hooks(store, hook("a", receiver.url("/failing")), hook("b", receiver.url("/fine")))
    webhooks.start()
    bus.publish("recording.started", {"job_id": "1"})
    bus.publish("recording.started", {"job_id": "2"})

    started = time.monotonic()
    receiver.wait_for(2, "/fine", timeout=2.5)
    assert time.monotonic() - started < 2.5
    # The failing webhook's second event waits behind its first one's retry
    # (each webhook has its own lane, so its first answer may come later).
    receiver.wait_for(1, "/failing", timeout=2.5)
    assert len(receiver.at("/failing")) == 1


def test_a_full_queue_drops_the_oldest_waiting_delivery(receiver, bus, store, monkeypatch):
    monkeypatch.setattr(webhooks, "MAX_QUEUED", 3)
    release = threading.Event()
    receiver.answer("/a", Answer(hold=release), Answer(200))
    save_hooks(store, hook("a", receiver.url("/a")))
    webhooks.start()
    first = bus.publish("recording.started", {"n": 0})
    wait_until(lambda: receiver.arrivals == ["/a"])  # in flight, held
    later = [bus.publish("recording.started", {"n": i}) for i in range(1, 7)]
    wait_until(lambda: sum(1 for r in webhooks.deliveries("a") if "Dropped" in r.error) >= 3)
    release.set()

    got = receiver.wait_for(4, "/a")
    assert [r.json()["id"] for r in got] == [first.id] + [e.id for e in later[3:]]
    dropped = [r.event_id for r in webhooks.deliveries("a") if "Dropped" in r.error]
    assert sorted(dropped) == sorted(e.id for e in later[:3])


def test_stop_returns_quickly_with_a_delivery_in_flight(receiver, bus, store):
    receiver.answer("/a", Answer(delay=4))
    # Its own id: the attempt in flight still records its outcome later.
    save_hooks(store, hook("slow", receiver.url("/a")))
    webhooks.start()
    bus.publish("recording.started", {"job_id": "1"})
    wait_until(lambda: receiver.arrivals == ["/a"])
    started = time.monotonic()
    webhooks.stop()
    assert time.monotonic() - started < 1.5


# -- proxies ---------------------------------------------------------------------

def test_private_addresses_never_go_through_a_proxy(receiver, bus, store, monkeypatch):
    dead_proxy = f"http://127.0.0.1:{closed_port()}"
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, dead_proxy)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    save_hooks(store, hook("a", receiver.url("/a")))
    webhooks.start()
    bus.publish("recording.started", {"job_id": "1"})
    receiver.wait_for(1, "/a")


def test_public_addresses_follow_the_environment():
    with webhooks._new_client(False) as public, webhooks._new_client(True) as private:
        assert public.trust_env is True
        assert private.trust_env is False
        assert public.follow_redirects is False and private.follow_redirects is False
    assert webhooks._is_private("http://192.168.1.20:8080/hook")
    assert webhooks._is_private("http://localhost:9/x")
    assert not webhooks._is_private("https://hooks.example.com/x")


# -- test sends --------------------------------------------------------------------

def test_send_test_delivers_one_signed_test_event_even_when_disabled(receiver, store):
    save_hooks(store, hook("a", receiver.url("/a"), enabled=False))
    future = webhooks.send_test("a")
    record = future.result(timeout=10)

    assert record.ok and record.status == 200 and record.event_type == "webhook.test"
    request = receiver.wait_for(1, "/a")[0]
    body = request.json()
    assert body["type"] == "webhook.test" and body["id"] == record.event_id
    assert body["data"]["webhook_id"] == "a"
    assert signing.verify(webhooks.ensure_secret("a"), request.headers, request.body)
    assert webhooks.deliveries("a") == [record]


def test_send_test_does_not_retry(receiver, store):
    receiver.answer("/a", Answer(500), Answer(200))
    save_hooks(store, hook("a", receiver.url("/a")))
    record = webhooks.send_test("a").result(timeout=10)
    time.sleep(0.3)
    assert (record.ok, record.status, record.attempt) == (False, 500, 1)
    assert len(receiver.at("/a")) == 1


def test_send_test_runs_off_the_callers_thread(receiver, store):
    receiver.answer("/a", Answer(delay=0.5))
    save_hooks(store, hook("a", receiver.url("/a")))
    started = time.monotonic()
    future = webhooks.send_test("a")
    assert time.monotonic() - started < 0.3
    assert future.result(timeout=10).ok


def test_send_test_for_an_unsaved_webhook_says_so(store):
    record = webhooks.send_test("missing").result(timeout=10)
    assert not record.ok and record.error == "Save the webhook first."


# -- secrets -----------------------------------------------------------------------

def test_secrets_are_created_once_rotated_and_deleted():
    first = webhooks.ensure_secret("a")
    assert first.startswith("whsec_")
    assert webhooks.ensure_secret("a") == first
    assert credentials.store().get(webhook_secret_name("a")) == first
    rotated = webhooks.rotate_secret("a")
    assert rotated != first and webhooks.ensure_secret("a") == rotated
    webhooks.delete_secret("a")
    assert credentials.store().get(webhook_secret_name("a")) is None
    webhooks.delete_secret("a")  # nothing stored: still quiet


def test_deliveries_use_the_rotated_secret(receiver, bus, store):
    save_hooks(store, hook("a", receiver.url("/a")))
    webhooks.start()
    old = webhooks.ensure_secret("a")
    new = webhooks.rotate_secret("a")
    bus.publish("recording.started", {"job_id": "1"})
    request = receiver.wait_for(1, "/a")[0]
    assert signing.verify(new, request.headers, request.body)
    assert not signing.verify(old, request.headers, request.body)


def _broken_backend():
    raise RuntimeError("no keyring here")


def test_an_unavailable_credential_store_is_a_clear_message_not_a_crash(receiver, bus, store):
    credentials.set_store(credentials.CredentialStore(backend_factory=_broken_backend))
    with pytest.raises(webhooks.SecretUnavailable, match="signing secret"):
        webhooks.ensure_secret("a")
    with pytest.raises(webhooks.SecretUnavailable):
        webhooks.rotate_secret("a")
    webhooks.delete_secret("a")

    save_hooks(store, hook("a", receiver.url("/a")))
    webhooks.start()
    bus.publish("recording.started", {"job_id": "1"})
    wait_until(lambda: webhooks.deliveries("a"))
    time.sleep(0.3)
    records = webhooks.deliveries("a")
    assert len(records) == 1 and not records[0].ok and "signing secret" in records[0].error
    assert receiver.requests == []


# -- one-off signed posts ------------------------------------------------------------

def test_post_signed_signs_with_the_given_secret(receiver):
    secret = signing.new_secret()
    payload = {"id": "msg_123", "type": "action.run", "text": "héllo"}
    record = webhooks.post_signed(receiver.url("/x"), payload, secret)

    assert record.ok and record.status == 200 and record.event_id == "msg_123"
    assert record.event_type == "action.run"
    request = receiver.wait_for(1, "/x")[0]
    assert request.json() == payload
    assert request.headers["webhook-id"] == "msg_123"
    assert signing.verify(secret, request.headers, request.body)


def test_post_signed_never_raises():
    secret = signing.new_secret()
    assert "http" in webhooks.post_signed("ftp://x/y", {}, secret).error
    assert not webhooks.post_signed("http://127.0.0.1:9/x", {}, "not-a-secret").ok
    assert not webhooks.post_signed("http://127.0.0.1:9/x", {"bad": object()}, secret).ok
    record = webhooks.post_signed(f"http://127.0.0.1:{closed_port()}/x", {}, secret)
    assert (record.ok, record.error) == (False, "Couldn't connect")
    assert record.event_id.startswith("msg_")


def test_post_signed_reply_returns_the_reply_body(receiver):
    secret = signing.new_secret()
    answer = json.dumps({"text": "Réponse"}).encode()
    receiver.answer("/text", Answer(200, {"Content-Type": "application/json"}, answer))
    receiver.answer("/plain", Answer(200, {}, b"not json"))
    receiver.answer("/empty", Answer(204))
    receiver.answer("/fail", Answer(500, {}, b'{"text": "no"}'))
    receiver.answer("/moved", Answer(301, {"Location": "/text"}))

    record, body = webhooks.post_signed_reply(receiver.url("/text"), {"x": 1}, secret)
    assert record.ok and body == answer
    request = receiver.at("/text")[0]
    assert signing.verify(secret, request.headers, request.body)
    assert webhooks.post_signed_reply(receiver.url("/plain"), {}, secret)[1] == b"not json"
    assert webhooks.post_signed_reply(receiver.url("/empty"), {}, secret)[1] == b""
    record, body = webhooks.post_signed_reply(receiver.url("/fail"), {}, secret)
    assert (record.ok, record.status, body) == (False, 500, None)
    record, body = webhooks.post_signed_reply(receiver.url("/moved"), {}, secret)
    assert (record.ok, record.status, body) == (False, 301, None)
    assert len(receiver.at("/text")) == 1


def test_post_signed_reply_refuses_a_reply_over_max_bytes(receiver):
    receiver.answer("/big", Answer(200, {}, b'{"text": "' + b"x" * 500 + b'"}'))
    record, body = webhooks.post_signed_reply(receiver.url("/big"), {}, signing.new_secret(),
                                              max_bytes=100)
    assert body is None and not record.ok and record.error == "The reply was too large"
    record, body = webhooks.post_signed_reply(receiver.url("/big"), {}, signing.new_secret())
    assert record.ok and len(body) == 512


def test_post_signed_reply_never_raises():
    record, body = webhooks.post_signed_reply("not a url", {}, signing.new_secret())
    assert (record.ok, body) == (False, None)
    record, body = webhooks.post_signed_reply(
        "http://127.0.0.1:9/x", {}, signing.new_secret(), max_bytes="lots")
    assert (record.ok, body) == (False, None)


def test_post_signed_takes_a_message_id_and_extra_headers(receiver):
    secret = signing.new_secret()
    record = webhooks.post_signed(
        receiver.url("/x"), {"id": "evt_payload", "type": "meeting.started"}, secret,
        msg_id="msg_chosen",
        headers={"X-MCP-Subscription-Id": "sub_1", "webhook-signature": "v1,forged",
                 "Content-Type": "text/plain"},
    )
    assert record.ok and record.event_id == "msg_chosen"
    request = receiver.wait_for(1, "/x")[0]
    assert request.headers["webhook-id"] == "msg_chosen"
    assert request.headers["x-mcp-subscription-id"] == "sub_1"
    assert request.headers["content-type"] == "application/json"
    assert signing.verify(secret, request.headers, request.body)

    _record, _body = webhooks.post_signed_reply(receiver.url("/y"), {}, secret,
                                                msg_id="msg_two", headers={"X-Extra": "1"})
    request = receiver.wait_for(1, "/y")[0]
    assert (request.headers["webhook-id"], request.headers["x-extra"]) == ("msg_two", "1")


# -- privacy ---------------------------------------------------------------------------

def test_logs_never_carry_urls_event_data_or_secrets(receiver, bus, store, caplog):
    caplog.set_level(logging.DEBUG)
    receiver.answer("/a?token=sekrit", Answer(500))
    url = receiver.url("/a?token=sekrit")
    save_hooks(store, hook("a", url))
    webhooks.start()
    bus.publish("recording.started", {"job_id": "dictated words here"})
    wait_until(lambda: len(webhooks.deliveries("a")) == 4)
    secret = webhooks.ensure_secret("a")
    for needle in ("sekrit", "dictated words", secret, f":{receiver.port}",
                   f"port={receiver.port}"):
        assert needle not in caplog.text


def test_other_requests_keep_their_log_lines(receiver, caplog):
    import httpx

    webhooks._install_quiet_requests()
    caplog.set_level(logging.INFO, logger="httpx")
    with httpx.Client() as client:
        client.post(receiver.url("/other"))
    assert "/other" in caplog.text
