"""Decode a long dictation's completed windows while it is still recorded.

``LocalSpeechBackend.transcribe(path)`` resamples the saved WAV to 16 kHz and
decodes it in ``windows()`` of at most 30 s, each split chosen from the audio
before it alone. A window that is complete while the user is still speaking
is therefore cut identically from the saved file, provided the 16 kHz stream
matches bit for bit, and the engines decode identical input identically. A
``DictationSession`` follows the recorder, runs its PCM through the same
resampler and splitter, and decodes each window as it completes, so the stop
only waits for the last partial window.

The saved file stays the source of truth. At stop the session checks that the
audio it followed is exactly the start of the saved WAV, takes the rest
(post-roll and end padding) from the file itself, and checks the resampled
length against the file's. Any doubt (an engine reload or switch, a cancel, a
meeting, a language change, an error, a mismatch) discards the early text and
the caller decodes the whole file as before, so the transcript is always the
one that path would produce.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import wave
import zlib
from fractions import Fraction
from typing import Optional

import numpy as np

from config import config
from services.local_asr.audio import MAX_SAMPLES, SAMPLE_RATE, WindowSplitter, resampler

logger = logging.getLogger(__name__)


class _Mismatch(Exception):
    """The followed audio cannot be proven to be the saved recording."""


def _same_file(first: str, second: str) -> bool:
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


class DictationSession:
    """One recording's early decode.

    ``poll`` runs on the session's daemon thread until the capture ends or the
    session is stopped; ``finish`` stops and joins that thread before it
    touches the decode state, so the state itself needs no lock. ``discard``
    only raises flags and never waits for a window in flight.
    """

    def __init__(self, controller, backend, recorder, *, poll_sec: Optional[float] = None):
        self._controller = controller
        self.backend = backend
        self.recorder = recorder
        self._poll_sec = (
            config.INCREMENTAL_DICTATION_POLL_SEC if poll_sec is None else poll_sec
        )
        self._generation = backend.generation
        # Both set when the first window's worth of audio has been captured.
        self._language: Optional[str] = None
        self._resampler = None
        self._splitter = WindowSplitter()
        # Recorder bytes already fed to the resampler, and their CRC-32.
        self._consumed = 0
        self._crc = 0
        # Completed windows not yet decoded, and the decoded windows' text.
        self._ready: list[tuple[float, np.ndarray]] = []
        self._texts: list[str] = []
        self._invalid: Optional[str] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="incremental-dictation", daemon=True
        )

    @classmethod
    def create(cls, controller) -> Optional["DictationSession"]:
        """A session for the recording just started, or None where it cannot help."""
        from transcriber.optional_backend import LocalSpeechBackend

        backend = getattr(controller, "current_backend", None)
        recorder = getattr(controller, "recorder", None)
        if not isinstance(backend, LocalSpeechBackend):
            return None
        if backend.backend_id not in config.INCREMENTAL_DICTATION_BACKENDS:
            return None
        if not backend.is_available() or backend.should_cancel:
            return None
        # A native preview (Nemotron) holds a stream session open on the same
        # recognizer for the whole recording. The file path never decodes
        # with one open and nothing has verified that offline decodes
        # interleaved with it match, so such recordings take the file path.
        from services.streaming_transcriber import NativeStreamingTranscriber

        if isinstance(
            getattr(controller, "streaming_transcriber", None), NativeStreamingTranscriber
        ):
            return None
        # The session rebuilds frames from raw PCM; only the recorder's own
        # mono int16 format is verified to resample exactly like the file.
        if getattr(recorder, "channels", None) != 1:
            return None
        if np.dtype(getattr(recorder, "dtype", None)) != np.int16:
            return None
        return cls(controller, backend, recorder)

    @property
    def early_windows(self) -> int:
        """Windows decoded before ``finish``."""
        return len(self._texts)

    def start(self) -> None:
        self._thread.start()

    def discard(self, reason: str = "discarded") -> None:
        """Drop the early text and stop following; never blocks."""
        self._invalidate(reason)
        self._stop.set()

    def _invalidate(self, reason: str) -> None:
        if self._invalid is None:
            self._invalid = reason
            logger.info("Incremental dictation abandoned: %s", reason)

    def _run(self) -> None:
        try:
            while self.poll():
                self._stop.wait(self._poll_sec)
        except Exception as exc:
            self._invalidate(f"{type(exc).__name__}: {exc}")

    def poll(self) -> bool:
        """Take newly captured audio and decode any window it completes.

        Returns whether to keep polling: False once the capture has ended or
        the session was stopped or invalidated.
        """
        # Read first: once capture has ended, the read below gets all of it.
        recording = bool(self.recorder.is_recording)
        if self._stop.is_set() or self._invalid:
            return False
        if self._resampler is None:
            # No window completes before a full window of audio exists, so a
            # short dictation costs one duration check per poll.
            if self.recorder.get_recording_duration() < MAX_SAMPLES / SAMPLE_RATE:
                return recording
            self._resampler = resampler()
            self._language = self.backend.request_language()
        # Also how the thread ends promptly at shutdown, when every engine is
        # cleaned up.
        reason = self._stale()
        if reason:
            self._invalidate(reason)
            return False
        data = self.recorder.read_recorded_bytes(self._consumed)
        if data is None:
            self._invalidate("the capture was cleared")
            return False
        usable = len(data) - len(data) % 2
        if usable:
            self._crc = zlib.crc32(memoryview(data)[:usable], self._crc)
            self._consumed += usable
            self._push(np.frombuffer(data, dtype=np.int16, count=usable // 2))
        self._decode_ready()
        return recording and not self._stop.is_set() and not self._invalid

    def _push(self, samples: np.ndarray) -> None:
        import av

        if not len(samples):
            return
        # Mirror the frames PyAV's WAV decoder hands windows(): timestamps
        # cleared, source rate and time base set.
        frame = av.AudioFrame.from_ndarray(
            samples.reshape(1, -1), format="s16", layout="mono"
        )
        frame.sample_rate = self.recorder.rate
        frame.time_base = Fraction(1, self.recorder.rate)
        frame.pts = None
        self._ready.extend(self._splitter.push(self._resampler.resample(frame)))

    def _decode_ready(self) -> None:
        while self._ready and not self._stop.is_set():
            reason = self._stale()
            if reason:
                self._invalidate(reason)
                return
            offset, audio = self._ready[0]
            started = time.perf_counter()
            try:
                text = self.backend.decode_window(audio, self._language)
            except Exception as exc:
                self._invalidate(f"early decode failed: {exc}")
                return
            # A reload during the decode would mix two workers' text.
            reason = self._stale()
            if reason:
                self._invalidate(reason)
                return
            self._ready.pop(0)
            self._texts.append(text)
            logger.info(
                "Decoded window %d (%.1f-%.1f s) while recording in %.0f ms",
                len(self._texts), offset, offset + len(audio) / SAMPLE_RATE,
                (time.perf_counter() - started) * 1000,
            )

    def _stale(self) -> Optional[str]:
        """Why text decoded now could differ from the file path's, or None."""
        if self._invalid:
            return self._invalid
        backend = self.backend
        if backend.generation != self._generation:
            return "the engine was reloaded, released, or canceled"
        if backend.should_cancel:
            return "the transcription was canceled"
        if getattr(self._controller, "current_backend", None) is not backend:
            return "the speech engine was switched"
        if getattr(self._controller, "recorder", None) is not self.recorder:
            return "the audio device was switched"
        is_meeting_active = getattr(self._controller, "is_meeting_active", None)
        if callable(is_meeting_active) and is_meeting_active():
            return "a meeting started"
        if self._language is not None and backend.request_language() != self._language:
            return "the language setting changed"
        return None

    def finish(self, audio_path: str) -> Optional[str]:
        """The saved recording's transcript, or None to decode the file instead.

        None when nothing was decoded early (a short dictation falls straight
        through) or when the early text cannot be proven to match.
        """
        self._stop.set()
        thread = self._thread
        if thread.is_alive() and thread is not threading.current_thread():
            # Wait out a window in flight, as transcribe() would queue behind
            # it on the decode lock. The job is transcribing by now, so mark
            # the engine busy: a cancel press then tears the worker down and
            # ends that decode instead of only flagging the job.
            self.backend.is_transcribing = True
            try:
                thread.join()
            finally:
                self.backend.is_transcribing = False
        if self._invalid or not self._texts:
            return None
        early = len(self._texts)
        started = time.perf_counter()
        try:
            texts = self.backend.transcribe_windows(
                self._final_windows(audio_path), self._language
            )
            reason = self._stale()
            if reason:
                raise _Mismatch(reason)
        except Exception as exc:
            # Cancels land here too; the file path then raises them itself.
            self._invalidate(str(exc) or type(exc).__name__)
            return None
        logger.info(
            "Incremental dictation: %d window(s) decoded while recording, "
            "%d at stop in %.0f ms",
            early, len(texts), (time.perf_counter() - started) * 1000,
        )
        return self.backend.join_texts(self._texts + texts)

    def _final_windows(self, audio_path: str):
        """Yield the windows left at stop, once the saved file is verified.

        Runs inside ``transcribe_windows``, under the decode lock and with the
        engine marked busy, as ``windows()`` does for a file.
        """
        reason = self._stale()
        if reason:
            raise _Mismatch(reason)
        with wave.open(audio_path, "rb") as saved:
            layout = (saved.getnchannels(), saved.getsampwidth(), saved.getframerate())
            frames = saved.getnframes()
            pcm = saved.readframes(frames)
        if layout != (1, 2, self.recorder.rate):
            raise _Mismatch(f"the saved WAV is {layout}, not the recorder's format")
        if len(pcm) != frames * 2 or len(pcm) < self._consumed:
            raise _Mismatch("the saved WAV is shorter than the audio followed")
        if zlib.crc32(memoryview(pcm)[:self._consumed]) != self._crc:
            raise _Mismatch("the saved WAV does not start with the audio followed")
        # The rest of the recording and the end padding, straight from the file.
        self._push(np.frombuffer(pcm, dtype=np.int16, offset=self._consumed))
        self._ready.extend(self._splitter.push(self._resampler.resample(None)))
        self._ready.extend(self._splitter.finish())
        # Chunk-invariant resampling makes this exact; the swr flush lands
        # within a sample of the ideal count for any input over 1000 frames.
        expected = frames * SAMPLE_RATE / self.recorder.rate
        if abs(self._splitter.total - expected) >= 1:
            raise _Mismatch(
                f"resampled {self._splitter.total} samples where the file "
                f"holds {expected:.1f}"
            )
        ready, self._ready = self._ready, []
        yield from ready


