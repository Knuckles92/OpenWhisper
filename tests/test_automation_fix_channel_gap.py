"""When the event stream says messages were missed, Claude and the log hear about it.

An ``events.gap`` used to be dropped silently, so neither the user nor Claude
knew that dictated words never arrived. The bridge now logs one warning and
sends Claude a short notice with origin "openwhisper"; the same gap seen
again from the same resume point (a reconnect before any new message) is
not announced twice. Only a gap the app can't fill is announced: after a
restart, or when the stream fell behind, the replay brings every message.
"""

import json
import logging

import pytest

from services.automation import catalog
from services.automation import channel_bridge as bridge
from services.automation.channel_bridge import AppLink, EventStream, SseEvent


def gap(last_id, missed_from_seq, reason=catalog.GAP_EVICTED):
    payload = {"id": "", "seq": 0, "type": "events.gap", "time": "2026-10-10T12:00:00.000Z",
               "v": 1, "data": {"missed_from_seq": missed_from_seq, "reason": reason}}
    return SseEvent("events.gap", json.dumps(payload), last_id)


def message(seq, text):
    payload = {"id": f"evt_{seq}", "seq": seq, "type": "channel.message", "v": 1,
               "data": {"job_id": "j", "source": "hotkey", "origin": "dictated", "action_id": "c",
                        "text": text, "app": None}}
    return SseEvent("channel.message", json.dumps(payload), str(seq))


def stream():
    delivered = []
    return EventStream(AppLink(token="t" * 40), delivered.append), delivered


def test_a_gap_reaches_claude_as_a_notice_and_the_log_as_one_warning(caplog):
    events, delivered = stream()
    caplog.set_level(logging.INFO, logger="openwhisper.channel")
    events._handle(message(4, "before"))
    events._handle(gap("4", 5))
    events._handle(message(9, "after"))
    assert [item["content"] for item in delivered][0::2] == ["before", "after"]
    notice = delivered[1]
    assert notice["meta"] == {"origin": bridge.ORIGIN_OPENWHISPER}
    assert "missed" in notice["content"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "missed" in warnings[0].getMessage()


def test_the_same_gap_on_a_reconnect_is_announced_once(caplog):
    events, delivered = stream()
    caplog.set_level(logging.INFO, logger="openwhisper.channel")
    # After an app restart no channel message has moved the resume point,
    # so every reconnect brings the same gap.
    for _ in range(3):
        events._handle(gap("4", 5))
    assert len(delivered) == 1
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    # A later gap from a newer position is news again.
    events._handle(message(12, "hello"))
    events._handle(gap("12", 13))
    assert [item["meta"].get("origin") for item in delivered] == [
        bridge.ORIGIN_OPENWHISPER, "dictated", bridge.ORIGIN_OPENWHISPER,
    ]


@pytest.mark.parametrize("reason", [catalog.GAP_RESTARTED, catalog.GAP_SLOW])
def test_a_gap_the_replay_fills_is_not_announced(caplog, reason):
    events, delivered = stream()
    caplog.set_level(logging.INFO, logger="openwhisper.channel")
    events._handle(message(4, "before"))
    # The app restarted (it replays all it sent since), or the stream fell
    # behind (the reconnect replays the rest): no message is lost.
    events._handle(gap("4", 5, reason))
    events._handle(message(9, "after"))
    assert [item["content"] for item in delivered] == ["before", "after"]
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
