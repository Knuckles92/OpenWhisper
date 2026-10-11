"""MCP Events' saved subscriptions: no waiting on slow storage, and no expired leftovers.

The credential store can be slow (a keychain or Secret Service prompt) and
the subscriptions file is fsynced. Neither may hold up the event bus's
dispatch thread or the count Settings reads on the Qt thread every 500 ms.
Expired subscriptions and their secrets are forgotten when delivery starts
and when they are counted, not only on the next subscribe.
"""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from config import config
from services import credentials
from services.agent_mcp import events_ext
from services.automation import catalog, events
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings

SECRET = "whsec_" + "A" * 32


class SlowBackend(credentials.MemoryCredentialBackend):
    """A credential backend whose writes stall while ``slow`` is set."""

    def __init__(self):
        super().__init__()
        self.slow = threading.Event()
        self.entered = threading.Event()

    def _stall(self):
        if self.slow.is_set():
            self.entered.set()
            time.sleep(1.0)

    def set_password(self, service, username, password):
        self._stall()
        return super().set_password(service, username, password)

    def delete_password(self, service, username):
        self._stall()
        return super().delete_password(service, username)


@pytest.fixture
def backend():
    slow = SlowBackend()
    previous = credentials.set_store(credentials.CredentialStore(backend_factory=lambda: (slow, "slow")))
    yield slow
    credentials.set_store(previous)


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
    return InMemorySettings({SettingsKey.MCP_ENABLED: True, SettingsKey.MCP_EVENTS_ENABLED: True})


def subscription(n, *, expires_in=3600.0):
    return events_ext.EventSubscription(
        f"sub_{n:032x}", catalog.TRANSCRIPTION_COMPLETED, {}, f"http://127.0.0.1:9/{n}",
        int(time.time() + expires_in), "", events_ext.ORIGIN_LOCAL,
    )


def timed(fn):
    started = time.monotonic()
    result = fn()
    return result, time.monotonic() - started


def test_slow_secret_removal_blocks_neither_counting_nor_the_bus(backend, bus, settings):
    events_ext.start_delivery(settings=settings)
    registry = events_ext._registry()
    for n in range(3):
        registry.upsert(subscription(n), SECRET)
    received = []
    bus.subscribe(lambda event: received.append((event.seq, time.monotonic())), label="other")

    backend.slow.set()
    clearing = threading.Thread(target=events_ext.clear_subscriptions)
    clearing.start()
    assert backend.entered.wait(5)

    count, elapsed = timed(events_ext.subscription_count)
    assert elapsed < 0.3
    assert count == 0  # the clear already took them out of memory

    published_at = time.monotonic()
    event = bus.publish(catalog.TRANSCRIPTION_COMPLETED, {"job_id": "j", "text": "x"})
    deadline = time.monotonic() + 5
    while not any(seq == event.seq for seq, _ in received) and time.monotonic() < deadline:
        time.sleep(0.005)
    arrived = [at for seq, at in received if seq == event.seq]
    assert arrived and arrived[0] - published_at < 0.3
    clearing.join(10)
    assert not clearing.is_alive()


def test_a_slow_save_of_a_new_subscription_does_not_block_counting(backend, bus, settings):
    events_ext.start_delivery(settings=settings)
    registry = events_ext._registry()
    registry.upsert(subscription(1), SECRET)
    backend.slow.set()
    saving = threading.Thread(target=registry.upsert, args=(subscription(2), "whsec_" + "B" * 32))
    saving.start()
    assert backend.entered.wait(5)
    count, elapsed = timed(events_ext.subscription_count)
    assert elapsed < 0.3 and count == 1
    saving.join(10)
    assert events_ext.subscription_count() == 2


def test_a_slow_fsync_does_not_block_counting(bus, settings, monkeypatch):
    events_ext.start_delivery(settings=settings)
    registry = events_ext._registry()
    registry.upsert(subscription(1), SECRET)
    entered = threading.Event()
    real_fsync = os.fsync

    def slow_fsync(fd):
        entered.set()
        time.sleep(1.0)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", slow_fsync)
    saving = threading.Thread(target=registry.remove, args=(subscription(1).id,))
    saving.start()
    assert entered.wait(5)
    count, elapsed = timed(events_ext.subscription_count)
    assert elapsed < 0.3 and count == 0
    saving.join(10)


def expire_saved(sub_id):
    path = Path(config.MCP_EVENT_SUBSCRIPTIONS_FILE)
    saved = json.loads(path.read_text(encoding="utf-8"))
    for record in saved["subscriptions"]:
        if record["id"] == sub_id:
            record["refresh_before"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(saved), encoding="utf-8")


def eventually(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def forgotten(sub_id):
    path = Path(config.MCP_EVENT_SUBSCRIPTIONS_FILE)
    return (
        credentials.store().get(events_ext.secret_name(sub_id)) is None
        and sub_id not in path.read_text(encoding="utf-8")
    )


def saved_then_expired(monkeypatch):
    """One live and one expired subscription, as a restarted app finds them."""
    registry = events_ext._registry()
    live, old = subscription(1), subscription(2)
    registry.upsert(live, SECRET)
    registry.upsert(old, SECRET)
    expire_saved(old.id)
    monkeypatch.setattr(events_ext, "_registry_instance", None)
    return live, old


def test_starting_delivery_forgets_expired_subscriptions(bus, settings, monkeypatch):
    live, old = saved_then_expired(monkeypatch)
    events_ext.start_delivery(settings=settings)
    assert eventually(lambda: forgotten(old.id))
    assert credentials.store().get(events_ext.secret_name(live.id)) == SECRET
    assert events_ext.subscription_count() == 1


def test_counting_forgets_expired_subscriptions(bus, monkeypatch):
    live, old = saved_then_expired(monkeypatch)
    assert events_ext.subscription_count() == 1
    assert eventually(lambda: forgotten(old.id))
    assert credentials.store().get(events_ext.secret_name(live.id)) == SECRET


def test_a_subscription_that_expires_while_running_is_forgotten_when_counted(bus, settings):
    events_ext.start_delivery(settings=settings)
    registry = events_ext._registry()
    soon = subscription(3, expires_in=1.2)  # whole seconds: expires 0.2 to 1.2 s from now
    registry.upsert(soon, SECRET)
    assert events_ext.subscription_count() == 1
    time.sleep(max(0.0, soon.refresh_before - time.time()) + 0.05)
    assert events_ext.subscription_count() == 0
    assert eventually(lambda: forgotten(soon.id))
