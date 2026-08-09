"""Background ASR engine for Meeting Mode.

``MeetingAsrEngine`` owns a dedicated faster-whisper instance (separate from
the dictation backend) and a single daemon worker that consumes an unbounded
queue of spooled chunks — durability first: chunks are never dropped, failures
are retried up to :data:`MAX_ATTEMPTS` times, and anything still unfinished
survives in the database (``asr_status``) for startup recovery via
:meth:`MeetingAsrEngine.requeue_pending`.
"""
from __future__ import annotations

import logging
import hashlib
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from meeting.asr.audio import load_wav_int16, prepare_for_whisper
from meeting.interfaces import SpooledChunk, TranscriptSegment

logger = logging.getLogger(__name__)

#: Total transcription attempts per chunk before giving up.
MAX_ATTEMPTS = 3

#: Queue sentinel telling the worker to exit.
_STOP = object()


class MeetingAsrEngine:
    """Implements ``meeting.interfaces.AsrEngine`` on faster-whisper.

    A failed model load never raises out of the constructor: the engine logs
    the failure and sets :attr:`is_available` to False so the meeting can
    proceed (chunks stay ``pending``/``failed`` in the database and remain
    recoverable).
    """

    def __init__(self, model_name: str, meeting_id: str, repository: Any) -> None:
        """Load a dedicated Whisper model for one meeting.

        Args:
            model_name: Whisper model name (``auto`` resolves to turbo on GPU,
                base on CPU inside ``LocalWhisperBackend``).
            meeting_id: Owning meeting session id.
            repository: ``MeetingRepository`` for chunk status bookkeeping.
        """
        self.meeting_id = meeting_id
        self._repository = repository
        self._backend = None
        self.is_available = False

        try:
            from transcriber.local_backend import LocalWhisperBackend

            backend = LocalWhisperBackend(model_name=model_name)
            if backend.is_available():
                self._backend = backend
                self.is_available = True
                logger.info(
                    "Meeting ASR engine ready: %s", getattr(backend, "name", model_name)
                )
            else:
                logger.error(
                    "Meeting ASR model '%s' failed to load "
                    "(missing=%s); engine unavailable",
                    model_name, getattr(backend, "is_model_missing", "?"),
                )
        except Exception:
            logger.exception(
                "Meeting ASR backend construction failed for model '%s'; "
                "engine unavailable", model_name,
            )

        self._queue: "queue.Queue" = queue.Queue()  # unbounded: never drop chunks
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
        self._attempts: Dict[int, int] = {}

    # ------------------------------------------------------------------
    # AsrEngine surface
    # ------------------------------------------------------------------

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
        self._thread = threading.Thread(
            target=self._worker, name="meeting-asr", daemon=True
        )
        self._thread.start()

    def enqueue(self, chunk: SpooledChunk) -> None:
        """Queue a finalized chunk for transcription."""
        with self._idle_cond:
            self._outstanding += 1
            self._queued_ids.add(chunk.chunk_id)
        self._queue.put(chunk)

    def drain(self, timeout_s: float) -> bool:
        """Block until every enqueued chunk has finished (or given up).

        Args:
            timeout_s: Maximum seconds to wait.

        Returns:
            True when the queue emptied and the worker went idle within the
            timeout, False otherwise.
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._idle_cond:
            while self._outstanding > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "ASR drain timed out with %d chunk(s) outstanding",
                        self._outstanding,
                    )
                    return False
                self._idle_cond.wait(remaining)
        return True

    def stop(self) -> None:
        """Stop the worker and release the model."""
        self._stopping = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._queue.put(_STOP)
            thread.join(timeout=10.0)
            if thread.is_alive():
                logger.warning("Meeting ASR worker did not stop within 10s")
        self._thread = None

        backend = self._backend
        self._backend = None
        self.is_available = False
        if backend is not None:
            try:
                cleanup = getattr(backend, "cleanup", None)
                if callable(cleanup):
                    cleanup()
            except Exception:
                logger.exception("Error releasing meeting ASR model")
            del backend

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def requeue_pending(self) -> int:
        """Re-enqueue this meeting's unfinished chunks from the database.

        Pulls ``pending`` and retryable ``failed`` chunks via
        ``repository.get_pending_chunks`` and enqueues them, skipping any
        chunk already tracked by this engine. Used at startup recovery and
        after engine start.

        Returns:
            Number of chunks enqueued.
        """
        try:
            rows = self._repository.get_pending_chunks(self.meeting_id)
        except Exception:
            logger.exception("Could not list pending chunks for %s", self.meeting_id)
            return 0

        requeued = 0
        for row in rows:
            chunk = SpooledChunk(
                chunk_id=row["id"],
                meeting_id=row["meeting_id"],
                channel=row["channel"],
                seq=row["seq"],
                file_path=row["file_path"],
                start_s=row["start_s"],
                duration_s=row["duration_s"],
                sample_rate=row["sample_rate"],
            )
            with self._idle_cond:
                if chunk.chunk_id in self._queued_ids:
                    continue
                # Seed local attempts from the database so a chunk that failed
                # before a crash keeps its bounded retry budget.
                self._attempts[chunk.chunk_id] = int(row.get("asr_attempts") or 0)
            self.enqueue(chunk)
            requeued += 1
        if requeued:
            logger.info(
                "Requeued %d pending chunk(s) for meeting %s",
                requeued, self.meeting_id,
            )
        return requeued

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        """Consume the chunk queue until the stop sentinel arrives."""
        while True:
            item = self._queue.get()
            if item is _STOP:
                break
            requeued = False
            try:
                requeued = self._process_chunk(item)
            except Exception:  # defensive: _process_chunk handles its own errors
                logger.exception(
                    "Unexpected ASR worker error on chunk %s", item.chunk_id
                )
            finally:
                with self._idle_cond:
                    self._outstanding -= 1
                    if not requeued:
                        self._queued_ids.discard(item.chunk_id)
                    if self._outstanding <= 0:
                        self._idle_cond.notify_all()
        logger.debug("Meeting ASR worker exited")

    def _process_chunk(self, chunk: SpooledChunk) -> bool:
        """Transcribe one chunk; retry on failure.

        Returns:
            True when the chunk was re-enqueued for another attempt, False
            when it finished (done, gave up, or engine unavailable).
        """
        if self._backend is None or not self._backend.is_available():
            # Leave asr_attempts untouched ('processing' is what increments
            # it), so the chunk stays recoverable once a model is available.
            logger.warning(
                "ASR engine unavailable; leaving chunk %s for recovery",
                chunk.chunk_id,
            )
            self._set_status(chunk.chunk_id, "failed", "ASR engine unavailable")
            self._attempts.pop(chunk.chunk_id, None)
            return False

        attempts = self._attempts.get(chunk.chunk_id, 0) + 1
        self._attempts[chunk.chunk_id] = attempts
        try:
            self._set_status(chunk.chunk_id, "processing")
            segments = self._transcribe_chunk(chunk)
            if self._on_chunk_result is None:
                raise RuntimeError("No durable ASR result callback is registered")
            self._on_chunk_result(chunk, segments)
            self._attempts.pop(chunk.chunk_id, None)
            return False
        except Exception as e:
            logger.exception(
                "Transcription failed for chunk %s (attempt %d/%d)",
                chunk.chunk_id, attempts, MAX_ATTEMPTS,
            )
            self._set_status(chunk.chunk_id, "failed", str(e))
            if attempts < MAX_ATTEMPTS and not self._stopping:
                self.enqueue(chunk)
                return True
            self._attempts.pop(chunk.chunk_id, None)
            logger.error(
                "Giving up on chunk %s after %d attempt(s)", chunk.chunk_id, attempts
            )
            return False

    def _transcribe_chunk(self, chunk: SpooledChunk) -> List[TranscriptSegment]:
        """Load, convert, and transcribe one spooled WAV chunk.

        Returns:
            Meeting-clock-timestamped segments; empty when the chunk holds no
            speech.
        """
        frames, sample_rate = load_wav_int16(chunk.file_path)
        audio = prepare_for_whisper(frames, sample_rate)
        if audio.size == 0:
            return []

        whisper_segments, _info = self._backend.model.transcribe(
            audio,
            beam_size=5,
            vad_filter=True,
            word_timestamps=False,
        )

        segments: List[TranscriptSegment] = []
        for ordinal, seg in enumerate(whisper_segments):
            text = (seg.text or "").strip()
            if not text:
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
        return segments

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
