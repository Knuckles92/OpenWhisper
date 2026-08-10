"""Tests for bounded rolling ASR revision helpers and persistence."""
from __future__ import annotations

import os
import sys
import wave

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting.asr.revise import (
    REVISION_WINDOW_S,
    build_initial_prompt,
    interval_iou,
    match_segments,
    revision_window,
    select_chunks_for_window,
    stitch_window_audio,
)
from meeting.interfaces import TranscriptSegment
from meeting.persist.repository import SqlMeetingRepository
from services.database import DatabaseManager


def test_revision_window_caps_at_horizon():
    start, end = revision_window(100.0, window_s=45.0)
    assert start == 55.0
    assert end == 100.0
    start0, end0 = revision_window(10.0, window_s=45.0)
    assert start0 == 0.0
    assert end0 == 10.0


def test_interval_iou_and_match_prefers_overlap():
    assert interval_iou(0, 10, 5, 15) == pytest.approx(5 / 15)
    existing = [
        {
            "id": "sg_a",
            "start_s": 0.0,
            "end_s": 4.0,
            "speaker_pinned": False,
            "speaker_participant_id": "p_me",
            "speaker_source": "channel",
            "chunk_id": 1,
            "channel": "mic",
            "meeting_id": "m1",
            "text": "helo",
        },
        {
            "id": "sg_b",
            "start_s": 4.0,
            "end_s": 8.0,
            "speaker_pinned": True,
            "speaker_participant_id": "p_me",
            "speaker_source": "human",
            "chunk_id": 1,
            "channel": "mic",
            "meeting_id": "m1",
            "text": "world",
        },
    ]
    decoded = [
        TranscriptSegment(
            segment_id="sg_new1",
            meeting_id="m1",
            chunk_id=2,
            channel="mic",
            start_s=0.2,
            end_s=3.8,
            text="hello",
        ),
        TranscriptSegment(
            segment_id="sg_new2",
            meeting_id="m1",
            chunk_id=2,
            channel="mic",
            start_s=8.5,
            end_s=11.0,
            text="again",
        ),
    ]
    plan = match_segments(existing, decoded)
    by_id = {seg.segment_id: seg for seg in plan.upserts}
    assert "sg_a" in by_id
    assert by_id["sg_a"].text == "hello"
    assert by_id["sg_a"].speaker_participant_id == "p_me"
    # Pinned unmatched old is kept (not deleted).
    assert "sg_b" not in plan.remove_ids
    # New unmatched insert kept with its new id.
    assert any(seg.segment_id == "sg_new2" for seg in plan.upserts)


def test_select_chunks_and_stitch(tmp_path):
    def write_wav(name, duration_s, amp=1000):
        path = tmp_path / name
        frames = np.full(int(duration_s * 16000), amp, dtype=np.int16)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(frames.tobytes())
        return str(path)

    chunks = [
        {
            "id": 1,
            "channel": "mic",
            "seq": 0,
            "start_s": 0.0,
            "duration_s": 5.0,
            "file_path": write_wav("a.wav", 5.0),
            "asr_status": "done",
        },
        {
            "id": 2,
            "channel": "mic",
            "seq": 1,
            "start_s": 5.0,
            "duration_s": 5.0,
            "file_path": write_wav("b.wav", 5.0),
            "asr_status": "done",
        },
        {
            "id": 3,
            "channel": "loopback",
            "seq": 0,
            "start_s": 0.0,
            "duration_s": 5.0,
            "file_path": write_wav("c.wav", 5.0),
            "asr_status": "done",
        },
    ]
    selected = select_chunks_for_window(chunks, "mic", 3.0, 8.0)
    assert [c["id"] for c in selected] == [1, 2]
    audio, start = stitch_window_audio(selected, 3.0, 8.0)
    assert start == pytest.approx(3.0)
    assert audio.size == pytest.approx(5.0 * 16000, rel=0.01)


def test_build_initial_prompt_truncates():
    segs = [{"text": "alpha"}, {"text": "beta " * 80}]
    prompt = build_initial_prompt(segs)
    assert len(prompt) <= 224
    assert "beta" in prompt


def test_revise_segments_in_range_persists(tmp_path):
    from datetime import datetime

    db = DatabaseManager(db_path=str(tmp_path / "meet.db"))
    repo = SqlMeetingRepository(db)
    meeting_id = "m_revise"
    repo.create_meeting(
        id=meeting_id,
        title="t",
        status="active",
        started_at=datetime.now().isoformat(),
        host_token="h",
        guest_token="g",
        cloud_enabled=False,
        spool_dir=str(tmp_path / "spool"),
        asr_model="base",
    )
    chunk_id = repo.register_chunk(
        meeting_id=meeting_id,
        channel="mic",
        seq=0,
        file_path=str(tmp_path / "x.wav"),
        start_s=0.0,
        duration_s=10.0,
        sample_rate=16000,
    )
    repo.commit_chunk_transcription(
        meeting_id,
        chunk_id,
        [
            TranscriptSegment(
                segment_id="sg_old",
                meeting_id=meeting_id,
                chunk_id=chunk_id,
                channel="mic",
                start_s=1.0,
                end_s=3.0,
                text="helo",
            )
        ],
    )
    upserts = [
        TranscriptSegment(
            segment_id="sg_old",
            meeting_id=meeting_id,
            chunk_id=chunk_id,
            channel="mic",
            start_s=1.0,
            end_s=3.2,
            text="hello",
        ),
        TranscriptSegment(
            segment_id="sg_new",
            meeting_id=meeting_id,
            chunk_id=chunk_id,
            channel="mic",
            start_s=4.0,
            end_s=6.0,
            text="there",
        ),
    ]
    rows, removed = repo.revise_segments_in_range(
        meeting_id, "mic", 0.0, 10.0, upserts, remove_ids=[]
    )
    assert removed == []
    texts = {r["id"]: r["text"] for r in rows}
    assert texts["sg_old"] == "hello"
    assert texts["sg_new"] == "there"
    assert REVISION_WINDOW_S == 45.0