class IncrementalDictation:
    """The transcription runtime's slot for the dictation being recorded."""

    def __init__(self):
        self._lock = threading.Lock()
        self._session: Optional[DictationSession] = None

    def start(self, controller) -> None:
        """Follow the recording that just started, where its engine allows."""
        try:
            session = DictationSession.create(controller)
        except Exception:
            logger.exception("Incremental dictation could not start")
            session = None
        with self._lock:
            previous, self._session = self._session, session
        if previous is not None:
            previous.discard("a new recording started")
        if session is not None:
            session.start()

    def discard(self) -> None:
        """Drop the session of a canceled recording without waiting on it."""
        with self._lock:
            session, self._session = self._session, None
        if session is not None:
            session.discard("the recording was canceled")

    def transcribe(self, backend, audio_path: str) -> str:
        """``backend.transcribe(audio_path)``, reusing this dictation's early windows.

        Only the dictation's own recording can use them; an upload or a
        retranscribe takes the plain path and drops any stale session.
        """
        with self._lock:
            session, self._session = self._session, None
        if session is not None:
            if (
                session.backend is backend
                and _same_file(audio_path, config.RECORDED_AUDIO_FILE)
                and _same_file(audio_path, session.recorder.output_file)
            ):
                text = session.finish(audio_path)
                if text is not None:
                    return text
            else:
                session.discard("the job is not this recording's")
        return backend.transcribe(audio_path)
