"""Failure injection for durable capture; no microphone or personal data."""
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock
import wave

import numpy as np

from services.recording_journal import RecordingJournal, recover_recordings, recovery_directory
from services.recorder import AudioRecorder


def test_active_journal_is_skipped_and_recovery_is_idempotent(tmp_path):
    output = str(tmp_path / 'recorded.wav')
    saved = str(tmp_path / 'recordings')
    journal = RecordingJournal(output, 16000, 1, 2, lambda message: None)
    pcm = np.arange(4096, dtype=np.int16).tobytes()
    journal.append(pcm)
    assert journal.finish()
    assert recover_recordings(output, saved) == []
    assert journal.close()
    recovered = recover_recordings(output, saved)
    assert len(recovered) == 1
    with wave.open(recovered[0]) as handle:
        assert handle.readframes(4096) == pcm
    assert recover_recordings(output, saved) == []


def test_recovery_after_process_is_killed(tmp_path):
    output = str(tmp_path / 'capture.wav')
    code = '''
import sys, time
from services.recording_journal import RecordingJournal
j = RecordingJournal(sys.argv[1], 16000, 1, 2, print)
j.append(b'\\x10\\x00' * 16000)
j._queue.join()
print('ready', flush=True)
time.sleep(60)
'''
    child = subprocess.Popen([sys.executable, '-u', '-c', code, output],
                             cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        # read readiness with a deadline, so a child import failure cannot hang CI
        ready = threading.Event()
        lines = []
        def read():
            lines.append(child.stdout.readline())
            ready.set()
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        assert ready.wait(10) and lines == ['ready\n']
        child.kill()
        child.wait(timeout=5)
        recovered = recover_recordings(output, str(tmp_path / 'saved'))
        assert len(recovered) == 1
        with wave.open(recovered[0]) as audio:
            assert audio.getnframes() == 16000
            assert audio.readframes(16000) == b'\x10\x00' * 16000
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_disk_full_stops_recording_and_reports_once(tmp_path, monkeypatch):
    monkeypatch.setattr('services.recorder.sd.InputStream', MagicMock())
    recorder = AudioRecorder(output_file=str(tmp_path / 'capture.wav'))
    errors = []
    failed = threading.Event()
    recorder.error_callback = lambda message: (errors.append(message), failed.set())
    assert recorder.start_recording()
    journal = recorder._audio_spool
    original = journal._file
    class FullDisk:
        def write(self, payload):
            raise OSError(28, 'No space left on device')
        def close(self):
            original.close()
    journal._file = FullDisk()
    try:
        recorder._audio_callback(np.ones((1024, 1), dtype=np.int16), 1024, None, None)
        assert failed.wait(2)
        assert recorder.wait_for_stop_completion(2)
        assert not recorder.is_recording
        assert len(errors) == 1 and 'saved' in errors[0]
        assert recorder.last_capture_error
    finally:
        recorder.cleanup()


def test_slow_disk_never_blocks_enqueue_and_overflow_is_reported(tmp_path):
    errors = []
    journal = RecordingJournal(str(tmp_path / 'capture.wav'), 16000, 1, 2,
                               errors.append, queue_blocks=2)
    original = journal._file
    entered, release = threading.Event(), threading.Event()
    class SlowDisk:
        def write(self, payload):
            entered.set()
            assert release.wait(3)
            return original.write(payload)
        def fileno(self):
            return original.fileno()
        def close(self):
            original.close()
    journal._file = SlowDisk()
    try:
        assert journal.append(b'\0\0' * 1024)
        assert entered.wait(1)
        assert journal.append(b'\0\0' * 1024)
        assert journal.append(b'\0\0' * 1024)
        start = time.monotonic()
        assert not journal.append(b'\0\0' * 1024)
        assert time.monotonic() - start < 0.1
        assert journal.dropped_frames == 1024
        release.set()
        assert journal.finish()
        assert len(errors) == 1
    finally:
        release.set()
        journal.close()


def test_explicit_cancel_removes_journal(tmp_path):
    output = str(tmp_path / 'capture.wav')
    journal = RecordingJournal(output, 16000, 1, 2, lambda message: None)
    journal.append(b'\0\0' * 2048)
    assert journal.close(discard=True)
    assert list(recovery_directory(output).iterdir()) == []


def test_device_disconnect_is_reported_without_a_stop_press(tmp_path, monkeypatch):
    stream = MagicMock()
    stream.active = True
    monkeypatch.setattr('services.recorder.sd.InputStream', lambda **kwargs: stream)
    recorder = AudioRecorder(output_file=str(tmp_path / 'capture.wav'))
    error = threading.Event()
    recorder.error_callback = lambda message: error.set()
    try:
        assert recorder.start_recording()
        stream.active = False
        assert error.wait(2)
        assert recorder.wait_for_stop_completion(2)
        assert not recorder.is_recording
    finally:
        recorder.cleanup()


def test_start_keeps_previous_output_until_new_recording_is_saved(tmp_path, monkeypatch):
    output = tmp_path / 'capture.wav'
    output.write_bytes(b'previous unsaved audio')
    monkeypatch.setattr('services.recorder.sd.InputStream', MagicMock())
    recorder = AudioRecorder(output_file=str(output))
    try:
        assert recorder.start_recording()
        assert output.read_bytes() == b'previous unsaved audio'
    finally:
        recorder.cleanup()


def test_partial_write_keeps_recoverable_prefix(tmp_path):
    output = str(tmp_path / 'capture.wav')
    journal = RecordingJournal(output, 16000, 1, 2, lambda message: None)
    original = journal._file
    class ShortWrite:
        def write(self, data):
            return original.write(data[:4])
        def close(self):
            original.close()
    journal._file = ShortWrite()
    journal.append(b'\x12\x00' * 8)
    assert journal.finish()
    assert journal.written_bytes == 4
    assert journal.close()
    recovered = recover_recordings(output, str(tmp_path / 'saved'))
    assert len(recovered) == 1
    with wave.open(recovered[0]) as audio:
        assert audio.readframes(100) == b'\x12\x00' * 2


def test_canceled_timed_out_writer_removes_audio_when_it_unblocks(tmp_path):
    output = str(tmp_path / 'capture.wav')
    journal = RecordingJournal(output, 16000, 1, 2, lambda message: None)
    original = journal._file
    entered, release = threading.Event(), threading.Event()
    class SlowDisk:
        def write(self, data):
            entered.set()
            assert release.wait(3)
            return original.write(data)
        def close(self):
            original.close()
    journal._file = SlowDisk()
    finish = journal.finish
    journal.finish = lambda: finish(timeout=0.01)
    try:
        journal.append(b'\x12\x00' * 8)
        assert entered.wait(1)
        assert not journal.close(discard=True)
        assert (journal.directory / 'discarded').exists()
        release.set()
        journal._thread.join(2)
        assert not journal.directory.exists()
        assert recover_recordings(output, str(tmp_path / 'saved')) == []
    finally:
        release.set()
        journal._thread.join(3)


def test_recovery_never_restores_canceled_session(tmp_path):
    output = str(tmp_path / 'capture.wav')
    journal = RecordingJournal(output, 16000, 1, 2, lambda message: None)
    journal.append(b'\x12\x00' * 8)
    assert journal.close()
    (journal.directory / 'discarded').touch()
    assert recover_recordings(output, str(tmp_path / 'saved')) == []
    assert not journal.directory.exists()


def test_old_writer_error_cannot_stop_next_recording(tmp_path, monkeypatch):
    monkeypatch.setattr('services.recorder.sd.InputStream', MagicMock())
    recorder = AudioRecorder(output_file=str(tmp_path / 'capture.wav'))
    try:
        assert recorder.start_recording()
        old_error = recorder._audio_spool._on_error
        recorder.cancel_recording()
        assert recorder.wait_for_stop_completion(2)
        assert recorder.start_recording()
        old_error('a previous disk write finally failed')
        assert recorder.last_capture_error is None
        assert recorder.is_recording
    finally:
        recorder.cleanup()


def test_cancel_returns_while_cancellation_checkpoint_is_stalled(tmp_path, monkeypatch):
    monkeypatch.setattr('services.recorder.sd.InputStream', MagicMock())
    recorder = AudioRecorder(output_file=str(tmp_path / 'capture.wav'))
    entered, release = threading.Event(), threading.Event()
    try:
        assert recorder.start_recording()
        journal = recorder._audio_spool
        # Stall disposal itself: even a canceled journal's marker fsync can
        # block on slow storage. Qt's cancel handler must not wait on it.
        close = journal.close
        def slow_close(**kwargs):
            entered.set()
            assert release.wait(3)
            return close(**kwargs)
        monkeypatch.setattr(journal, 'close', slow_close)
        started = time.monotonic()
        recorder.cancel_recording()
        assert time.monotonic() - started < 0.5
        assert entered.wait(1)
        assert recorder.capture_canceled
        assert not recorder.has_recording_data()
    finally:
        release.set()
        recorder.cleanup()
    assert not journal.directory.exists()
