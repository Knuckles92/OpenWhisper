"""Tests for post-meeting OpenAI speaker identification.

Covers overlap mapping, majority-vote resolution, window planning, clip
clamping, MP3 encode, the headless segment-handler fix, and the engine
finalization step. Network is never used.
"""
from datetime import datetime

import numpy as np
import pytest


from meeting.diarize.cloud_audio import (
    CLIP_MAX_S,
    CLIP_MIN_S,
    clip_sample_range,
    encode_mp3,
    estimate_mp3_bytes,
    plan_windows,
)
from meeting.diarize.cloud_pass import (
    map_turns_to_segments,
    overlap_duration,
    resolve_speaker_participants,
    run_cloud_speaker_pass,
    speaker_slug,
)
from meeting.interfaces import TranscriptSegment
from meeting.state.schema import MeetingState
from meeting.state.segment_ops import make_segment_handler
from meeting.state.store import MeetingStateStore


def _seg(seg_id, start, end, *, pinned=False, participant=None, channel="loopback"):
    return {
        "id": seg_id,
        "channel": channel,
        "start_s": start,
        "end_s": end,
        "speaker_pinned": pinned,
        "speaker_participant_id": participant,
        "text": "x",
    }


class TestOverlapMapping:
    def test_max_overlap_wins(self):
        assigned = map_turns_to_segments(
            [_seg("sg_a", 1.0, 3.0)],
            [
                {"speaker": "A", "start": 0.0, "end": 1.4},
                {"speaker": "B", "start": 1.2, "end": 3.5},
            ],
        )
        assert assigned == {"sg_a": "B"}

    def test_tie_goes_to_earlier_turn(self):
        assigned = map_turns_to_segments(
            [_seg("sg_a", 2.0, 4.0)],
            [
                {"speaker": "late", "start": 3.0, "end": 5.0},
                {"speaker": "early", "start": 1.0, "end": 3.0},
            ],
        )
        assert assigned == {"sg_a": "early"}

    def test_gap_keeps_current_label(self):
        assigned = map_turns_to_segments(
            [_seg("sg_a", 0.0, 1.0)],
            [{"speaker": "A", "start": 5.0, "end": 6.0}],
        )
        assert assigned == {}

    def test_partial_overlap_still_assigns(self):
        assigned = map_turns_to_segments(
            [_seg("sg_a", 0.0, 2.0)],
            [{"speaker": "A", "start": 1.5, "end": 3.0}],
        )
        assert assigned == {"sg_a": "A"}

    def test_pinned_segments_are_omitted(self):
        assigned = map_turns_to_segments(
            [_seg("sg_a", 0.0, 2.0, pinned=True, participant="p_human")],
            [{"speaker": "A", "start": 0.0, "end": 2.0}],
        )
        assert assigned == {}

    def test_zero_overlap_helper(self):
        assert overlap_duration(0.0, 1.0, 1.0, 2.0) == 0.0
        assert overlap_duration(0.0, 2.0, 1.0, 3.0) == 1.0


class TestParticipantResolution:
    def test_majority_vote_reuses_local_label(self):
        mapping = resolve_speaker_participants(
            {"speaker_0": ["sg_a", "sg_b", "sg_c"]},
            {
                "sg_a": _seg("sg_a", 0, 1, participant="p_1"),
                "sg_b": _seg("sg_b", 1, 2, participant="p_1"),
                "sg_c": _seg("sg_c", 2, 3, participant="p_2"),
            },
            {},
        )
        assert mapping == {"speaker_0": "p_1"}

    def test_unlabeled_segments_create_new(self):
        mapping = resolve_speaker_participants(
            {"speaker_0": ["sg_a"]},
            {"sg_a": _seg("sg_a", 0, 1)},
            {},
        )
        assert mapping == {"speaker_0": None}

    def test_named_reference_wins(self):
        mapping = resolve_speaker_participants(
            {"Sarah": ["sg_a"], "speaker_1": ["sg_b"]},
            {
                "sg_a": _seg("sg_a", 0, 1, participant="p_old"),
                "sg_b": _seg("sg_b", 1, 2, participant="p_2"),
            },
            {"Sarah": "p_sarah"},
        )
        assert mapping["Sarah"] == "p_sarah"
        assert mapping["speaker_1"] == "p_2"

    def test_two_speakers_voting_same_pid_splits(self):
        mapping = resolve_speaker_participants(
            {
                "speaker_0": ["sg_a", "sg_b"],
                "speaker_1": ["sg_c"],
            },
            {
                "sg_a": _seg("sg_a", 0, 1, participant="p_1"),
                "sg_b": _seg("sg_b", 1, 2, participant="p_1"),
                "sg_c": _seg("sg_c", 2, 3, participant="p_1"),
            },
            {},
        )
        assert mapping["speaker_0"] == "p_1"
        assert mapping["speaker_1"] is None

    def test_speaker_slug_is_short(self):
        assert speaker_slug("Sarah Chen") == "Sarah_Chen"
        assert speaker_slug("!!!") == "speaker"


