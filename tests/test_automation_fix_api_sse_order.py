"""An event stream never sends an event at or before its position, nor out of order.

The bus hands events to subscribers on its dispatch thread, which can lag
behind publishing (another subscriber is slow). A stream that subscribes
while events it already replayed, or that come before its starting point,
are still waiting there used to send them again, after newer ones.
"""

import asyncio
import json
import threading
import time

import pytest

from services.automation import api, catalog, events


@pytest.fixture
def bus():
    fresh = events.EventBus()
    previous = events.set_bus(fresh)
    yield fresh
    events.set_bus(previous)
    fresh.close()


def stalled_dispatch(bus):
    """A subscriber that holds the dispatch thread on the first event until released."""
    release = threading.Event()
    holding = threading.Event()

    def slow(event):
        if not holding.is_set():
            holding.set()
            release.wait(5)

    subscription = bus.subscribe(slow, label="slow")
    return subscription, holding, release


def run_stream(replay_from, *, after_subscribe, more=lambda: None, seconds=0.6):
    """Open a stream at ``replay_from``; returns the (id, type) of each message it sent."""

    async def scenario():
        stream = api.EventStream(
            types=None, replay_from=replay_from, label=api.SSE_LABEL,
            enabled=lambda: True, allowed=lambda: True,
        )
        body = stream.body()
        sent = [await body.__anext__()]  # "retry:": the stream has subscribed
        after_subscribe()
        more()

        async def collect():
            async for chunk in body:
                sent.append(chunk)

        try:
            await asyncio.wait_for(collect(), seconds)
        except asyncio.TimeoutError:
            pass
        await body.aclose()
        return sent[1:]

    messages = []
    for chunk in asyncio.run(scenario()):
        fields = dict(line.split(": ", 1) for line in chunk.decode("ascii").splitlines() if ": " in line)
        if "event" in fields:
            messages.append((fields.get("id"), fields["event"], json.loads(fields["data"])))
    return messages


def test_events_the_client_already_has_are_not_sent_again(bus):
    _, holding, release = stalled_dispatch(bus)
    published = [bus.publish(catalog.RECORDING_STARTED, {"job_id": str(n)}) for n in range(3)]
    assert holding.wait(5)  # the dispatch thread is stuck on the first; the rest wait
    newer = []
    sent = run_stream(
        published[-1].seq,  # the client resumes with everything up to the third
        after_subscribe=release.set,
        more=lambda: newer.append(bus.publish(catalog.RECORDING_STOPPED, {"job_id": "x"})),
    )
    assert [message[0] for message in sent] == [str(newer[0].seq)]


def test_a_stream_without_a_position_starts_after_what_was_already_published(bus):
    _, holding, release = stalled_dispatch(bus)
    for n in range(3):
        bus.publish(catalog.RECORDING_STARTED, {"job_id": str(n)})
    assert holding.wait(5)
    newer = []
    # What GET /v1/events without Last-Event-ID or since does: start at last_seq.
    sent = run_stream(
        bus.last_seq(),
        after_subscribe=release.set,
        more=lambda: newer.append(bus.publish(catalog.HISTORY_SAVED, {"job_id": "x"})),
    )
    assert [message[0] for message in sent] == [str(newer[0].seq)]


def test_a_preview_older_than_the_replay_is_not_sent_after_it(bus):
    _, holding, release = stalled_dispatch(bus)
    started = bus.publish(catalog.RECORDING_STARTED, {"job_id": "p"})
    assert holding.wait(5)
    # Live preview text is never kept for replay.
    bus.publish(catalog.TRANSCRIPTION_PREVIEW, {"job_id": "p", "text": "hel", "final": False},
                replay=False)
    stopped = bus.publish(catalog.RECORDING_STOPPED, {"job_id": "p", "duration_s": 1.0})
    newer = []
    sent = run_stream(
        0,
        after_subscribe=release.set,
        more=lambda: newer.append(bus.publish(catalog.HISTORY_SAVED, {"job_id": "p"})),
    )
    ids = [int(message[0]) for message in sent]
    assert ids == [started.seq, stopped.seq, newer[0].seq]
    assert ids == sorted(ids)


def test_live_events_after_the_replay_still_arrive_in_order(bus):
    first = bus.publish(catalog.RECORDING_STARTED, {"job_id": "a"})
    newer = []

    def publish_more():
        newer.extend(bus.publish(catalog.RECORDING_CANCELED, {"job_id": str(n)}) for n in range(3))

    sent = run_stream(0, after_subscribe=lambda: None, more=publish_more)
    assert [int(message[0]) for message in sent] == [first.seq, *(e.seq for e in newer)]


@pytest.fixture
def restarted():
    """A new app session: the process-wide bus numbers past every old position."""
    fresh = events.EventBus(seq_base=100)
    previous = events.set_bus(fresh)
    yield fresh
    events.set_bus(previous)
    fresh.close()


def test_a_position_from_a_previous_session_gets_a_gap_then_everything(restarted):
    new_session = [restarted.publish(catalog.RECORDING_STARTED, {"job_id": f"new{n}"}) for n in range(2)]
    sent = run_stream(3, after_subscribe=lambda: None)
    gap, *replayed = sent
    assert gap[0] is None and gap[1] == catalog.EVENTS_GAP
    assert gap[2]["data"] == {"missed_from_seq": 4, "reason": catalog.GAP_RESTARTED}
    assert [message[0] for message in replayed] == [str(e.seq) for e in new_session]


def test_eighteen_digit_positions_are_accepted():
    assert api.parse_last_event_id("9" * 18) == 10 ** 18 - 1
    with pytest.raises(api._Invalid):
        api.parse_last_event_id("1" * 19)


def test_a_stream_from_a_large_position_still_delivers_live_events(bus):
    """A position past this session's numbering is from another session."""
    bus.publish(catalog.RECORDING_STARTED, {"job_id": "a"})
    newer = []
    sent = run_stream(
        10 ** 17, after_subscribe=lambda: None,
        more=lambda: newer.append(bus.publish(catalog.RECORDING_STOPPED, {"job_id": "a"})),
    )
    assert sent[-1][0] == str(newer[0].seq)


def test_streams_release_their_subscription(bus):
    run_stream(0, after_subscribe=lambda: None, seconds=0.1)
    deadline = time.monotonic() + 2
    while bus.subscribers(api.SSE_LABEL) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert bus.subscribers(api.SSE_LABEL) == 0
