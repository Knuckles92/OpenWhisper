"""Failure logging on the meeting path: visible once, quiet on repeats, private.

Covers ``FailureLog`` (warn on the first failure of a run, debug on repeats),
the capture watchdog that uses it, the diarization chunk reload that catches
only what reading a WAV file raises, and handlers that name the exception
type only because the error could quote what was said.
"""
from __future__ import annotations

import logging
import threading
import wave
from types import SimpleNamespace

import pytest

from meeting.capture_session import FailureLog, MeetingCapture
from meeting.interfaces import CHANNEL_LOOPBACK

LOGGER = "meeting.engine"


def _records(caplog, name, level):
    return [r for r in caplog.records if r.name == name and r.levelno == level]


class TestFailureLog:
    def test_first_failure_is_an_error_with_traceback_and_repeats_are_debug(self, caplog):
        log = FailureLog(logging.getLogger("test.failure_log"))
        caplog.set_level(logging.DEBUG, logger="test.failure_log")
        for _ in range(3):
            try:
                raise OSError("disk gone")
            except OSError:
                log.failed("key", "Write failed for %s", "chunk")

        warnings = _records(caplog, "test.failure_log", logging.ERROR)
        debugs = _records(caplog, "test.failure_log", logging.DEBUG)
        assert len(warnings) == 1 and len(debugs) == 2
        assert warnings[0].getMessage().startswith("Write failed for chunk")
        assert warnings[0].exc_info is not None

    def test_recovery_lets_the_next_failure_be_reported_again(self, caplog):
        log = FailureLog(logging.getLogger("test.failure_log"))
        caplog.set_level(logging.DEBUG, logger="test.failure_log")
        for recover in (False, True, False):
            if recover:
                log.recovered("key")
            try:
                raise RuntimeError("x")
            except RuntimeError:
                log.failed("key", "Probe failed")

        assert len(_records(caplog, "test.failure_log", logging.ERROR)) == 2

    def test_type_only_says_where_it_failed_and_drops_the_message(self, caplog):
        log = FailureLog(logging.getLogger("test.failure_log"))
        caplog.set_level(logging.DEBUG, logger="test.failure_log")
        try:
            raise ValueError("what the host said in confidence")
        except ValueError:
            log.failed("key", "Observe failed", type_only=True)

        (record,) = caplog.records
        assert record.levelno == logging.ERROR
        # The type and the frame it was raised in, never what the error said.
        assert "ValueError at test_meeting_failure_logging.py:" in record.getMessage()
        assert "in test_type_only_says_where_it_failed_and_drops_the_message" in record.getMessage()
        assert "confidence" not in record.getMessage()
        assert record.exc_info is None


class TestWatchdogRateLimit:
    def test_a_lasting_probe_failure_warns_once_per_channel(self, caplog, monkeypatch):
        import meeting.capture_session as capture_session

        monkeypatch.setattr(capture_session, "CAPTURE_WATCHDOG_INTERVAL_S", 0.0)
        passes = iter([True, True, True, False])
        capture = MeetingCapture(
            SimpleNamespace(system_audio_policy="auto"), clock=None, repository=None,
            recording=lambda: next(passes), asr=lambda: None, store=lambda: None,
            on_chunk=lambda chunk: None, emit_status=lambda **_: None,
            emit=lambda kind, payload: None,
        )

        def broken(channel):
            raise RuntimeError("device enumeration failed")

        capture._watch_channel = broken
        capture._watchdog_stop = threading.Event()
        caplog.set_level(logging.DEBUG, logger="meeting.capture_session")
        capture._watchdog_loop()

        warnings = _records(caplog, "meeting.capture_session", logging.ERROR)
        debugs = _records(caplog, "meeting.capture_session", logging.DEBUG)
        assert len(warnings) == 2  # one per channel, not one per pass
        assert all("Capture watchdog failed" in r.getMessage() for r in warnings)
        assert all(r.exc_info is not None for r in warnings)
        assert len(debugs) == 4