class TestWindowPlanning:
    def test_short_session_is_one_window(self):
        ranges = plan_windows(16000 * 30, 16000, byte_budget=20 * 1024 * 1024)
        assert ranges == [(0, 16000 * 30)]

    def test_over_budget_splits(self):
        tiny_budget = estimate_mp3_bytes(5.0, 32000)
        ranges = plan_windows(
            16000 * 40, 16000, byte_budget=tiny_budget, bitrate=32000,
            overlap_s=1.0,
        )
        assert len(ranges) > 1
        assert ranges[0][0] == 0
        assert ranges[-1][1] == 16000 * 40
        # Adjacent windows overlap or abut.
        assert ranges[1][0] < ranges[0][1]

    def test_empty_audio(self):
        assert plan_windows(0, 16000) == []


class TestClipClamping:
    def test_short_interval_expands_to_two_seconds(self):
        start, end = clip_sample_range(16000 * 10, 16000, 4.0, 4.4)
        assert (end - start) / 16000 >= CLIP_MIN_S - 1e-6

    def test_long_interval_keeps_first_ten_seconds(self):
        start, end = clip_sample_range(16000 * 60, 16000, 1.0, 40.0)
        assert abs((end - start) / 16000 - CLIP_MAX_S) < 0.05
        assert start == 16000

    def test_clip_stays_inside_file(self):
        start, end = clip_sample_range(8000, 16000, 0.0, 0.1)
        assert start == 0
        assert end == 8000


class TestMp3Encode:
    def test_encodes_mono_int16(self):
        frames = np.zeros(16000, dtype=np.int16)
        payload = encode_mp3(frames, 16000, bitrate=32000)
        assert isinstance(payload, (bytes, bytearray))
        assert len(payload) > 100
        # ID3 tag or MPEG frame sync.
        assert payload[:3] == b"ID3" or payload[0] == 0xFF

    def test_one_minute_stays_under_budget(self):
        frames = np.zeros(16000 * 60, dtype=np.int16)
        payload = encode_mp3(frames, 16000, bitrate=32000)
        assert len(payload) < 20 * 1024 * 1024
        assert estimate_mp3_bytes(60.0, 32000) >= len(payload)


def _seed_meeting(repo, meeting_id="m_spk"):
    repo.create_meeting(
        id=meeting_id, title="Sync", status="ended",
        started_at=datetime.now().isoformat(),
        host_token="h", guest_token="g",
        cloud_enabled=False, spool_dir="/tmp/spool",
        state_json=None, state_seq=0,
    )
    repo.add_segments([
        TranscriptSegment(
            segment_id="sg_lb", meeting_id=meeting_id, chunk_id=None,
            channel="loopback", start_s=0.0, end_s=2.0, text="hello",
        ),
        TranscriptSegment(
            segment_id="sg_pin", meeting_id=meeting_id, chunk_id=None,
            channel="loopback", start_s=2.0, end_s=4.0, text="pinned",
            speaker_participant_id=None, speaker_source="human",
            speaker_pinned=True,
        ),
    ])
    return meeting_id


