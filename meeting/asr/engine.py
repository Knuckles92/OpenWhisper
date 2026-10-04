"""Background ASR engine for Meeting Mode.

``MeetingAsrEngine`` owns a dedicated speech backend (local weights or a paired
remote connection, separate from dictation) and a single daemon worker. Only
a bounded number of chunk references live in memory; the registered WAV and
SQLite row remain durable when that queue is full. Retryable failures use
bounded backoff, and anything still unfinished survives for recovery via
:meth:`MeetingAsrEngine.requeue_pending`.
"""
from __future__ import annotations

import hashlib
import logging
import math
import queue
import threading
import time
import wave
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from meeting.asr.audio import load_wav_int16, prepare_for_whisper
from meeting.asr.hallucination import is_hallucination
from meeting.corrections import correct_text, vocabulary_hint
from meeting.interfaces import SpooledChunk, TranscriptSegment

logger = logging.getLogger(__name__)

#: Total transcription attempts per chunk before giving up.
MAX_ATTEMPTS = 3

#: In-memory chunk references, including the in-flight chunk. The capture
#: spool registers every WAV in SQLite before enqueue; excess work is fetched
#: in bounded pages by the worker instead of blocking audio capture.
MAX_IN_MEMORY_CHUNKS = 64

#: Pauses before local/backend/commit retries. Avoid hot loops when a model or
#: database fails while preserving the existing three-attempt budget.
RETRY_BACKOFF_S = (0.5, 2.0)

#: Disconnected hosts do not spend decode attempts. Retry slowly, but wake
#: immediately when the meeting stops.
REMOTE_RETRY_BACKOFF_S = (5.0, 10.0, 20.0, 40.0, 60.0)
REMOTE_BUSY_MIN_RETRY_S = 0.5

#: Recheck SQLite when a prior refill failed; committed audio remains there.
REFILL_RETRY_S = 5.0

#: Chunks waiting behind the in-flight chunk before live ASR switches to a
#: faster single-beam decode.  This preserves normal quality until the worker
#: is genuinely falling behind, then favors bounded live latency.
FAST_MODE_BACKLOG_CHUNKS = 3

#: Queue waits above this age are operationally significant and logged once
#: per chunk so CPU/model configurations that cannot sustain live cadence are
#: visible instead of silently accumulating minutes of latency.
QUEUE_WAIT_WARNING_S = 15.0

#: Peak int16 amplitude at or below which a chunk is digital silence.  Keep
#: this deliberately conservative: quiet microphones can carry usable speech
#: at low levels, while idle loopback capture commonly produces exact zeros.
DIGITAL_SILENCE_PEAK = 8

#: Number of preceding recognized words supplied to the next draft decode on
#: the same channel. Ten-meeting dogfood reduced strict tcWER on every tested
#: meeting (0.9--4.4 absolute points) with no measurable throughput cost.
DRAFT_PROMPT_WORDS = 50

#: With language set to auto, the offline re-decode reuses the live pass's
#: language only when the meeting was clearly one language: enough confident
#: chunk detections, nearly all agreeing. Bilingual meetings keep auto.
LANGUAGE_VOTE_MIN_PROB = 0.8
LANGUAGE_VOTE_MIN_COUNT = 5
LANGUAGE_VOTE_SHARE = 0.9


class PermanentChunkError(RuntimeError):
    """The saved WAV needs repair or replacement before ASR can retry."""


_STOP = object()


