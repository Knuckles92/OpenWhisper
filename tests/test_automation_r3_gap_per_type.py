"""A gap is reported only when an event of a type the client follows was dropped.

The bus tracked dropped events for all types together, so a client following
only ``channel.message`` (the Claude Code channel bridge) was told
``evicted`` after a meeting's segments filled the replay buffer, or after
the user deleted a history entry, though no message was lost; Claude was
then told to ask the user to send it again. Now the bus tracks the newest
dropped event of each type, evicted or cleared, and ``since(seq, types=...)``
says ``evicted`` only when one of the wanted types was dropped after ``seq``.

A clear (history or a meeting deleted) drops every buffered event so its
text can't be replayed; a client that hadn't yet got a cleared event of a
type it follows did miss it, so it still hears of the gap (no text comes
with it). One that had them all, or follows other types, hears nothing.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from services.agent_mcp.app import create_app
from services.automation import api, catalog, events
from services.automation import channel_bridge as bridge
from services.automation.events import EventBus
from services.database import DatabaseManager
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings
from tests.test_automation_fix_mcp_since import TOKEN, recent_events

CHANNEL = [catalog.CHANNEL_MESSAGE]


def channel(bus, text):
    return bus.publish(catalog.CHANNEL_MESSAGE, {
        "job_id": "j", "source": "hotkey", "origin": "dictated", "action_id": "c",
        "text": text, "app": None})


def segments(bus, count):
    return [bus.publish(catalog.MEETING_SEGMENTS, {"meeting_id": "m", "items": []})
            for _ in range(count)]


# -- the bus ---------------------------------------------------------------------


def test_other_types_evicted_are_no_gap_for_a_client_that_doesnt_follow_them():
    bus = EventBus(buffer_size=5)
    seen = channel(bus, "fix the failing test")
    held = segments(bus, 6)[1:]

    assert bus.since(seen.seq, types=CHANNEL) == ([], None)
    assert bus.since(seen.seq, types=[catalog.MEETING_SEGMENTS]) == (held, catalog.GAP_EVICTED)
    assert bus.since(seen.seq) == (held, catalog.GAP_EVICTED)


def test_a_followed_type_evicted_after_the_position_is_a_gap():
    bus = EventBus(buffer_size=5)
    seen = channel(bus, "first")
    channel(bus, "second, never received")
    segments(bus, 5)

    assert bus.since(seen.seq, types=CHANNEL) == ([], catalog.GAP_EVICTED)
    newest = bus.recent(1)[0]
    assert bus.since(newest.seq, types=CHANNEL) == ([], None)


def test_since_zero_is_no_gap_when_only_other_types_were_evicted():
    bus = EventBus(buffer_size=3, seq_base=2_000)
    segments(bus, 5)
    latest = channel(bus, "hello")

    assert bus.since(0, types=CHANNEL) == ([latest], None)
    assert bus.since(0)[1] == catalog.GAP_EVICTED


def test_a_position_from_before_a_restart_still_reads_as_restarted():
    earlier = EventBus(seq_base=1_000)
    position = channel(earlier, "before the restart").seq
    earlier.close()
    bus = EventBus(buffer_size=3, seq_base=2_000)
    segments(bus, 5)
    latest = channel(bus, "after the restart")

    assert bus.since(position, types=CHANNEL) == ([latest], catalog.GAP_RESTARTED)
    assert bus.since(position)[1] == catalog.GAP_EVICTED


def test_a_clear_is_a_gap_only_for_cleared_types_the_client_hadnt_got():
    bus = EventBus()
    seen = channel(bus, "fix the failing test")
    bus.publish(catalog.HISTORY_SAVED, {"job_id": "j", "record_id": "1", "kind": "action"})
    bus.clear()

    assert bus.since(seen.seq, types=CHANNEL) == ([], None)
    assert bus.since(seen.seq, types=[catalog.HISTORY_SAVED]) == ([], catalog.GAP_EVICTED)

    unseen = channel(bus, "a message the client never got")
    bus.clear()
    assert bus.since(seen.seq, types=CHANNEL) == ([], catalog.GAP_EVICTED)
    assert bus.since(unseen.seq, types=CHANNEL) == ([], None)


# -- the live stream and the channel bridge ---------------------------------------


@pytest.fixture
def bus():
    fresh = EventBus(buffer_size=50)
    previous = events.set_bus(fresh)
    yield fresh
    events.set_bus(previous)
    fresh.close()


def reconnect(resume):
    """The bridge reconnecting with Last-Event-ID ``resume``: the stream's
    event names, and what the bridge hands Claude."""
    stream = api.EventStream(types=CHANNEL, replay_from=resume, label="claude-channel",
                             enabled=lambda: False, allowed=lambda: True)

    async def body():
        return [chunk async for chunk in stream.body()]

    raw = b"".join(asyncio.run(body())).decode("ascii")
    delivered = []
    follower = bridge.EventStream(bridge.AppLink(token="t" * 40), delivered.append)
    parser = bridge.SseParser(str(resume))
    for line in raw.split("\n"):
        event = parser.feed(line.rstrip("\r"))
        if event is not None:
            follower._handle(event)
    names = [line[len("event: "):] for line in raw.split("\n") if line.startswith("event: ")]
    return names, delivered


def test_a_meeting_filling_the_buffer_tells_claude_nothing(bus):
    seen = channel(bus, "fix the failing test")
    segments(bus, 60)

    assert reconnect(seen.seq) == ([], [])


def test_deleted_history_tells_claude_nothing_when_no_message_was_missed(bus):
    seen = channel(bus, "fix the failing test")
    bus.publish(catalog.HISTORY_SAVED, {"job_id": "j", "record_id": "1", "kind": "action"})
    bus.clear()

    assert reconnect(seen.seq) == ([], [])


def test_a_message_dropped_while_away_is_still_announced(bus):
    seen = channel(bus, "fix the failing test")
    channel(bus, "and run it again")
    segments(bus, 50)

    names, delivered = reconnect(seen.seq)
    assert names == [catalog.EVENTS_GAP]
    assert [item["meta"] for item in delivered] == [{"origin": bridge.ORIGIN_OPENWHISPER}]


# -- MCP recent_events ------------------------------------------------------------


def test_recent_events_reports_a_gap_only_for_the_types_asked(tmp_path, bus):
    seen = channel(bus, "fix the failing test")
    segments(bus, 60)
    path = tmp_path / "history.db"
    DatabaseManager(str(path)).close()
    app = create_app(path, TOKEN, enabled=lambda: True,
                     settings=InMemorySettings({SettingsKey.MCP_ENABLED: True}))
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        followed = recent_events(client, since_seq=seen.seq, types=CHANNEL)
        everything = recent_events(client, since_seq=seen.seq)

    assert followed["gap"] is False and followed["events"] == []
    assert everything["gap"] is True
