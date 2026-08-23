"""Failure-path tests for meeting audio playback and recoverable deletion."""
from __future__ import annotations

import wave

import numpy as np
import pytest

from meeting.audio_playback import PLAYBACK_RATE, build_playback
from meeting.persist.data_lifecycle import delete_meeting_data
from tests.helpers import write_wav as _write_wav


class LifecycleRepository:
    def __init__(self, meeting, chunks=None, fail_delete=False):
        self.meeting = meeting
        self.chunks = list(chunks or [])
        self.fail_delete = fail_delete

    def get_meeting(self, meeting_id):
        if self.meeting and self.meeting["id"] == meeting_id:
            return dict(self.meeting)
        return None

    def get_audio_chunks(self, meeting_id):
        return [dict(chunk) for chunk in self.chunks]

    def delete_meeting(self, meeting_id):
        if self.fail_delete:
            raise RuntimeError("database unavailable")
        self.meeting = None


def test_playback_mixes_channels_and_preserves_silence(tmp_path):
    spool = tmp_path / "m_audio"
    spool.mkdir()
    mic = spool / "mic.wav"
    loopback = spool / "loopback.wav"
    later = spool / "later.wav"
    _write_wav(mic, 1000)
    _write_wav(loopback, 3000)
    _write_wav(later, 4000)
    chunks = [
        {"id": 1, "file_path": str(mic), "start_s": 0.0,
         "duration_s": 0.25},
        {"id": 2, "file_path": str(loopback), "start_s": 0.0,
         "duration_s": 0.25},
        {"id": 3, "file_path": str(later), "start_s": 1.0,
         "duration_s": 0.25},
    ]
    repo = LifecycleRepository(
        {"id": "m_audio", "spool_dir": str(spool)}, chunks
    )

    output = build_playback(repo, "m_audio")
    with wave.open(output, "rb") as source:
        rendered = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")

    assert np.all(rendered[: PLAYBACK_RATE // 4] == 2000)
    assert np.all(rendered[PLAYBACK_RATE // 4: PLAYBACK_RATE] == 0)
    assert np.all(rendered[PLAYBACK_RATE:] == 4000)
    assert build_playback(repo, "m_audio") == output  # cache hit


def test_playback_rejects_chunk_outside_spool(tmp_path):
    spool = tmp_path / "m_audio"
    spool.mkdir()
    outside = tmp_path / "outside.wav"
    _write_wav(outside, 1)
    repo = LifecycleRepository(
        {"id": "m_audio", "spool_dir": str(spool)},
        [{"id": 1, "file_path": str(outside), "start_s": 0.0,
          "duration_s": 0.25}],
    )
    with pytest.raises(ValueError, match="outside"):
        build_playback(repo, "m_audio")


def test_delete_removes_database_and_spool(tmp_path):
    root = tmp_path / "meetings"
    spool = root / "m_delete"
    spool.mkdir(parents=True)
    (spool / "audio.wav").write_bytes(b"audio")
    repo = LifecycleRepository({"id": "m_delete", "spool_dir": str(spool)})

    assert delete_meeting_data(repo, "m_delete", str(root)) is True
    assert repo.meeting is None
    assert not spool.exists()
    assert list(root.iterdir()) == []


def test_delete_restores_spool_when_database_delete_fails(tmp_path):
    root = tmp_path / "meetings"
    spool = root / "m_delete"
    spool.mkdir(parents=True)
    (spool / "audio.wav").write_bytes(b"audio")
    repo = LifecycleRepository(
        {"id": "m_delete", "spool_dir": str(spool)}, fail_delete=True
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        delete_meeting_data(repo, "m_delete", str(root))
    assert spool.is_dir()
    assert (spool / "audio.wav").read_bytes() == b"audio"


def test_delete_rejects_paths_outside_configured_root(tmp_path):
    root = tmp_path / "meetings"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = LifecycleRepository({"id": "m_delete", "spool_dir": str(outside)})

    with pytest.raises(ValueError, match="outside"):
        delete_meeting_data(repo, "m_delete", str(root))
    assert outside.is_dir()
    assert repo.meeting is not None
