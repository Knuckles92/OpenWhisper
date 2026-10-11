"""Meeting engine events become public meeting.* events, and nothing private leaks."""

import json
from types import SimpleNamespace

import pytest

from services.automation import catalog, events, meeting_events


@pytest.fixture
def bus(monkeypatch):
    fresh = events.EventBus()
    previous = events.set_bus(fresh)
    monkeypatch.setattr(meeting_events, "_announced", type(meeting_events._announced)())
    yield fresh
    events.set_bus(previous)
    fresh.close()


def published(bus):
    return [(e.type, dict(e.data)) for e in bus.recent(500)]


PRIVATE = {"_retry_generation": 3, "_finalization_claim": object()}


def test_the_first_running_status_announces_the_meeting_once(bus):
    meeting_events.forward("server_started", {
        "url": "http://127.0.0.1:9/m?token=guest-secret",
        "host_url": "http://127.0.0.1:9/host?t=host-secret",
        "guest_url": "http://127.0.0.1:9/guest?t=guest-secret",
    }, meeting_id="m_1")
    meeting_events.forward("status", {"status": "starting", **PRIVATE}, meeting_id="m_1")
    meeting_events.forward("status", {"status": "active", "note": "Mic only", **PRIVATE},
                           meeting_id="m_1")
    meeting_events.forward("status", {"status": "paused"}, meeting_id="m_1")
    meeting_events.forward("status", {"status": "active"}, meeting_id="m_1")
    assert published(bus) == [
        (catalog.MEETING_STARTED, {"meeting_id": "m_1", "title": ""}),
    ]
    meeting_events.forward("status", {"status": "active", "title": " Standup "}, meeting_id="m_2")
    assert published(bus)[-1] == (catalog.MEETING_STARTED, {"meeting_id": "m_2", "title": "Standup"})


def test_a_meeting_resumed_after_pause_is_not_announced_again(bus):
    meeting_events.forward("status", {"status": "paused"}, meeting_id="m_1")
    meeting_events.forward("status", {"status": "active"}, meeting_id="m_1")
    assert [t for t, _ in published(bus)] == [catalog.MEETING_STARTED]


def test_ended_carries_the_status(bus):
    meeting_events.forward("ended", {
        "meeting_id": "m_1", "canceled": False, "status": "needs_recovery",
        "unfinished_chunks": 2, "capture_error": "device lost", **PRIVATE,
    })
    meeting_events.forward("ended", {"status": ""}, meeting_id="m_2")
    assert published(bus) == [
        (catalog.MEETING_ENDED, {"meeting_id": "m_1", "status": "needs_recovery"}),
        (catalog.MEETING_ENDED, {"meeting_id": "m_2", "status": "ended"}),
    ]


def test_committed_segments_are_published_as_lines(bus):
    rows = [
        {
            "id": "sg_1", "meeting_id": "m_1", "chunk_id": 4, "channel": "mic",
            "start_s": 1.23456, "end_s": 3.5, "text": "Ship it Friday.",
            "original_text": "ship it friday", "speaker_participant_id": "p_me",
            "speaker_source": "mic", "speaker_pinned": False, "created_at": "x",
        },
        {"id": "sg_2", "text": "   ", "start_s": 4, "end_s": 5},
        {"id": "sg_3", "text": "No speaker yet.", "start_s": 6, "end_s": 7.25,
         "speaker_participant_id": None},
        "not a row",
    ]
    meeting_events.forward("segments", {"items": rows, **PRIVATE}, meeting_id="m_1")
    assert published(bus) == [(catalog.MEETING_SEGMENTS, {
        "meeting_id": "m_1",
        "items": [
            {"segment_id": "sg_1", "speaker": "p_me", "text": "Ship it Friday.",
             "start_s": 1.235, "end_s": 3.5},
            {"segment_id": "sg_3", "speaker": "", "text": "No speaker yet.",
             "start_s": 6.0, "end_s": 7.25},
        ],
    })]


