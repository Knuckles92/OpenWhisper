"""The in-process event bus behind the live API, hooks and MCP Events."""

import json
import threading
import time

import pytest

from services.automation import catalog, events
from services.automation.events import EventBus


def _wait(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


def test_subscribers_get_events_in_order_on_another_thread():
    bus = EventBus()
    seen, threads = [], []

    def callback(event):
        seen.append((event.seq, event.type))
        threads.append(threading.current_thread())

    bus.subscribe(callback)
    for n in range(5):
        bus.publish("recording.started", {"n": n})
    assert _wait(lambda: len(seen) == 5)
    assert seen == [(n, "recording.started") for n in range(1, 6)]
    assert threading.current_thread() not in threads
    bus.close()


def test_type_filter_and_labels():
    bus = EventBus()
    wanted = []
    sub = bus.subscribe(wanted.append, types=["history.saved"], label="claude-channel")
    bus.subscribe(lambda e: None, label="other")
    bus.publish("recording.started", {})
    bus.publish("history.saved", {"record_id": "1"})
    assert _wait(lambda: len(wanted) == 1)
    assert wanted[0].type == "history.saved"
    assert bus.subscribers() == 2
    assert bus.subscribers("claude-channel") == 1
    sub.close()
    assert sub.closed and bus.subscribers("claude-channel") == 0
    bus.close()


def test_a_failing_subscriber_does_not_stop_the_others():
    bus = EventBus()
    good = []

    def bad(event):
        raise RuntimeError("boom")

    bus.subscribe(bad)
    bus.subscribe(good.append)
    bus.publish("recording.started", {})
    bus.publish("recording.stopped", {})
    assert _wait(lambda: len(good) == 2)
    bus.close()


def test_publish_rejects_data_that_is_not_json():
    bus = EventBus()
    with pytest.raises(TypeError):
        bus.publish("recording.started", {"x": object()})
    assert bus.last_seq() == 0


def test_module_publish_never_raises(monkeypatch):
    previous = events.set_bus(EventBus())
    try:
        assert events.publish("recording.started", {"x": object()}) is None
        event = events.publish("recording.started", {"ok": True})
        assert event is not None and event.seq == 1
    finally:
        events.set_bus(previous)


def test_wire_shape():
    bus = EventBus()
    event = bus.publish("history.saved", {"record_id": "7"})
    data = json.loads(event.to_json())
    assert set(data) == {"id", "seq", "type", "time", "v", "data"}
    assert data["v"] == events.SCHEMA_VERSION
    assert data["id"].startswith("evt_") and len(data["id"]) == 28
    assert data["time"].endswith("Z")
    assert data["data"] == {"record_id": "7"}


def test_since_replays_and_reports_gaps():
    bus = EventBus(buffer_size=3)
    for n in range(5):
        bus.publish("recording.started", {"n": n})
    # Buffer holds 3, 4, 5; 1 and 2 were evicted.
    replay, gap = bus.since(3)
    assert [e.seq for e in replay] == [4, 5] and not gap
    replay, gap = bus.since(2)
    assert [e.seq for e in replay] == [3, 4, 5] and not gap
    replay, gap = bus.since(1)
    assert [e.seq for e in replay] == [3, 4, 5] and gap
    replay, gap = bus.since(5)
    assert replay == [] and not gap


def test_since_from_an_earlier_app_session_replays_everything():
    bus = EventBus()
    bus.publish("recording.started", {})
    replay, gap = bus.since(900)
    assert [e.seq for e in replay] == [1] and gap


def test_clear_drops_the_buffer_and_marks_a_gap():
    bus = EventBus()
    bus.publish("recording.started", {})
    bus.publish("recording.stopped", {})
    bus.clear()
    assert bus.recent() == []
    replay, gap = bus.since(1)
    assert replay == [] and gap
    replay, gap = bus.since(2)
    assert replay == [] and not gap


def test_live_only_events_are_delivered_but_never_replayed():
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    bus.publish("recording.started", {})
    bus.publish("transcription.preview", {"text": "hel"}, replay=False)
    bus.publish("recording.stopped", {})
    assert _wait(lambda: len(seen) == 3)
    assert [e.type for e in bus.recent()] == ["recording.started", "recording.stopped"]
    # The skipped number is not a missed event.
    replay, gap = bus.since(0)
    assert not gap and len(replay) == 2
    bus.close()


def test_recent_filters_and_limits():
    bus = EventBus()
    for type_ in ("a", "b", "a", "a"):
        bus.publish(type_, {})
    assert [e.seq for e in bus.recent(2, types=["a"])] == [3, 4]
    assert bus.recent(0) == []


def test_close_stops_delivery():
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    bus.close()
    bus.publish("recording.started", {})
    time.sleep(0.05)
    assert seen == []
    assert bus.subscribe(seen.append).closed


def test_catalog_lists_every_public_type_once():
    assert catalog.EVENTS_GAP not in catalog.PUBLIC_TYPES
    assert len(set(catalog.PUBLIC_TYPES)) == len(catalog.PUBLIC_TYPES)
    assert catalog.known_types(["history.saved", "nope", "history.saved", "events.gap"]) == [
        "history.saved"
    ]


def test_completed_payload_for_a_file_and_for_a_job():
    from services.dictation_pipeline import DictationJob, JobMode

    data = catalog.transcription_completed(None, text="hi", raw_text="hi")
    assert data["mode"] == "file" and data["raw_text"] is None and data["app"] is None

    job = DictationJob(mode=JobMode.ACTION, action_id="a1", source="api", routed=True)
    data = catalog.transcription_completed(
        job, text="Hi.", raw_text="hi",
        fields={"app_id": "notepad.exe", "app_name": "Notepad", "app_category": "docs",
                "language": "en"},
        audio_s=1.23456,
    )
    assert data["job_id"] == job.id and data["mode"] == "action"
    assert data["action_id"] == "a1" and data["source"] == "api" and data["routed"] is True
    assert data["raw_text"] == "hi" and data["language"] == "en"
    assert data["app"] == {"id": "notepad.exe", "name": "Notepad", "category": "docs"}
    assert data["audio_s"] == 1.235
    json.dumps(data)
