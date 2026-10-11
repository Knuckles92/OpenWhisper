"""Each webhook has its own delivery lane: a slow or unreachable receiver only
delays its own events (review: one slow webhook delayed every other one)."""

from __future__ import annotations

import threading
import time

from services.automation import webhooks
from tests import test_automation_hooks_webhooks as base
from tests.test_automation_hooks_webhooks import Answer, hook, save_hooks, wait_until

# The webhook suite's fixtures: a real receiver, a fresh bus, in-memory
# settings and credentials, short retry delays.
receiver = base.receiver
bus = base.bus
store = base.store
isolated = base.isolated


def test_a_slow_webhook_does_not_delay_the_others(receiver, bus, store):
    # Answers after 2 s, inside the 5 s timeout: a slow receiver, not a failing one.
    receiver.answer("/slow", Answer(delay=2.0))
    save_hooks(store, hook("slow", receiver.url("/slow")), hook("fast", receiver.url("/fast")))
    webhooks.start()
    started = time.monotonic()
    published = [bus.publish("recording.started", {"n": i}) for i in range(3)]

    got = receiver.wait_for(3, "/fast", timeout=1.5)
    assert time.monotonic() - started < 1.5
    assert [r.json()["id"] for r in got] == [e.id for e in published]
    # The slow one still gets every event, in order.
    slow = receiver.wait_for(3, "/slow", timeout=15)
    assert [r.json()["id"] for r in slow] == [e.id for e in published]


def test_an_unreachable_webhooks_retries_do_not_delay_the_others(receiver, bus, store, monkeypatch):
    monkeypatch.setattr(webhooks, "RETRY_DELAYS_S", (2.0, 2.0, 2.0))
    receiver.answer("/down", Answer(503))
    save_hooks(store, hook("down", receiver.url("/down")), hook("up", receiver.url("/up")))
    webhooks.start()
    for i in range(5):
        bus.publish("recording.started", {"n": i})
    receiver.wait_for(5, "/up", timeout=1.5)
    assert len(receiver.at("/down")) == 1


def test_a_full_queue_only_drops_that_webhooks_own_deliveries(receiver, bus, store, monkeypatch):
    monkeypatch.setattr(webhooks, "MAX_QUEUED", 3)
    release = threading.Event()
    receiver.answer("/stuck", Answer(hold=release), Answer(200))
    save_hooks(store, hook("stuck", receiver.url("/stuck")), hook("fine", receiver.url("/fine")))
    webhooks.start()
    first = bus.publish("recording.started", {"n": 0})
    wait_until(lambda: "/stuck" in receiver.arrivals)  # in flight, held
    later = []
    for i in range(1, 9):
        # The other webhook keeps up, one at a time, while the stuck one backs up.
        later.append(bus.publish("recording.started", {"n": i}))
        receiver.wait_for(i + 1, "/fine")

    # The other webhook gets all nine and never loses one to the stuck one's backlog.
    fine = receiver.wait_for(9, "/fine")
    assert [r.json()["id"] for r in fine] == [first.id] + [e.id for e in later]
    assert not any("Dropped" in r.error for r in webhooks.deliveries("fine"))

    wait_until(lambda: sum("Dropped" in r.error for r in webhooks.deliveries("stuck")) == 5)
    release.set()
    stuck = receiver.wait_for(4, "/stuck")
    assert [r.json()["id"] for r in stuck] == [first.id] + [e.id for e in later[5:]]


def test_an_idle_lane_lets_its_thread_go(receiver, bus, store, monkeypatch):
    monkeypatch.setattr(webhooks, "LANE_IDLE_S", 0.1)
    save_hooks(store, hook("a", receiver.url("/a")), hook("b", receiver.url("/b")))
    webhooks.start()
    bus.publish("recording.started", {"n": 0})
    receiver.wait_for(1, "/a")
    receiver.wait_for(1, "/b")

    def lanes() -> int:
        return sum(t.name.startswith("automation-webhooks") and t.is_alive()
                   for t in threading.enumerate())

    wait_until(lambda: lanes() == 0, timeout=5)
    # A later event starts a lane again.
    bus.publish("recording.started", {"n": 1})
    receiver.wait_for(2, "/a")
    receiver.wait_for(2, "/b")
