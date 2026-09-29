"""Shared non-fixture helpers for tests (import, don't rely on conftest)."""
from __future__ import annotations

import wave
from datetime import datetime

import numpy as np

from meeting.interfaces import TranscriptSegment


def write_wav(path, value=1000, duration_s=0.5, sample_rate=16000):
    """Write a mono 16-bit PCM wav filled with a constant sample value."""
    frames = np.full(int(duration_s * sample_rate), value, dtype=np.int16)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(frames.tobytes())


def make_meeting(repo, meeting_id="m_test1"):
    """Insert a bare active meeting row and return its id."""
    repo.create_meeting(
        id=meeting_id, title="Test meeting", status="active",
        started_at=datetime.now().isoformat(),
        host_token="host-token", guest_token="guest-token",
        cloud_enabled=False, spool_dir="/tmp/spool",
    )
    return meeting_id


def make_segment(meeting_id, seg_id="sg_aaa111", start=1.0, end=3.0,
                 text="hello world", channel="mic"):
    """A transcript segment for ``meeting_id`` (mic channel by default)."""
    return TranscriptSegment(
        segment_id=seg_id, meeting_id=meeting_id, chunk_id=None,
        channel=channel, start_s=start, end_s=end, text=text,
    )