class TestSegmentHandlerFix:
    def test_reassign_succeeds_on_headless_store(self, repo):
        meeting_id = _seed_meeting(repo)
        store = MeetingStateStore(
            MeetingState(meeting_id=meeting_id),
            repository=repo,
            segment_handler=make_segment_handler(repo, meeting_id),
            segment_exists=lambda sg_id: repo.segment_exists(meeting_id, sg_id),
            segment_pinned=lambda sg_id: bool(
                (repo.get_segment(meeting_id, sg_id) or {}).get("speaker_pinned")
            ),
        )
        created = store.apply("system", "diarizer", [{
            "op": "upsert_participant",
            "display_name": "Speaker 1",
            "kind": "others_cluster",
            "is_provisional": True,
        }])
        assert created[0].ok
        pid = created[0].effect["participant"]["id"]
        results = store.apply("system", "diarizer", [{
            "op": "reassign_segment_speaker",
            "segment_id": "sg_lb",
            "participant_id": pid,
        }])
        assert results[0].ok
        assert results[0].reason is None
        row = repo.get_segment(meeting_id, "sg_lb")
        assert row["speaker_participant_id"] == pid
        assert row["speaker_source"] == "diarizer"

    def test_pinned_segment_is_rejected(self, repo):
        meeting_id = _seed_meeting(repo)
        store = MeetingStateStore(
            MeetingState(meeting_id=meeting_id),
            repository=repo,
            segment_handler=make_segment_handler(repo, meeting_id),
            segment_exists=lambda sg_id: repo.segment_exists(meeting_id, sg_id),
            segment_pinned=lambda sg_id: bool(
                (repo.get_segment(meeting_id, sg_id) or {}).get("speaker_pinned")
            ),
        )
        created = store.apply("system", "diarizer", [{
            "op": "upsert_participant",
            "display_name": "Speaker 1",
            "kind": "others_cluster",
            "is_provisional": True,
        }])
        pid = created[0].effect["participant"]["id"]
        results = store.apply("system", "diarizer", [{
            "op": "reassign_segment_speaker",
            "segment_id": "sg_pin",
            "participant_id": pid,
        }])
        assert results[0].ok is False
        assert results[0].reason == "segment_pinned"


class TestCloudPass:
    def test_injectable_transcribe_relabels(self, repo, monkeypatch):
        meeting_id = _seed_meeting(repo)
        store = MeetingStateStore(
            MeetingState(meeting_id=meeting_id),
            repository=repo,
            segment_handler=make_segment_handler(repo, meeting_id),
            segment_exists=lambda sg_id: repo.segment_exists(meeting_id, sg_id),
            segment_pinned=lambda sg_id: bool(
                (repo.get_segment(meeting_id, sg_id) or {}).get("speaker_pinned")
            ),
        )
        frames = np.zeros(16000 * 4, dtype=np.int16)

        def fake_session(spool_dir, channel, chunks=None):
            return frames, 16000, 0.0

        monkeypatch.setattr(
            "meeting.asr.offline.load_channel_session", fake_session,
        )

        def fake_transcribe(mp3_bytes, **kwargs):
            return [{"speaker": "speaker_0", "start": 0.0, "end": 2.0}]

        result = run_cloud_speaker_pass(
            repo, meeting_id, store, "/tmp/spool",
            api_key="sk-test", transcribe_fn=fake_transcribe,
        )
        assert result["ok"] is True
        assert result["applied"] >= 1
        row = repo.get_segment(meeting_id, "sg_lb")
        assert row["speaker_participant_id"]
        pinned = repo.get_segment(meeting_id, "sg_pin")
        assert pinned["speaker_participant_id"] is None


class TestRespeaker:
    def test_rerun_speakers_uses_headless_store(self, repo, monkeypatch):
        from meeting.respeaker import rerun_speakers

        meeting_id = _seed_meeting(repo)
        frames = np.zeros(16000 * 4, dtype=np.int16)
        monkeypatch.setattr(
            "meeting.asr.offline.load_channel_session",
            lambda spool_dir, channel, chunks=None: (frames, 16000, 0.0),
        )

        result = rerun_speakers(
            repo, meeting_id, api_key="sk-test",
            transcribe_fn=lambda mp3_bytes, **kwargs: [
                {"speaker": "speaker_0", "start": 0.0, "end": 2.0}
            ],
        )
        assert result["ok"] is True
        assert result["applied"] >= 1
        assert result["state"]["meeting_id"] == meeting_id
        row = repo.get_segment(meeting_id, "sg_lb")
        assert row["speaker_participant_id"]

    def test_unknown_meeting_raises(self, repo):
        from meeting.respeaker import rerun_speakers

        with pytest.raises(ValueError, match="unknown meeting"):
            rerun_speakers(repo, "m_missing", api_key="sk-test")
