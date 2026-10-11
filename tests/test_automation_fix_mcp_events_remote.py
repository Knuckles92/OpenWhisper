"""MCP Events from another computer: no reaching into this one, no outliving access.

A subscription remembers whether it was made on this computer or by a
Tailscale peer. A peer's subscriptions are delivered only while Allow agents
over Tailscale is on, and a peer can't point deliveries at this computer or
its local network (by address or by a name that resolves there).
"""

import json
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import config
from services.agent_mcp import events_ext
from services.agent_mcp.app import create_app
from services.automation import catalog, events, signing
from services.database import DatabaseManager
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings
from tests.test_automation_mcp_events import (
    TOKEN,
    Receiver,
    error_of,
    result_of,
    rpc,
    subscribe_params,
)

HOST = "100.82.22.3"
PEER = ("100.65.148.53", 1234)
LOCAL = ("127.0.0.1", 50000)


@pytest.fixture
def receiver():
    server = Receiver()
    yield server
    server.close()


@pytest.fixture
def bus(monkeypatch):
    monkeypatch.setattr(events_ext, "_registry_instance", None)
    replacement = events.EventBus()
    previous = events.set_bus(replacement)
    yield replacement
    events_ext.stop_delivery()
    events.set_bus(previous)
    replacement.close()


@pytest.fixture
def settings():
    return InMemorySettings({
        SettingsKey.MCP_ENABLED: True,
        SettingsKey.MCP_EVENTS_ENABLED: True,
        SettingsKey.MCP_TAILSCALE_ENABLED: True,
    })


@pytest.fixture
def clients(tmp_path, settings, bus):
    """A Tailscale peer's and a local agent's view of the listener."""
    path = tmp_path / "history.db"
    DatabaseManager(str(path)).close()

    def app():
        return create_app(path, TOKEN, enabled=lambda: True, settings=settings, allowed_hosts=(HOST,))

    with TestClient(app(), base_url=f"http://{HOST}", client=PEER) as peer, \
            TestClient(app(), base_url="http://127.0.0.1", client=LOCAL) as local:
        events_ext.start_delivery(settings=settings)
        yield peer, local


@pytest.mark.parametrize(
    "url",
    [
        "loopback",  # the receiver on 127.0.0.1
        "https://127.0.0.1:8443/hook",
        "https://localhost/hook",
        "https://[::1]/hook",
        "https://[::ffff:127.0.0.1]/hook",
        "https://0.0.0.0/hook",
        "https://192.168.1.20/hook",
        "https://10.1.2.3/hook",
        "https://169.254.169.254/latest",
        "https://printer.local/hook",
    ],
)
def test_a_peer_cannot_point_deliveries_at_this_computer_or_its_network(clients, receiver, url):
    peer, _ = clients
    url = receiver.url() if url == "loopback" else url
    error = error_of(rpc(peer, "events/subscribe", subscribe_params(
        catalog.HISTORY_SAVED, url, signing.new_secret()
    )))
    assert error["code"] == events_ext.INVALID_PARAMS
    assert "another computer" in error["message"]
    assert receiver.requests == []
    assert events_ext.subscription_count() == 0


def test_handshake_era_clients_are_told_apart_the_same_way(clients, receiver):
    peer, local = clients
    params = subscribe_params(catalog.HISTORY_SAVED, receiver.url(), signing.new_secret())
    assert error_of(rpc(peer, "events/subscribe", params, modern=False))["code"] == (
        events_ext.INVALID_PARAMS
    )
    assert result_of(rpc(local, "events/subscribe", params, modern=False))["id"].startswith("sub_")


def test_a_name_that_resolves_to_this_computer_counts_as_this_computer(clients, monkeypatch):
    peer, _ = clients
    real = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        if host == "rebind.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    error = error_of(rpc(peer, "events/subscribe", subscribe_params(
        catalog.HISTORY_SAVED, "https://rebind.example/hook", signing.new_secret()
    )))
    assert error["code"] == events_ext.INVALID_PARAMS


def test_this_computer_may_still_subscribe_its_own_receivers(clients, receiver):
    _, local = clients
    granted = result_of(rpc(local, "events/subscribe", subscribe_params(
        catalog.HISTORY_SAVED, receiver.url(), signing.new_secret()
    )))
    saved = json.loads(Path(config.MCP_EVENT_SUBSCRIPTIONS_FILE).read_text(encoding="utf-8"))
    assert [record["origin"] for record in saved["subscriptions"]] == ["local"]
    assert granted["deliveryStatus"]["active"] is True


def peer_subscription(clients, receiver, monkeypatch, secret):
    """A subscription a peer made; its callback stands in for one on the internet."""
    peer, _ = clients
    url = receiver.url()
    monkeypatch.setattr(
        events_ext, "_destination", lambda target: "public" if target == url else "private"
    )
    granted = result_of(rpc(peer, "events/subscribe", subscribe_params(catalog.HISTORY_SAVED, url, secret)))
    return granted


def handed_out(bus):
    """Wait until the bus has handed every event published so far to its subscribers.

    One thread hands events out in order, so once a later marker arrives,
    MCP Events has seen (and queued or dropped) everything before it.
    """
    arrived = threading.Event()
    marker = bus.subscribe(lambda event: arrived.set(), types=["test.marker"])
    bus.publish("test.marker", {})
    assert arrived.wait(5)
    marker.close()


def saved(n):
    return {"job_id": f"j{n}", "record_id": str(n), "kind": "dictation"}


