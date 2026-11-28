"""Regression tests for core backend audit quick-win fixes."""

from __future__ import annotations

import os
import subprocess
import threading
import wave
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest


def test_app_control_list_models_populates_whisper_models(monkeypatch):
    from services.automation import app_control_ext
    from services import hf_access

    monkeypatch.setattr(hf_access, "is_model_cached", lambda name: name == "base")
    result = app_control_ext.list_models(SimpleNamespace())

    assert result["whisper_models"], "whisper_models should not be silently empty"
    by_id = {m["id"]: m for m in result["whisper_models"]}
    assert "base" in by_id
    assert by_id["base"]["downloaded"] is True


def test_audio_processor_cleanup_temp_files_scoped_to_job(tmp_path):
    from services.audio_processor import AudioProcessor

    proc = AudioProcessor()
    dir_a = tmp_path / "job_a"
    dir_b = tmp_path / "job_b"
    dir_a.mkdir()
    dir_b.mkdir()
    chunk_a = dir_a / "chunk_000.wav"
    chunk_b = dir_b / "chunk_000.wav"
    chunk_a.write_bytes(b"a")
    chunk_b.write_bytes(b"b")
    proc.temp_files = [str(dir_a), str(dir_b)]

    proc.cleanup_temp_files([str(chunk_a)])

    assert not dir_a.exists()
    assert dir_b.exists()
    assert proc.temp_files == [str(dir_b)]

    proc.cleanup_temp_files()
    assert not dir_b.exists()
    assert proc.temp_files == []


def test_speech_process_isolated_uses_devnull_stdout_and_hardened_close(monkeypatch):
    from services.local_asr import process as proc_mod

    popen_kwargs = {}

    class _BadPipe:
        def __init__(self, fail: bool):
            self.fail = fail
            self.closed = False

        def close(self):
            self.closed = True
            if self.fail:
                raise OSError("broken pipe")

        def __iter__(self):
            return iter(())

    class _FakeProc:
        def __init__(self):
            self.stdin = _BadPipe(fail=True)
            self.stdout = None
            self.stderr = _BadPipe(fail=False)
            self.killed = False

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)

    fake_proc = _FakeProc()

    def _fake_popen(cmd, **kwargs):
        popen_kwargs.update(kwargs)
        return fake_proc

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    sp = proc_mod.SpeechProcess("python", isolated=True)
    assert popen_kwargs["stdout"] is subprocess.DEVNULL

    # Must not raise even when wait() times out after kill() and stdin.close() fails.
    sp.close()
    assert fake_proc.killed is True
    assert fake_proc.stdin.closed is True
    assert fake_proc.stderr.closed is True
    assert sp not in proc_mod._workers


def test_recorder_observe_block_single_rms_no_extra_copy_and_no_logging(monkeypatch):
    from services import recorder as rec_mod

    rec = rec_mod.AudioRecorder()
    levels = []
    streamed = []
    rec.set_audio_level_callback(levels.append)
    rec.set_streaming_callback(streamed.append)

    rms_calls = 0
    orig_rms = rec_mod._block_rms_normalized

    def _counted_rms(audio):
        nonlocal rms_calls
        rms_calls += 1
        return orig_rms(audio)

    monkeypatch.setattr(rec_mod, "_block_rms_normalized", _counted_rms)

    block = np.full((1024, 1), 16384, dtype=np.int16)
    rec._observe_block(block, 1024)

    assert rms_calls == 1
    assert len(levels) == 1 and 0.0 < levels[0] <= 1.0
    assert len(streamed) == 1 and streamed[0] is block

    # Exceptions in callbacks must not invoke logger.debug on PortAudio's thread.
    log_calls = []
    monkeypatch.setattr(rec_mod.logger, "debug", lambda *a, **k: log_calls.append(a))
    rec.set_streaming_callback(lambda _b: (_ for _ in ()).throw(RuntimeError("boom")))
    rec.set_audio_level_callback(lambda _l: (_ for _ in ()).throw(RuntimeError("boom")))
    rec._observe_block(block, 1024)
    assert log_calls == []


def test_transcript_cleanup_close_and_context_manager(monkeypatch):
    from services import transcript_cleanup as tc

    fake_client = MagicMock()
    cleaner = tc.TranscriptCleanup(defer_client=True)
    cleaner.client = fake_client

    with cleaner:
        pass

    fake_client.close.assert_called_once()
    assert cleaner.client is None

    # polish_cleanup_rule closes its one-off client.
    created = []

    def _fake_init(self):
        c = MagicMock()
        created.append(c)
        self.client = c

    monkeypatch.setattr(tc.TranscriptCleanup, "_initialize_client", _fake_init)
    monkeypatch.setattr(tc.TranscriptCleanup, "cleanup", lambda self, *a, **k: "Rule.")
    rule, _err = tc.polish_cleanup_rule("spell it right")
    assert rule == "Rule."
    assert len(created) == 1
    created[0].close.assert_called_once()


