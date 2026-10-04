"""Offline synthetic service lifecycle gate, also executed by frozen --self-test.

Exercises real audio decoding, Qt queued delivery, streaming worker lifecycle,
and database persistence. Only the speech model is a deterministic fixture.
These numbers measure plumbing, never ASR quality or hardware inference speed.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import json
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import wave


def peak_rss_mb() -> float | None:
    try:
        if sys.platform == "win32":
            class Counters(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
                    (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = ctypes.c_void_p
            query = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
            query.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
            if not query(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return None
            return counters.PeakWorkingSetSize / 1_000_000
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / (1_000_000 if sys.platform == "darwin" else 1000)
    except (OSError, ImportError, AttributeError):
        return None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _join_worker(stream, timeout=2.0):
    worker = stream.worker_thread
    if worker is not None:
        worker.join(timeout)
        _require(not worker.is_alive(), "Synthetic preview worker did not exit before deadline")


@contextmanager
def _isolated_legacy_history(path):
    # DatabaseManager imports legacy JSON on an empty DB. Explicitly isolate
    # this migration too: a diagnostic must never rename a user's history.
    from config import config
    original = config.HISTORY_FILE
    config.HISTORY_FILE = str(path)
    try:
        yield
    finally:
        config.HISTORY_FILE = original


class _FixtureDecoder:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False

    def transcribe(self, samples, **_options):
        _require(len(samples) > 0, "Decoded fixture audio is empty")
        if self.block:
            self.entered.set()
            _require(self.release.wait(2), "Fixture cancel did not release decoder")
        return [SimpleNamespace(text="synthetic fixture")], None


def run_workflow_smoke(*, repeats: int = 5) -> dict:
    if repeats < 1 or repeats > 1000:
        raise ValueError("repeats must be between 1 and 1000")
    started = time.perf_counter()
    import numpy as np
    from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot
    from PyQt6.QtWidgets import QApplication
    from services.audio_processor import AudioProcessor
    from services.database import DatabaseManager
    from services.streaming_transcriber import StreamingTranscriber

    class Delivery(QObject):
        result = pyqtSignal(str, bool)

        def __init__(self):
            super().__init__()
            self.values = []
            self.result.connect(self.receive, Qt.ConnectionType.QueuedConnection)

        @pyqtSlot(str, bool)
        def receive(self, text, final):
            self.values.append((text, final))

    qt_app = QApplication.instance() or QApplication([])
    delivery = Delivery()
    decoder = _FixtureDecoder()
    stream = StreamingTranscriber(SimpleNamespace(model=decoder), chunk_duration_sec=.1, overlap_sec=0)
    ready_s = time.perf_counter() - started
    timings = []
    with tempfile.TemporaryDirectory(prefix="openwhisper-smoke-") as temporary:
        root = Path(temporary)
        audio_path = root / "fixture.wav"
        samples = np.full(16000, 1000, dtype=np.int16)
        with wave.open(str(audio_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(samples.tobytes())
        decoded, rate = AudioProcessor()._load_audio_data(str(audio_path))
        _require(rate == 16000 and len(decoded) == 16000, "Fixture sample rate/duration changed")
        _require(np.allclose(decoded, samples, atol=1), "PCM amplitude changed during decoding")
        try:
            for _ in range(repeats):
                stream.start_streaming(rate, delivery.result.emit)
                stream.feed_audio(decoded.reshape(-1, 1))
                stop = time.perf_counter()
                stream.stop_streaming()
                result = stream.finalize_preview(timeout=2)
                _join_worker(stream)
                timings.append(time.perf_counter() - stop)
                _require(result == "synthetic fixture", "Fixture transcription was lost or duplicated")
            # Keep a decode in flight so cancel proves late text cannot escape.
            delivery.values.clear()
            decoder.block = True
            stream.start_streaming(rate, delivery.result.emit)
            stream.feed_audio(decoded.reshape(-1, 1))
            _require(decoder.entered.wait(2), "Preview worker did not start decoding")
            cancel_started = time.perf_counter()
            stream.cancel_streaming()
            decoder.release.set()
            _join_worker(stream)
            qt_app.processEvents()
            cancel_s = time.perf_counter() - cancel_started
            _require(not stream.preview_text and not delivery.values, "Canceled text escaped through Qt")
            decoder.block = False
            restart_started = time.perf_counter()
            stream.start_streaming(rate, delivery.result.emit)
            stream.feed_audio(decoded.reshape(-1, 1))
            deadline = time.monotonic() + 2
            while not delivery.values and time.monotonic() < deadline:
                qt_app.processEvents()
                time.sleep(.001)
            _require(delivery.values == [("synthetic fixture", True)], "Restart did not deliver exactly one queued Qt result")
            restart_s = time.perf_counter() - restart_started
            with _isolated_legacy_history(root / "absent-legacy.json"):
                database = DatabaseManager(str(root / "history.db"))
                try:
                    database.add_history_entry("synthetic", delivery.values[0][0], "2000-01-01T00:00:00", "fixture")
                finally:
                    database.close()
                database = DatabaseManager(str(root / "history.db"))
                try:
                    _require(database.get_history_entry_by_id("synthetic").text == "synthetic fixture",
                             "History did not survive a database restart")
                finally:
                    database.close()
            checks = _check_data_recovery(root, samples.tobytes())
        finally:
            cleanup_started = time.perf_counter()
            decoder.release.set()
            stream.cleanup()
            _join_worker(stream)
            cleanup_s = time.perf_counter() - cleanup_started
    p95 = float(np.percentile(timings, 95))
    metrics = dict(fixture_ready_s=ready_s, stop_to_result_p50_s=statistics.median(timings),
                   stop_to_result_p95_s=p95, cancel_s=cancel_s, restart_s=restart_s,
                   cleanup_s=cleanup_s, peak_rss_mb=peak_rss_mb())
    report = dict(schema=2, kind="synthetic_service_lifecycle", repeats=repeats,
                  metrics=metrics, checks=checks, passed=True,
                  limitations=["Fixture decoder; no real speech recognition or model startup",
                               "No physical capture device, native inference, VRAM or dropped-audio measurement",
                               "Service/database restart and cleanup; not full application process restart"])
    # Windowed PyInstaller builds intentionally have no stdout.
    if sys.stdout is not None:
        print(json.dumps(report, sort_keys=True), flush=True)
    return report


def _check_data_recovery(root: Path, pcm: bytes) -> dict:
    from services.backup import create_backup, prepare_restore, previous_data_dir
    from services.backup_startup import acquire_startup_data_lease, release_startup_data_lease
    from services.database import DatabaseManager
    from services.recording_journal import RecordingJournal, recover_recordings

    source, target = root / "source-data", root / "restored-data"
    source.mkdir()
    target.mkdir()
    errors = []
    journal = RecordingJournal(source / "recorded_audio.wav", 16000, 1, 2, errors.append)
    try:
        _require(journal.append(pcm), "Recovery fixture could not queue audio")
        _require(journal.finish(), "Recovery fixture writer did not finish")
    finally:
        _require(journal.close(), "Recovery fixture writer did not close")
    _require(not errors, "Recovery fixture reported a storage error")
    recovered = recover_recordings(str(source / "recorded_audio.wav"), str(source / "recordings"))
    _require(len(recovered) == 1, "Interrupted recording was not recovered exactly once")
    with wave.open(recovered[0], "rb") as wav:
        _require(wav.readframes(wav.getnframes()) == pcm, "Recovered recording changed its audio")
    _require(not recover_recordings(str(source / "recorded_audio.wav"), str(source / "recordings")),
             "Recording recovery duplicated an earlier result")
    with _isolated_legacy_history(source / "absent.json"):
        database = DatabaseManager(str(source / "openwhisper.db"))
        try:
            database.add_history_entry("recoverable", "backup fixture", "2000-01-01T00:00:00", "fixture",
                                       audio_file=Path(recovered[0]).name)
            archive = root / "portable.owbackup"
            create_backup(archive, data_dir=source)
        finally:
            database.close()
        original_settings = '{"before_restore": true}'
        (target / "openwhisper_settings.json").write_text(original_settings, encoding="utf-8")
        prepare_restore(archive, data_dir=target)
        try:
            acquire_startup_data_lease(str(target))
            database = DatabaseManager(str(target / "openwhisper.db"))
            try:
                row = database.get_history_entry_by_id("recoverable")
                _require(row is not None and row.text == "backup fixture", "Restored history is missing")
                with wave.open(str(target / "recordings" / row.audio_file), "rb") as wav:
                    _require(wav.readframes(wav.getnframes()) == pcm, "Restored recording differs")
            finally:
                database.close()
            previous = previous_data_dir(target)
            _require(previous is not None and
                     (previous / "openwhisper_settings.json").read_text(encoding="utf-8") == original_settings,
                     "Restore did not retain previous user data")
        finally:
            release_startup_data_lease()
    return {"recording_recovery": True, "backup_restore": True, "previous_data_preserved": True}