def test_a_peers_subscription_is_delivered_only_while_tailscale_access_is_on(
    clients, receiver, settings, bus, monkeypatch
):
    secret = signing.new_secret()
    granted = peer_subscription(clients, receiver, monkeypatch, secret)
    records = json.loads(Path(config.MCP_EVENT_SUBSCRIPTIONS_FILE).read_text(encoding="utf-8"))
    assert [record["origin"] for record in records["subscriptions"]] == ["tailscale"]
    assert granted["deliveryStatus"]["active"] is True

    first = bus.publish(catalog.HISTORY_SAVED, saved(1))
    [(_, headers, body, payload)] = receiver.wait_for_deliveries(1)
    assert payload["eventId"] == first.id and signing.verify(secret, headers, body)

    # The user turned Tailscale access off: the peer's subscription goes quiet.
    settings.values[SettingsKey.MCP_TAILSCALE_ENABLED] = False
    bus.publish(catalog.HISTORY_SAVED, saved(2))
    handed_out(bus)
    settings.values[SettingsKey.MCP_TAILSCALE_ENABLED] = True
    third = bus.publish(catalog.HISTORY_SAVED, saved(3))
    deliveries = receiver.wait_for_deliveries(2)
    assert [d[3]["eventId"] for d in deliveries] == [first.id, third.id]


class HeldSends:
    """Counts queued deliveries; workers wait for ``release()`` before each try."""

    def __init__(self, deliver, push):
        self._deliver = deliver
        self._push = push
        self._gate = threading.Event()
        self._cond = threading.Condition()
        self.queued = 0
        self.done = 0

    def push(self, deliverer, *args, **kwargs):
        accepted = self._push(deliverer, *args, **kwargs)
        with self._cond:
            self.queued += bool(accepted)
        return accepted

    def deliver(self, job, client=None):
        try:
            assert self._gate.wait(10)
            return self._deliver(job, client)
        finally:
            with self._cond:
                self.done += 1
                self._cond.notify_all()

    def release(self):
        """Let every try go, and wait until each one queued so far has finished."""
        self._gate.set()
        with self._cond:
            queued = self.queued
            assert self._cond.wait_for(lambda: self.done >= queued, 10)


@pytest.fixture
def held_sends(settings, monkeypatch):
    held = HeldSends(events_ext._deliver, events_ext._Deliverer.push)
    monkeypatch.setattr(events_ext, "_deliver", held.deliver)
    monkeypatch.setattr(events_ext._Deliverer, "push", lambda self, *a, **k: held.push(self, *a, **k))
    # A fresh deliverer, so its workers call the held version.
    events_ext.stop_delivery()
    events_ext.start_delivery(settings=settings)
    return held


def test_an_event_from_while_access_was_off_stays_unsent_when_access_returns_first(
    clients, receiver, settings, bus, monkeypatch, held_sends
):
    peer_subscription(clients, receiver, monkeypatch, signing.new_secret())
    settings.values[SettingsKey.MCP_TAILSCALE_ENABLED] = False
    bus.publish(catalog.HISTORY_SAVED, saved(1))
    handed_out(bus)
    # Access is back before any worker got to send.
    settings.values[SettingsKey.MCP_TAILSCALE_ENABLED] = True
    second = bus.publish(catalog.HISTORY_SAVED, saved(2))
    handed_out(bus)
    held_sends.release()
    assert held_sends.queued == 1
    assert [d[3]["eventId"] for d in receiver.wait_for_deliveries(1)] == [second.id]


def test_access_turned_off_before_the_send_still_stops_it(
    clients, receiver, settings, bus, monkeypatch, held_sends
):
    peer_subscription(clients, receiver, monkeypatch, signing.new_secret())
    bus.publish(catalog.HISTORY_SAVED, saved(1))
    handed_out(bus)
    settings.values[SettingsKey.MCP_TAILSCALE_ENABLED] = False
    held_sends.release()
    assert held_sends.queued == 1
    assert receiver.deliveries() == []


def test_a_local_subscription_does_not_need_tailscale_access(clients, receiver, settings, bus):
    _, local = clients
    settings.values[SettingsKey.MCP_TAILSCALE_ENABLED] = False
    result_of(rpc(local, "events/subscribe", subscribe_params(
        catalog.HISTORY_SAVED, receiver.url(), signing.new_secret()
    )))
    published = bus.publish(catalog.HISTORY_SAVED, {"job_id": "j", "record_id": "1", "kind": "dictation"})
    [(_, _, _, payload)] = receiver.wait_for_deliveries(1)
    assert payload["eventId"] == published.id


def test_a_peers_callback_that_later_resolves_here_is_not_delivered(
    clients, receiver, bus, monkeypatch
):
    peer_subscription(clients, receiver, monkeypatch, signing.new_secret())
    monkeypatch.setattr(events_ext, "_destination", lambda target: "private")
    bus.publish(catalog.HISTORY_SAVED, {"job_id": "j1", "record_id": "1", "kind": "dictation"})
    registry = events_ext._registry()
    [subscription] = registry.active()
    deadline = time.monotonic() + 5
    while "lastError" not in registry.delivery_status(subscription.id, True):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert registry.delivery_status(subscription.id, True)["lastError"] == "address_refused"
    assert receiver.deliveries() == []


def test_saved_subscriptions_without_an_origin_count_as_a_peers():
    record = {
        "id": "sub_" + "0" * 32, "name": catalog.HISTORY_SAVED, "arguments": {},
        "url": "https://example.com/hook", "refresh_before": "2099-01-01T00:00:00Z",
        "created_at": "",
    }
    assert events_ext.EventSubscription.from_record(record).origin == "tailscale"
    assert events_ext.EventSubscription.from_record({**record, "origin": "local"}).origin == "local"