def test_history_manager_invalidates_usage_and_lazy_singleton_is_threadsafe(tmp_path, monkeypatch):
    from services import history_manager as hm

    folder = tmp_path / "recordings"
    folder.mkdir()
    wav_path = folder / "recording_20261127_120000.wav"
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 160)

    mgr = hm.HistoryManager(recordings_folder=str(folder), max_recordings=None, max_bytes=None)
    assert len(mgr.get_recordings()) == 1
    assert mgr.get_recordings_usage()[0] == 1

    monkeypatch.setattr(hm.db, "clear_history", lambda: None)
    monkeypatch.setattr(hm, "_forget_replayable_events", lambda: None)
    monkeypatch.setattr(hm, "_record_sync", lambda: SimpleNamespace(cleared=lambda _k: None))

    mgr.clear_history_and_recordings()
    assert mgr._usage_cache is None
    assert mgr.get_recordings_usage() == (0, 0)

    # _LazyHistoryManager builds only one instance under concurrent first use.
    built = []
    barrier = threading.Barrier(4)

    class _SlowHM:
        def __init__(self):
            built.append(self)

    monkeypatch.setattr(hm, "HistoryManager", _SlowHM)
    lazy = hm._LazyHistoryManager()

    def _worker():
        barrier.wait()
        lazy._get_instance()

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1


def test_optional_backend_reuses_scratch_file_and_cleans_up():
    from transcriber.optional_backend import LocalSpeechBackend

    backend = LocalSpeechBackend("parakeet", model_name="parakeet-tdt-0.6b-v3")
    seen_paths = []

    class _FakeProcess:
        def request(self, op, *, audio_path, language, timeout, **options):
            seen_paths.append(audio_path)
            assert os.path.isfile(audio_path)
            return {"text": "ok", "segments": []}

        def close(self):
            pass

    backend._process = _FakeProcess()
    audio = np.ones(1600, dtype=np.float32) * 0.1

    backend._request_audio("stream", audio, "en", session="s1", finish=False)
    backend._request_audio("stream", audio, "en", session="s1", finish=True)

    assert len(seen_paths) == 2
    assert seen_paths[0] == seen_paths[1]
    scratch_dir = os.path.dirname(seen_paths[0])
    assert os.path.isdir(scratch_dir)

    backend.cleanup()
    assert not os.path.exists(scratch_dir)
    assert backend._scratch_dir is None


def test_database_v20_indexes_on_fresh_and_migrated_db(tmp_path):
    import sqlite3
    from sqlalchemy import text
    from services.database import SCHEMA_VERSION, DatabaseManager

    expected_indexes = {
        "idx_dictation_stats_entry_id",
        "idx_history_audio_file",
        "idx_history_origin_timestamp",
        "idx_mparticipants_meeting",
        "idx_mquestions_meeting",
    }

    # 1. Fresh DB includes all 5 indexes.
    fresh_path = tmp_path / "fresh.db"
    fresh = DatabaseManager(str(fresh_path))
    try:
        with fresh.engine.connect() as conn:
            names = {
                row[0]
                for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))
            }
            assert expected_indexes.issubset(names)
            ver = conn.execute(text("SELECT version FROM schema_version")).scalar()
            assert ver == SCHEMA_VERSION == 20
    finally:
        fresh.close()

    # 2. v19 DB without those indexes gets them via migration.
    migrated_path = tmp_path / "v19.db"
    seed = DatabaseManager(str(migrated_path))
    seed.close()
    raw = sqlite3.connect(str(migrated_path))
    try:
        for idx in expected_indexes:
            raw.execute(f"DROP INDEX IF EXISTS {idx}")
        raw.execute("UPDATE schema_version SET version = 19")
        raw.commit()
    finally:
        raw.close()

    migrated = DatabaseManager(str(migrated_path))
    try:
        with migrated.engine.connect() as conn:
            names = {
                row[0]
                for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))
            }
            assert expected_indexes.issubset(names)
            ver = conn.execute(text("SELECT version FROM schema_version")).scalar()
            assert ver == 20
    finally:
        migrated.close()


def test_settings_save_repopulates_cache_and_hot_callers_avoid_load_all(tmp_path, monkeypatch):
    from services.settings import SettingsKey, SettingsManager, settings_manager
    from transcriber.optional_backend import LocalSpeechBackend
    from transcriber.openai_backend import OpenAIBackend

    path = tmp_path / "settings.json"
    mgr = SettingsManager(str(path))
    payload = {"auto_paste": True, "nested": ["a"]}
    mgr.save_all_settings(payload)
    # Mutating caller's dict must not corrupt the populated cache.
    payload["nested"].append("mutated")
    assert mgr._cached_settings is not None
    assert mgr.load_settings(("nested",)) == {"nested": ["a"]}

    # Hot callers should use load_settings(), not load_all_settings().
    def _forbid_load_all():
        raise AssertionError("load_all_settings should not be called on this hot path")

    monkeypatch.setattr(settings_manager, "load_all_settings", _forbid_load_all)
    backend = LocalSpeechBackend("parakeet", model_name="parakeet-tdt-0.6b-v3")
    assert isinstance(backend.request_language(), str)

    api_backend = OpenAIBackend("api")
    assert isinstance(api_backend.is_available(), bool)