class MeetingAsrEngine:
    """Transcribes spooled meeting chunks on a dedicated speech backend.

    A failed model load never raises out of the constructor: the engine logs
    the failure and sets :attr:`is_available` to False so the meeting can
    proceed (chunks stay ``pending``/``failed`` in the database and remain
    recoverable).
    """

    def __init__(
        self,
        model_name: str,
        meeting_id: str,
        repository: Any,
        language: Optional[str] = None,
        term_rules: Optional[Callable[[], Dict[str, str]]] = None,
        remote: Optional[Dict[str, Any]] = None,
        on_connection_status: Optional[Callable[[str, bool], None]] = None,
        *,
        defer_load: bool = False,
        on_backlog_status: Optional[Callable[[str, bool], None]] = None,
        on_chunk_failure: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Load a dedicated Whisper model for one meeting.

        Args:
            model_name: Whisper model name (``auto`` resolves to turbo on GPU,
                base on CPU inside ``LocalWhisperBackend``).
            meeting_id: Owning meeting session id.
            repository: ``MeetingRepository`` for chunk status bookkeeping.
            language: Optional ISO-639-1 language code. ``None`` keeps
                Whisper's automatic per-decode detection.
            term_rules: Optional callable returning the meeting's live human
                term corrections (misheard term -> replacement). They correct
                the decoder's context prompt and prime it with the right
                spellings so one misheard name stops repeating.
            remote: Non-secret paired host/model snapshot, or None for local ASR.
            on_connection_status: Receives (message, connected) when the remote
                connection changes; capture remains independent of the network.
        """
        self.meeting_id = meeting_id
        self._repository = repository
        self._term_rules = term_rules
        self.language = language.strip().lower() if language else None
        self._backend = None
        self._preview = None
        self.is_available = False
        self.last_error = ""
        self._connection_status = on_connection_status
        self._backlog_status = on_backlog_status
        self._chunk_failure = on_chunk_failure
        self._last_connection_status = None
        self._stop_event = threading.Event()
        self._backend_lock = threading.RLock()
        self._model_name = model_name
        self._remote = remote
        self._defer_load = defer_load

        self._queue: "queue.Queue[SpooledChunk | object]" = queue.Queue(
            maxsize=MAX_IN_MEMORY_CHUNKS)
        self._on_chunk_result: Optional[
            Callable[[SpooledChunk, List[TranscriptSegment]], None]
        ] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        # Task accounting for drain(): counts chunks enqueued but not finished
        # (queued + in-flight). _queued_ids prevents requeue_pending() from
        # double-enqueueing a chunk that is already tracked.
        self._idle_cond = threading.Condition()
        self._outstanding = 0
        self._queued_ids: set = set()
        self._refilling = False
        self._refill_needed = False
        self._next_refill_at = 0.0
        self._deferred_known = False
        self._backlog_overloaded = False
        self._attempts: Dict[int, int] = {}
        self._enqueued_at: Dict[int, float] = {}
        self._queue_wait_warned: set = set()
        self._fast_mode = False
        # A small, per-meeting/channel transcript tail restores linguistic
        # continuity across the short WAV files required for live latency.
        # The key includes meeting_id because the benchmark deliberately
        # reuses one loaded model for several independent meetings.
        self._draft_context: Dict[tuple[str, str], List[str]] = {}
        #: Confident auto-detected languages of chunks that held speech.
        self._language_votes: Dict[str, int] = {}
        if not defer_load:
            self.load_backend()

    def load_backend(self) -> bool:
        """Load on the owning startup worker, with a handle visible to stop().

        Eager construction remains the default for recovery and direct callers.
        A live meeting opts into deferred loading so recording need not wait.
        """
        backend = None
        try:
            from transcriber.local_backend import LocalWhisperBackend
            from services.local_asr.catalog import MODELS

            with self._backend_lock:
                if self._stop_event.is_set():
                    return False
                if self.is_available:
                    return True
                if self._remote is not None:
                    from meeting.asr.remote import MeetingRemoteBackend
                    backend = MeetingRemoteBackend(self._remote)
                elif self._model_name in MODELS:
                    from transcriber.optional_backend import LocalSpeechBackend
                    backend = LocalSpeechBackend(
                        MODELS[self._model_name].backend, model_name=self._model_name)
                elif self._defer_load:
                    backend = LocalWhisperBackend(model_name=self._model_name, load=False)
                else:
                    backend = LocalWhisperBackend(model_name=self._model_name)
                self._backend = backend
            if self._remote is not None or self._model_name in MODELS:
                if self._defer_load:
                    backend.reload_model(cancel_event=self._stop_event)
                else:
                    backend.reload_model()
            elif self._defer_load:
                backend._load_model(cancel_event=self._stop_event)
            with self._backend_lock:
                if self._stop_event.is_set() or self._backend is not backend:
                    return False
                self.is_available = bool(backend.is_available())
                if self.is_available:
                    logger.info("Meeting ASR engine ready: %s", getattr(backend, "name", self._model_name))
                    return True
                self.last_error = getattr(backend, "last_error", "") or "Speech engine unavailable"
                self._backend = None
            logger.error("Meeting ASR model '%s' failed to load (missing=%s); engine unavailable",
                         self._model_name, getattr(backend, "is_model_missing", "?"))
        except Exception as exc:
            if not self._stop_event.is_set():
                self.last_error = str(exc)
                logger.exception("Meeting ASR backend construction failed for model '%s'; engine unavailable",
                                 self._model_name)
            with self._backend_lock:
                if self._backend is backend:
                    self._backend = None
                self.is_available = False
        finally:
            if backend is not None and not self.is_available:
                cleanup = getattr(backend, "cleanup", None)
                if callable(cleanup):
                    try:
                        cleanup()
                    except Exception:
                        logger.exception("Error releasing unavailable meeting ASR backend")
        return False

    def start(
        self,
        on_chunk_result: Callable[[SpooledChunk, List[TranscriptSegment]], None],
    ) -> None:
        """Start the worker thread.

        Args:
            on_chunk_result: Called from the worker thread after transcription,
                including for silent chunks. It must durably store the segments
                and mark the chunk done, or raise so the chunk is retried.
        """
        self._on_chunk_result = on_chunk_result
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping = False
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._worker, name="meeting-asr", daemon=True
        )
        self._thread.start()

    def start_preview(self, callback) -> None:
        from transcriber.optional_backend import LocalSpeechBackend
        from services.local_asr.catalog import MODELS
        if not isinstance(self._backend, LocalSpeechBackend) or self._preview is not None:
            return
        from meeting.asr.preview import MeetingSpeechPreview, WindowSpeechPreview
        model = MODELS.get(self._backend.model_name)
        if model is None:
            return  # Whisper has durable chunks, but no native preview.
        preview_type = (MeetingSpeechPreview if model.streaming else
                        WindowSpeechPreview if model.backend in ("parakeet", "parakeet_mlx") else None)
        if preview_type is not None:
            self._preview = preview_type(
                self._backend, callback, lambda: self._outstanding > 0 or self._stopping,
                self.language or "auto",
            )

    def feed_preview(self, block, start_s: float) -> None:
        if self._preview is not None:
            self._preview.feed(block, start_s)

    def stop_preview(self) -> None:
        preview, self._preview = self._preview, None
        if preview is not None:
            preview.stop()

    def enqueue(self, chunk: SpooledChunk, *, _from_refill: bool = False) -> bool:
        """Queue a registered chunk without blocking capture.

        False means the bounded memory queue is full or stopping. Its SQLite
        row and WAV remain pending for the worker's next bounded refill.
        """
        overloaded = False
        with self._idle_cond:
            if self._stopping or self._stop_event.is_set():
                return False
            if chunk.chunk_id in self._queued_ids:
                return True
            if (len(self._queued_ids) >= MAX_IN_MEMORY_CHUNKS or
                    (self._deferred_known and not _from_refill)):
                self._deferred_known = True
                self._refill_needed = True
                overloaded = True
            else:
                self._queue.put_nowait(chunk)
                self._queued_ids.add(chunk.chunk_id)
                self._enqueued_at.setdefault(chunk.chunk_id, time.monotonic())
                self._outstanding += 1
                self._idle_cond.notify_all()
        if overloaded:
            self._report_backlog(True)
            return False
        return True

    def _report_backlog(self, overloaded: bool) -> None:
        with self._idle_cond:
            if self._backlog_overloaded == overloaded:
                return
            self._backlog_overloaded = overloaded
        message = (
            "Live transcription is behind; audio is saved and captions will catch up."
            if overloaded else "Live transcription caught up."
        )
        if overloaded:
            logger.warning("Meeting ASR in-memory backlog reached %d chunks",
                           MAX_IN_MEMORY_CHUNKS)
        else:
            logger.info("Meeting ASR backlog recovered")
        if self._backlog_status is not None:
            try:
                self._backlog_status(message, overloaded)
            except Exception:
                logger.exception("Could not report meeting ASR backlog status")

    def drain(self, timeout_s: float) -> bool:
        """Block until every enqueued chunk has finished (or given up).

        Args:
            timeout_s: Maximum seconds to wait.

        Returns:
            True when the queue emptied and the worker went idle within the
            timeout, False otherwise.
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            with self._idle_cond:
                while self._outstanding > 0 or self._refilling:
                    if self._stopping or self._stop_event.is_set():
                        return False
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        logger.warning(
                            "ASR drain timed out with %d in-memory chunk(s) outstanding",
                            self._outstanding,
                        )
                        return False
                    self._idle_cond.wait(remaining)
            if self._stopping or self._stop_event.is_set():
                return False
            if self._refill_needed and time.monotonic() >= deadline:
                return False
            # A full in-memory queue may have left registered chunks in
            # SQLite; verify it before claiming the meeting is drained.
            added = self.requeue_pending()
            with self._idle_cond:
                if (added == 0 and self._outstanding == 0 and
                        not self._refilling and not self._refill_needed):
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            with self._idle_cond:
                self._idle_cond.wait(min(REFILL_RETRY_S, remaining))

    def transcribe_offline_session(
        self,
        spool_dir: str,
        chunks: Optional[List[Dict[str, Any]]] = None,
        *,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> List[TranscriptSegment]:
        """Re-decode the continuous session audio with offline silence cuts.

        Uses the already-loaded Whisper model. Does not stop the live worker.

        Args:
            spool_dir: Meeting spool directory containing session WAVs.
            chunks: Optional registered chunk rows for the concat fallback.
            progress_cb: Optional callback for window decoding progress.

        Returns:
            Fresh segments for every channel that has session audio.
        """
        if self._backend is None or not self.is_available:
            return []
        from meeting.asr.offline import transcribe_meeting_sessions

        model = getattr(self._backend, "model", None)
        if model is None:
            return []
        return transcribe_meeting_sessions(
            model,
            spool_dir,
            self.meeting_id,
            chunks,
            language=self.dominant_language(),
            progress_cb=progress_cb,
        )

    def dominant_language(self) -> Optional[str]:
        """The configured language, or the one live chunks clearly agreed on.

        Returns:
            An ISO-639-1 code, or ``None`` to keep per-window detection.
        """
        if self.language:
            return self.language
        votes = dict(self._language_votes)
        total = sum(votes.values())
        if total < LANGUAGE_VOTE_MIN_COUNT:
            return None
        language, count = max(votes.items(), key=lambda item: item[1])
        return language if count >= LANGUAGE_VOTE_SHARE * total else None

    def _record_language(self, info: Any, held_speech: bool) -> None:
        if self.language or not held_speech:
            return
        language = getattr(info, "language", None)
        probability = getattr(info, "language_probability", None)
        if (
            isinstance(language, str) and language
            and isinstance(probability, (int, float))
            and probability >= LANGUAGE_VOTE_MIN_PROB
        ):
            self._language_votes[language] = self._language_votes.get(language, 0) + 1

    def stop(self) -> None:
        """Stop the worker and release the model."""
        self._stopping = True
        self._stop_event.set()
        with self._idle_cond:
            self._idle_cond.notify_all()
        if getattr(self._backend, "is_remote", False):
            # Interrupt network waits before joining either worker.
            self._backend.cancel_transcription()
            self._backend.cleanup()
        self.stop_preview()
        thread = self._thread
        if thread is not None and thread.is_alive():
            try:
                self._queue.put_nowait(_STOP)
            except queue.Full:
                pass  # The active worker sees stop_event after its chunk.
            thread.join(timeout=10.0)
            if thread.is_alive():
                logger.warning("Meeting ASR worker did not stop within 10s")
                # A native decode may still hold the backend. Keep its worker
                # and references intact rather than cleaning them up in use.
                return
        self._thread = None
        self._draft_context.clear()
        with self._idle_cond:
            self._queue = queue.Queue(maxsize=MAX_IN_MEMORY_CHUNKS)
            self._outstanding = 0
            self._queued_ids.clear()
            self._attempts.clear()
            self._enqueued_at.clear()
            self._queue_wait_warned.clear()
            self._refilling = False
            self._refill_needed = False
            self._next_refill_at = 0.0
            self._deferred_known = False
            self._idle_cond.notify_all()

        backend = self._backend
        self._backend = None
        self._preview = None
        self.is_available = False
        if backend is not None:
            try:
                cleanup = getattr(backend, "cleanup", None)
                if callable(cleanup):
                    cleanup()
            except Exception:
                logger.exception("Error releasing meeting ASR model")
            del backend

    def requeue_pending(self, *, rows: Optional[List[Dict[str, Any]]] = None) -> int:
        """Refill at most one bounded page from durable pending rows."""
        with self._idle_cond:
            if self._stopping or self._stop_event.is_set() or self._refilling:
                return 0
            if self._refill_needed and time.monotonic() < self._next_refill_at:
                return 0
            self._refilling = True
            free = max(0, MAX_IN_MEMORY_CHUNKS - len(self._queued_ids))
            excluded = tuple(self._queued_ids)
        requeued = 0
        has_extra = False
        failed = False
        try:
            if rows is None:
                try:
                    rows = self._repository.get_pending_chunks(
                        self.meeting_id, limit=free + 1, exclude_ids=excluded)
                except TypeError:
                    # Older test/custom repositories may not support paging;
                    # the app's SQL repository always does.
                    rows = [row for row in self._repository.get_pending_chunks(self.meeting_id)
                            if row["id"] not in excluded][:free + 1]
            rows = sorted(
                (row for row in rows if row["id"] not in excluded),
                key=lambda row: (float(row.get("start_s") or 0),
                                 int(row.get("seq") or 0),
                                 str(row.get("channel") or ""), int(row["id"])),
            )
            has_extra = len(rows) > free
            for row in rows[:free]:
                chunk = SpooledChunk(
                    chunk_id=row["id"], meeting_id=row["meeting_id"],
                    channel=row["channel"], seq=row["seq"],
                    file_path=row["file_path"], start_s=row["start_s"],
                    duration_s=row["duration_s"], sample_rate=row["sample_rate"],
                )
                with self._idle_cond:
                    if chunk.chunk_id in self._queued_ids:
                        continue
                    self._attempts[chunk.chunk_id] = int(row.get("asr_attempts") or 0)
                if self.enqueue(chunk, _from_refill=True):
                    requeued += 1
                else:
                    has_extra = True
        except Exception:
            failed = True
            has_extra = True
            logger.exception("Could not refill saved ASR chunks for %s", self.meeting_id)
        finally:
            with self._idle_cond:
                self._refilling = False
                self._refill_needed = has_extra
                self._next_refill_at = (time.monotonic() + REFILL_RETRY_S
                                        if failed else 0.0)
                self._deferred_known = has_extra
                quiet = (not has_extra and self._outstanding <=
                         FAST_MODE_BACKLOG_CHUNKS)
                self._idle_cond.notify_all()
        if has_extra:
            self._report_backlog(True)
        elif quiet:
            self._report_backlog(False)
        if requeued:
            logger.info("Queued %d saved ASR chunk(s) for meeting %s",
                        requeued, self.meeting_id)
        return requeued

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=REFILL_RETRY_S)
            except queue.Empty:
                if self._refill_needed:
                    self.requeue_pending()
                continue
            if item is _STOP:
                self._queue.task_done()
                break
            try:
                self._process_chunk(item)
            except Exception:  # defensive: _process_chunk handles its own errors
                logger.exception(
                    "Unexpected ASR worker error on chunk %s", item.chunk_id
                )
                self._set_status(item.chunk_id, "blocked",
                                 "Unexpected ASR worker error; saved audio needs manual retry")
                self._report_chunk_failure(
                    item.chunk_id,
                    "An unexpected transcription error occurred. Audio is saved; retry this meeting.",
                )
            finally:
                with self._idle_cond:
                    self._queued_ids.discard(item.chunk_id)
                    self._attempts.pop(item.chunk_id, None)
                    self._enqueued_at.pop(item.chunk_id, None)
                    self._queue_wait_warned.discard(item.chunk_id)
                # Keep _outstanding nonzero through the DB refill so drain()
                # cannot report success between a dequeue and its saved tail.
                if not self._stop_event.is_set():
                    self.requeue_pending()
                with self._idle_cond:
                    self._outstanding -= 1
                    quiet = (not self._deferred_known and
                             self._outstanding <= FAST_MODE_BACKLOG_CHUNKS)
                    self._idle_cond.notify_all()
                if quiet:
                    self._report_backlog(False)
                self._queue.task_done()
        logger.debug("Meeting ASR worker exited")

    def _process_chunk(self, chunk: SpooledChunk) -> None:
        """Transcribe one durable chunk with classified, paced retries."""
        remote = getattr(self._backend, "is_remote", False)
        attempts = self._attempts.get(chunk.chunk_id, 0)
        remote_failures = 0
        while not self._stopping and not self._stop_event.is_set():
            if remote:
                # A disconnected host never spends the decode budget. The
                # saved chunk stays pending while capture continues to spool.
                from meeting.asr.remote import RemoteMeetingUnavailable
                try:
                    self._backend.ensure_ready()
                    self._report_connection(f"Transcribing on {self._backend.host_name}", True)
                except RemoteMeetingUnavailable as exc:
                    self._set_status(chunk.chunk_id, "pending", str(exc))
                    self._report_connection(
                        f"{exc} Audio is saved locally; retrying the remote connection.", False
                    )
                    delay = self._remote_retry_delay(exc, remote_failures)
                    remote_failures += 1
                    if self._stop_event.wait(delay):
                        return
                    continue
                except Exception as exc:
                    if not getattr(exc, "retryable", False):
                        raise
                    self._set_status(chunk.chunk_id, "pending", str(exc))
                    self._report_connection(
                        f"{exc} Audio is saved locally; waiting for host capacity.",
                        True,
                    )
                    delay = self._remote_retry_delay(exc, remote_failures)
                    remote_failures += 1
                    if self._stop_event.wait(delay):
                        return
                    continue
            if self._backend is None or not self._backend.is_available():
                self._set_status(
                    chunk.chunk_id, "blocked",
                    "Speech engine unavailable; select or install a model, then retry saved audio",
                )
                self._report_chunk_failure(
                    chunk.chunk_id,
                    "Speech engine unavailable. Select or install a model, then retry saved audio.",
                )
                logger.warning("ASR engine unavailable; chunk %s needs manual retry",
                               chunk.chunk_id)
                return

            attempts += 1
            self._attempts[chunk.chunk_id] = attempts
            try:
                self._set_status(chunk.chunk_id, "processing")
                self._log_queue_wait(chunk)
                segments = self._transcribe_chunk(
                    chunk, beam_size=self._beam_size_for_backlog(),
                    initial_prompt=self._draft_prompt(chunk),
                )
                if self._on_chunk_result is None:
                    raise RuntimeError("No durable ASR result callback is registered")
                self._on_chunk_result(chunk, segments)
                # The callback is the durability boundary. A failed commit
                # retries with the same preceding transcript prompt.
                self._remember_draft_segments(chunk, segments)
                return
            except PermanentChunkError as exc:
                logger.error("Saved ASR chunk %s needs repair: %s", chunk.chunk_id, exc)
                self._set_status(chunk.chunk_id, "blocked", str(exc))
                self._report_chunk_failure(chunk.chunk_id, str(exc))
                return
            except Exception as exc:
                disconnected = remote and isinstance(exc, RemoteMeetingUnavailable)
                if disconnected or (remote and getattr(exc, "retryable", False)):
                    self._defer_remote_chunk(chunk.chunk_id, str(exc))
                    attempts -= 1
                    self._attempts[chunk.chunk_id] = attempts
                    self._report_connection(
                        (f"{exc} Audio is saved locally; retrying the remote connection."
                         if disconnected else
                         f"{exc} Audio is saved locally; waiting for host capacity."),
                        not disconnected,
                    )
                    delay = self._remote_retry_delay(exc, remote_failures)
                    remote_failures += 1
                    if self._stop_event.wait(delay):
                        return
                    continue
                logger.exception(
                    "Transcription failed for chunk %s (attempt %d/%d)",
                    chunk.chunk_id, attempts, MAX_ATTEMPTS,
                )
                self._set_status(chunk.chunk_id, "failed", str(exc))
                if attempts >= MAX_ATTEMPTS:
                    logger.error("Giving up on chunk %s after %d attempts; audio remains saved",
                                 chunk.chunk_id, attempts)
                    self._report_chunk_failure(
                        chunk.chunk_id,
                        "Transcription failed after three attempts. Audio is saved; "
                        "check the speech engine and retry this meeting.",
                    )
                    return
                delay = RETRY_BACKOFF_S[min(attempts - 1,
                                            len(RETRY_BACKOFF_S) - 1)]
                if self._stop_event.wait(delay):
                    return

    def _defer_remote_chunk(self, chunk_id: int, error: str) -> None:
        defer = getattr(self._repository, "defer_chunk_after_connection_failure", None)
        if callable(defer):
            try:
                defer(chunk_id, error)
                return
            except Exception:
                logger.exception("Could not refund disconnected ASR attempt for %s", chunk_id)
        self._set_status(chunk_id, "pending", error)

    @staticmethod
    def _remote_retry_delay(error: Exception, failures: int) -> float:
        suggested = getattr(error, "retry_after_s", None)
        if (isinstance(suggested, (int, float)) and not isinstance(suggested, bool)
                and math.isfinite(suggested) and suggested >= 0):
            return min(REMOTE_RETRY_BACKOFF_S[-1],
                       max(REMOTE_BUSY_MIN_RETRY_S, float(suggested)))
        return REMOTE_RETRY_BACKOFF_S[min(failures,
                                          len(REMOTE_RETRY_BACKOFF_S) - 1)]

    def _report_chunk_failure(self, chunk_id: int, reason: str) -> None:
        if self._chunk_failure is None:
            return
        try:
            self._chunk_failure(f"Saved audio chunk {chunk_id}: {reason}")
        except Exception:
            logger.exception("Could not report terminal ASR failure")

    def _report_connection(self, message: str, connected: bool) -> None:
        if (message, connected) == self._last_connection_status:
            return
        self._last_connection_status = (message, connected)
        if self._connection_status is not None:
            try:
                self._connection_status(message, connected)
            except Exception:
                logger.exception("Could not report remote speech status")

    def _beam_size_for_backlog(self) -> int:
        with self._idle_cond:
            queued_behind = max(0, self._outstanding - 1)
        fast_mode = queued_behind >= FAST_MODE_BACKLOG_CHUNKS
        if fast_mode and not self._fast_mode:
            logger.warning(
                "Meeting ASR backlog is %d chunk(s); using fast decode",
                queued_behind,
            )
        elif self._fast_mode and not fast_mode:
            logger.info("Meeting ASR backlog recovered; restoring full decode")
        self._fast_mode = fast_mode
        return 1 if fast_mode else 5

    def _log_queue_wait(self, chunk: SpooledChunk) -> None:
        with self._idle_cond:
            enqueued_at = self._enqueued_at.get(chunk.chunk_id)
            already_warned = chunk.chunk_id in self._queue_wait_warned
            if enqueued_at is None or already_warned:
                return
            wait_s = time.monotonic() - enqueued_at
            if wait_s < QUEUE_WAIT_WARNING_S:
                return
            self._queue_wait_warned.add(chunk.chunk_id)
            queued_behind = max(0, self._outstanding - 1)
        logger.warning(
            "Meeting ASR chunk %s waited %.1fs (queued behind it: %d)",
            chunk.chunk_id,
            wait_s,
            queued_behind,
        )

    @staticmethod
    def _is_digital_silence(frames: np.ndarray) -> bool:
        if frames.size == 0:
            return True
        peak = int(np.max(np.abs(frames.astype(np.int32, copy=False))))
        return peak <= DIGITAL_SILENCE_PEAK

    def _transcribe_chunk(
        self,
        chunk: SpooledChunk,
        beam_size: int = 5,
        initial_prompt: Optional[str] = None,
    ) -> List[TranscriptSegment]:
        """Load, convert, and transcribe one spooled WAV chunk.

        Args:
            chunk: Durable audio chunk to transcribe.
            beam_size: Whisper decode beam size. Backlogged live processing
                uses one beam to recover; normal processing uses five.
            initial_prompt: Optional preceding transcript context. The worker
                supplies its bounded durable per-channel tail; callers may
                leave it unset for a context-free decode.

        Returns:
            Meeting-clock-timestamped segments; empty when the chunk holds no
            speech.
        """
        try:
            frames, sample_rate = load_wav_int16(chunk.file_path)
        except (FileNotFoundError, PermissionError, wave.Error, ValueError) as exc:
            raise PermanentChunkError(
                f"Saved audio cannot be read ({exc}); restore or replace the WAV, then retry"
            ) from exc
        if self._is_digital_silence(frames):
            logger.debug("Skipping digitally silent meeting chunk %s", chunk.chunk_id)
            return []
        try:
            audio = prepare_for_whisper(frames, sample_rate)
        except (ValueError, ZeroDivisionError) as exc:
            raise PermanentChunkError(
                f"Saved audio has invalid sample data ({exc}); repair the WAV, then retry"
            ) from exc
        if audio.size == 0:
            return []

        whisper_segments, info = self._backend.model.transcribe(
            audio,
            beam_size=beam_size,
            vad_filter=True,
            word_timestamps=False,
            language=self.language,
            # Avoid Whisper's unbounded automatic feedback between internal
            # decode windows. Cross-chunk continuity comes only from the
            # explicitly bounded ``initial_prompt`` above.
            condition_on_previous_text=False,
            initial_prompt=initial_prompt or None,
        )

        segments: List[TranscriptSegment] = []
        for ordinal, seg in enumerate(whisper_segments):
            text = (seg.text or "").strip()
            if not text or is_hallucination(seg):
                continue
            segments.append(TranscriptSegment(
                segment_id=self._stable_segment_id(chunk.chunk_id, ordinal),
                meeting_id=self.meeting_id,
                chunk_id=chunk.chunk_id,
                channel=chunk.channel,
                start_s=chunk.start_s + float(seg.start),
                end_s=chunk.start_s + float(seg.end),
                text=text,
            ))
        self._record_language(info, bool(segments))
        return segments

    def _draft_prompt(self, chunk: SpooledChunk) -> Optional[str]:
        """Return bounded durable transcript context preceding ``chunk``.

        On recovery, the cache is hydrated only from rows ending before this
        chunk. This avoids leaking later transcript text into an earlier hole.
        Normal live processing then advances the in-memory tail after each
        successful durable callback.

        Args:
            chunk: Chunk about to be decoded.

        Returns:
            Up to :data:`DRAFT_PROMPT_WORDS` preceding words, or ``None``.
        """
        key = (chunk.meeting_id, chunk.channel)
        if key not in self._draft_context:
            words: List[str] = []
            get_segments = getattr(self._repository, "get_segments", None)
            if callable(get_segments):
                try:
                    rows = get_segments(chunk.meeting_id, after_start_s=-1.0)
                    for row in rows:
                        if (
                            row.get("channel") == chunk.channel
                            and float(row.get("end_s") or 0.0)
                            <= chunk.start_s + 1e-6
                        ):
                            words.extend(str(row.get("text") or "").split())
                except Exception:
                    logger.exception("Could not hydrate meeting ASR draft context")
            self._draft_context[key] = words[-DRAFT_PROMPT_WORDS:]
        rules = self._current_term_rules()
        context = " ".join(self._draft_context[key][-DRAFT_PROMPT_WORDS:]).strip()
        # Apply corrections at prompt time, not when words are remembered, so
        # a correction offered mid-meeting also fixes context already cached.
        prompt = " ".join(
            part for part in (vocabulary_hint(rules), correct_text(context, rules))
            if part
        ).strip()
        return prompt or None

    def _current_term_rules(self) -> Dict[str, str]:
        """Live human term corrections, or ``{}`` when unavailable."""
        if self._term_rules is None:
            return {}
        try:
            rules = self._term_rules()
        except Exception:
            logger.exception("Could not read meeting term corrections")
            return {}
        return rules if isinstance(rules, dict) else {}

    def _remember_draft_segments(
        self,
        chunk: SpooledChunk,
        segments: List[TranscriptSegment],
    ) -> None:
        key = (chunk.meeting_id, chunk.channel)
        words = self._draft_context.setdefault(key, [])
        for segment in segments:
            words.extend(segment.text.split())
        if len(words) > DRAFT_PROMPT_WORDS:
            del words[:-DRAFT_PROMPT_WORDS]

    def _stable_segment_id(self, chunk_id: int, ordinal: int) -> str:
        """Return an idempotent evidence anchor for a chunk segment."""
        raw = f"{self.meeting_id}:{chunk_id}:{ordinal}".encode("utf-8")
        return f"sg_{hashlib.sha256(raw).hexdigest()[:20]}"

    def _set_status(self, chunk_id: int, status: str,
                    error: Optional[str] = None) -> None:
        """Update chunk status, never letting persistence errors kill the worker."""
        try:
            self._repository.set_chunk_status(chunk_id, status, error=error)
        except Exception:
            logger.exception(
                "Could not set chunk %s status to '%s'", chunk_id, status
            )
