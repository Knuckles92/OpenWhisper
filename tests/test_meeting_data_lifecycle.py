"""Failure-path tests for meeting audio playback and recoverable deletion."""
from __future__ import annotations

import wave

import numpy as np
import pytest

from meeting.audio_playback import PLAYBACK_RATE, build_playback
from meeting.persist.data_lifecycle import clear_meetings, delete_meeting_data
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


class MultiLifecycleRepository:
    def __init__(self, meetings):
        self.meetings = {meeting["id"]: dict(meeting) for meeting in meetings}

    def get_meeting(self, meeting_id):
        meeting = self.meetings.get(meeting_id)
        return dict(meeting) if meeting else None

    def list_meetings(self):
        return [dict(meeting) for meeting in self.meetings.values()]

    def delete_meeting(self, meeting_id):
        self.meetings.pop(meeting_id, None)


def test_delete_can_keep_the_spool(tmp_path):
    root = tmp_path / "meetings"
    spool = root / "m_keep"
    spool.mkdir(parents=True)
    (spool / "audio.wav").write_bytes(b"audio")
    repo = LifecycleRepository({"id": "m_keep", "spool_dir": str(spool)})

    assert delete_meeting_data(
        repo, "m_keep", str(root), delete_spool=False
    ) is True
    assert repo.meeting is None
    assert spool.is_dir()
    assert (spool / "audio.wav").read_bytes() == b"audio"


def test_clear_meetings_skips_live_rows_and_ids(tmp_path):
    root = tmp_path / "meetings"
    live = root / "m_live"
    paused = root / "m_paused"
    done = root / "m_done"
    skipped = root / "m_skip"
    for folder in (live, paused, done, skipped):
        folder.mkdir(parents=True)
        (folder / "audio.wav").write_bytes(b"audio")
    repo = MultiLifecycleRepository([
        {"id": "m_live", "status": "active", "spool_dir": str(live)},
        {"id": "m_paused", "status": "paused", "spool_dir": str(paused)},
        {"id": "m_done", "status": "ended", "spool_dir": str(done)},
        {"id": "m_skip", "status": "ended", "spool_dir": str(skipped)},
    ])

    removed = clear_meetings(
        repo, str(root), delete_spools=True, skip_ids={"m_skip"}
    )

    assert removed == 1
    assert set(repo.meetings) == {"m_live", "m_paused", "m_skip"}
    assert live.is_dir()
    assert paused.is_dir()
    assert skipped.is_dir()
    assert not done.exists()


def test_clear_meetings_keeps_spools_when_requested(tmp_path):
    root = tmp_path / "meetings"
    spool = root / "m_done"
    spool.mkdir(parents=True)
    (spool / "audio.wav").write_bytes(b"audio")
    repo = MultiLifecycleRepository([
        {"id": "m_done", "status": "ended", "spool_dir": str(spool)},
    ])

    removed = clear_meetings(repo, str(root), delete_spools=False)

    assert removed == 1
    assert repo.meetings == {}
    assert spool.is_dir()
    assert (spool / "audio.wav").read_bytes() == b"audio"


def test_clear_meetings_purges_orphan_spools_but_not_tombstones(tmp_path):
    root = tmp_path / "meetings"
    orphan = root / "m_orphan"
    tombstone = root / ".deleting-m_old-abc"
    leftover = root / "notes.txt"
    orphan.mkdir(parents=True)
    tombstone.mkdir(parents=True)
    (orphan / "audio.wav").write_bytes(b"audio")
    leftover.write_text("keep files", encoding="utf-8")
    repo = MultiLifecycleRepository([])

    removed = clear_meetings(repo, str(root), delete_spools=True)

    assert removed == 0
    assert not orphan.exists()
    assert tombstone.is_dir()
    assert leftover.is_file()