def _engine():
    from meeting.engine import MeetingEngine, MeetingEngineOptions

    return MeetingEngine(MeetingEngineOptions(), repository=object())


class TestDiarizationChunkReload:
    """``_assign_speakers`` skips a chunk whose WAV cannot be read."""

    @pytest.fixture
    def assigned(self, monkeypatch):
        import meeting.diarize.assign as assign

        calls = []
        monkeypatch.setattr(
            assign, "assign_from_frames",
            lambda diarizer, segments, frames, rate, start_s, on_unlabeled: (
                calls.append((segments[0].chunk_id, rate)) or list(segments)
            ),
        )
        monkeypatch.setattr(assign, "refresh_labels", lambda diarizer, labeled: None)
        return calls

    @staticmethod
    def _wav(path, sample_width=2):
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"\x00" * sample_width * 160)
        return str(path)

    def _run(self, chunk_paths):
        engine = _engine()
        engine._diarizer = object()
        segments = []
        for chunk_id, path in enumerate(chunk_paths, 1):
            engine._chunk_index[chunk_id] = SimpleNamespace(file_path=path, start_s=0.0)
            segments.append(SimpleNamespace(channel=CHANNEL_LOOPBACK, chunk_id=chunk_id))
        engine._assign_speakers(segments)
        return engine

    def test_unreadable_chunks_are_skipped_and_the_rest_still_labeled(
            self, tmp_path, assigned, caplog):
        garbage = tmp_path / "garbage.wav"
        garbage.write_bytes(b"not a wav file at all")
        paths = [
            str(tmp_path / "missing.wav"),                          # OSError
            str(garbage),                                           # wave.Error
            self._wav(tmp_path / "eight_bit.wav", sample_width=1),  # ValueError
            self._wav(tmp_path / "good.wav"),
        ]
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        self._run(paths)

        assert assigned == [(4, 16000)]
        reload_logs = [r for r in caplog.records
                       if "Failed to reload chunk" in r.getMessage()]
        assert [r.levelno for r in reload_logs] == [
            logging.ERROR, logging.DEBUG, logging.DEBUG,
        ]

    def test_a_programming_error_is_not_swallowed_here(self, assigned):
        # A chunk with no path is a bug, not a bad file: it reaches the
        # caller's broad handler (which logs it) instead of vanishing.
        with pytest.raises((TypeError, AttributeError)):
            self._run([None])


class TestTypeOnlyLogging:
    """Handlers near spoken text or keys log the exception type only."""

    def test_term_corrections_read_failure_names_only_the_type(self, caplog):
        engine = _engine()

        def broken(_fn):
            raise ValueError("rename Project Falcon to Osprey")

        engine.store = SimpleNamespace(with_state=broken)
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        assert engine._active_term_rules() == {}
        assert engine._active_term_rules() == {}

        logs = [r for r in caplog.records if "term corrections" in r.getMessage()]
        assert [r.levelno for r in logs] == [logging.ERROR, logging.DEBUG]
        assert all("ValueError" in r.getMessage() for r in logs)
        assert not any("Falcon" in r.getMessage() or r.exc_info for r in logs)

    def test_judge_setup_failure_hides_the_key_and_warns_once(self, caplog, monkeypatch):
        import services.settings as settings
        import services.typesafe as typesafe

        monkeypatch.setattr(settings, "resolve_typesafe_enabled", lambda: True)

        def broken():
            raise ValueError("invalid key sk-live-123")

        monkeypatch.setattr(typesafe, "judge_from_settings", broken)
        engine = _engine()
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        assert engine._typesafe_judge() is None
        assert engine._typesafe_judge() is None

        logs = [r for r in caplog.records if "TypeSafe judge" in r.getMessage()]
        assert [r.levelno for r in logs] == [logging.ERROR, logging.DEBUG]
        assert not any("sk-live" in r.getMessage() or r.exc_info for r in logs)
