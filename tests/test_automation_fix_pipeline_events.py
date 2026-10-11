"""Event bus hardening: per-run numbering, late subscribers, UTF-8, deleted text."""

import threading
import time
from types import SimpleNamespace

import pytest

from services.automation import events
from services.automation.events import EventBus


def _wait(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


@pytest.fixture
def fresh_bus():
    """Let ``events.bus()`` create the process-wide bus anew, then put it back."""
    previous = events.set_bus(None)
    yield
    created = events.set_bus(previous)
    if created is not None:
        created.close()


# -- numbering that only grows across app runs (protocol #2) ---------------------

def test_the_app_bus_numbers_from_a_base_that_grows_across_runs(fresh_bus, monkeypatch):
    monkeypatch.setattr(events.time, "time", lambda: 1_790_000_000.123)
    first = events.bus().publish("recording.started", {})
    assert first.seq == 1_790_000_000_123 + 1
    assert len(str(first.seq)) <= 18

    # The next run starts later, so every number it gives is new.
    previous = events.set_bus(None)
    previous.close()
    monkeypatch.setattr(events.time, "time", lambda: 1_790_000_060.0)
    second = events.bus().publish("recording.started", {})
    assert second.seq > first.seq


def test_a_position_from_an_earlier_run_reads_as_a_gap(fresh_bus, monkeypatch):
    monkeypatch.setattr(events.time, "time", lambda: 1_790_000_000.0)
    bus = events.bus()
    base = bus.last_seq()
    one = bus.publish("recording.started", {})

    # The earlier run's last event had a smaller number than this run's base.
    replay, gap = bus.since(base - 5)
    assert [e.seq for e in replay] == [one.seq] and gap
    replay, gap = bus.since(0)
    assert [e.seq for e in replay] == [one.seq] and gap
    # From this run's own positions, nothing was missed.
    replay, gap = bus.since(base)
    assert [e.seq for e in replay] == [one.seq] and not gap
    assert bus.since(one.seq) == ([], False)


def test_a_bus_made_directly_still_counts_from_one():
    bus = EventBus()
    assert bus.publish("recording.started", {}).seq == 1
    assert bus.since(0) == (bus.recent(), False)


# -- a subscription only sees what came after it (concurrency #2) ----------------

def test_a_late_subscriber_never_gets_events_published_before_it_subscribed():
    bus = EventBus()
    release = threading.Event()
    busy = threading.Event()

    def slow(event):
        busy.set()
        release.wait(2)

    bus.subscribe(slow)
    bus.publish("recording.started", {"n": 1})
    assert busy.wait(2)  # the dispatcher is held on the first event
    bus.publish("recording.stopped", {"n": 2})  # queued, not yet handed out
    late = []
    bus.subscribe(late.append)
    release.set()
    after = bus.publish("history.saved", {"n": 3})
    assert _wait(lambda: late)
    time.sleep(0.05)
    assert [e.seq for e in late] == [after.seq]
    bus.close()


def test_replacing_a_subscription_new_before_old_loses_nothing():
    bus = EventBus()
    release, busy = threading.Event(), threading.Event()

    def slow(event):
        busy.set()
        release.wait(2)

    bus.subscribe(slow)
    old_seen, new_seen = [], []
    old = bus.subscribe(old_seen.append)
    bus.publish("a", {})
    assert busy.wait(2)
    bus.publish("b", {})  # published while only the old one is open
    bus.subscribe(new_seen.append)
    old.close()
    bus.publish("c", {})
    release.set()
    assert _wait(lambda: new_seen and len(old_seen) == 2)
    time.sleep(0.05)
    assert [e.type for e in old_seen] == ["a", "b"]
    assert [e.type for e in new_seen] == ["c"]
    bus.close()


# -- data must encode as UTF-8 (protocol #1) -------------------------------------

def test_a_lone_surrogate_is_refused_at_the_publisher():
    bus = EventBus()
    with pytest.raises(TypeError):
        bus.publish("channel.message", {"text": "hi \ud83d"})
    assert bus.last_seq() == 0 and bus.recent() == []
    ok = bus.publish("channel.message", {"text": "hi \U0001f600"})
    assert ok.seq == 1


def test_the_module_publish_drops_it_quietly():
    previous = events.set_bus(EventBus())
    try:
        assert events.publish("channel.message", {"text": "\udc00"}) is None
        assert events.bus().recent() == []
    finally:
        events.set_bus(previous)


# -- deleted text can't be replayed (security #5, concurrency #8) ----------------

@pytest.fixture
def replaying_bus():
    bus = EventBus()
    previous = events.set_bus(bus)
    bus.publish("transcription.completed", {"text": "my secret"})
    yield bus
    events.set_bus(previous)
    bus.close()


def test_clear_on_the_module_forgets_only_an_existing_bus(fresh_bus):
    events.clear()  # creates nothing
    assert events.set_bus(None) is None


class _FakeDb:
    def __init__(self):
        self.calls = []

    def delete_history_entry(self, entry_id):
        self.calls.append(("delete", entry_id))
        return True

    def get_history_entry_by_id(self, entry_id):
        return None

    def clear_history(self):
        self.calls.append(("clear",))


@pytest.fixture
def manager(monkeypatch, tmp_path):
    from services import history_manager as module

    monkeypatch.setattr(module, "db", _FakeDb())
    sync = SimpleNamespace(record_deleted=lambda *a: None, cleared=lambda *a: None)
    monkeypatch.setattr(module, "_record_sync", lambda: sync)
    return module.HistoryManager(recordings_folder=str(tmp_path), max_recordings=None)


@pytest.mark.parametrize("delete", [
    lambda m: m.delete_entry("e1"),
    lambda m: m.clear_history(),
    lambda m: m.clear_history_and_recordings(),
])
def test_deleting_history_drops_the_replay_buffer(replaying_bus, manager, delete):
    delete(manager)
    assert replaying_bus.recent() == []


def test_deleting_a_meeting_drops_the_replay_buffer(replaying_bus):
    from meeting.persist import data_lifecycle

    class Repository:
        def get_meeting(self, meeting_id):
            return {"id": meeting_id, "spool_dir": "", "origin_device_id": ""}

        def delete_meeting(self, meeting_id):
            pass

    assert data_lifecycle.delete_meeting_data(Repository(), "m1", "", delete_spool=False)
    assert replaying_bus.recent() == []
