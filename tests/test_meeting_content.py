"""Tests for durable meeting content capability derivation."""
from meeting.content import summarize_meeting_content


class _Repo:
    def __init__(self, chunks=None, segments=None):
        self.chunks = list(chunks or [])
        self.segments = list(segments or [])

    def get_audio_chunks(self, meeting_id):
        return list(self.chunks)

    def get_segments(self, meeting_id):
        return list(self.segments)


def test_empty_meeting_has_no_content_capabilities():
    summary = summarize_meeting_content(_Repo(), "m_empty")

    assert summary["is_empty"] is True
    assert summary["has_audio"] is False
    assert summary["has_transcript"] is False
    assert summary["can_rerun_speakers"] is False


def test_speaker_rerun_requires_nonempty_loopback_audio():
    summary = summarize_meeting_content(
        _Repo(
            chunks=[
                {"channel": "mic", "duration_s": 3.0},
                {"channel": "loopback", "duration_s": 4.0},
            ],
            segments=[{"text": "hello"}],
        ),
        "m_recorded",
    )

    assert summary["is_empty"] is False
    assert summary["has_audio"] is True
    assert summary["has_transcript"] is True
    assert summary["can_rerun_speakers"] is True


def test_zero_duration_chunks_and_blank_segments_are_not_content():
    summary = summarize_meeting_content(
        _Repo(
            chunks=[{"channel": "loopback", "duration_s": 0.0}],
            segments=[{"text": "   "}],
        ),
        "m_blank",
    )

    assert summary["is_empty"] is True
    assert summary["audio_chunks"] == 0
    assert summary["transcript_segments"] == 0