def test_a_redecoded_replacement_transcript_and_empty_batches_publish_nothing(bus):
    meeting_events.forward("segments", {
        "items": [{"id": "sg_9", "text": "Re-decoded."}], "removed_ids": ["sg_1"],
    }, meeting_id="m_1")
    meeting_events.forward("segments", {"items": []}, meeting_id="m_1")
    meeting_events.forward("segments", {"items": [{"id": "sg_1", "text": ""}]}, meeting_id="m_1")
    assert published(bus) == []


def test_voice_commands_are_published_by_name(bus):
    meeting_events.forward("voice_command", {"command": "recap", "applied": 1, "rejected": 0},
                           meeting_id="m_1")
    meeting_events.forward("voice_command", {"command": ""}, meeting_id="m_1")
    assert published(bus) == [
        (catalog.MEETING_VOICE_COMMAND, {"meeting_id": "m_1", "command": "recap"}),
    ]


@pytest.mark.parametrize("kind", [
    "server_started", "error", "intelligence", "background_ready", "asr_released",
    "voice_feedback", "retry_status", "unknown",
])
def test_other_engine_events_stay_private(bus, kind):
    meeting_events.forward(kind, {
        "message": "guest link http://x/?t=secret", "online": True,
        "guest_url": "http://x/?t=secret", "phase": "saved",
    }, meeting_id="m_1")
    assert published(bus) == []


def test_nothing_private_reaches_any_event(bus):
    secret = "guest-secret-token"
    meeting_events.forward("server_started", {"guest_url": f"http://x/?t={secret}"}, meeting_id="m")
    meeting_events.forward("status", {
        "status": "active", "capture": {"message": secret}, "finalization": {"message": secret},
        "speech": {"x": secret}, "_retry_generation": 1,
    }, meeting_id="m")
    meeting_events.forward("segments", {"items": [{
        "id": "sg", "text": "hello", "original_text": secret, "channel": secret,
        "_private": secret,
    }]}, meeting_id="m")
    meeting_events.forward("ended", {"status": "ended", "error": secret, "capture_error": secret},
                           meeting_id="m")
    dumped = json.dumps([e.to_dict() for e in bus.recent(500)])
    assert len(bus.recent(500)) == 3
    assert secret not in dumped
    assert "_retry" not in dumped and "_private" not in dumped


@pytest.mark.parametrize("payload", [None, [], "status", {"status": object()}, {"items": 5}])
def test_forward_never_raises(bus, payload):
    for kind in ("status", "segments", "ended", "voice_command"):
        meeting_events.forward(kind, payload, meeting_id="m_1")


def test_forward_never_raises_when_publishing_fails(bus, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("bus gone")

    monkeypatch.setattr(meeting_events, "publish", broken)
    meeting_events.forward("status", {"status": "active"}, meeting_id="m_1")


def test_events_without_a_meeting_are_dropped(bus):
    meeting_events.forward("status", {"status": "active"})
    meeting_events.forward("ended", {"status": "ended"})
    assert published(bus) == []


def test_the_meeting_runtime_seam_forwards_engine_events(bus):
    from services.runtime.meeting import MeetingRuntime

    engine = SimpleNamespace(meeting_id="m_7")
    MeetingRuntime._forward_public_event(engine, "status", {"status": "active"})
    MeetingRuntime._forward_public_event(engine, "segments", {"items": [
        {"id": "sg_1", "meeting_id": "m_7", "text": "Hi.", "start_s": 0.0, "end_s": 1.0},
    ]})
    MeetingRuntime._forward_public_event(engine, "ended", {"meeting_id": "m_7", "status": "ended"})
    assert [t for t, _ in published(bus)] == [
        catalog.MEETING_STARTED, catalog.MEETING_SEGMENTS, catalog.MEETING_ENDED,
    ]
