"""Background ASR engine for Meeting Mode.

``MeetingAsrEngine`` owns a dedicated faster-whisper instance (separate from
the dictation backend) and a single daemon worker that consumes an unbounded
queue of spooled chunks — durability first: chunks are never dropped, failures
are retried up to :data:`MAX_ATTEMPTS` times, and anything still unfinished
survives in the database (``asr_status``) for startup recovery via
:meth:`MeetingAsrEngine.requeue_pending`.
"""
from __future__ import annotations

import hashlib
import logging
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from meeting.asr.audio import load_wav_int16, prepare_for_whisper
from meeting.asr.revise import (
    REVISION_CONTEXT_S,
    REVISION_WINDOW_S,
    build_initial_prompt,
    match_segments,
    revise_segment_id,
    revision_window,
    select_chunks_for_window,
    stitch_window_audio,
)
from meeting.interfaces import SpooledChunk, TranscriptSegment

logger = logging.getLogger(__name__)

#: Total transcription attempts per chunk before giving up.
MAX_ATTEMPTS = 3

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

#: Minimum meeting-clock progress per channel between rolling re-decodes.
#: Draft chunks are intentionally short for low UI latency, but re-running a
#: 45-second window after every 5-second chunk would spend roughly nine times
#: real time on duplicate audio once the window is full.  A 20-second cadence
#: keeps revisions responsive while bounding the steady-state overlap cost.
REVISE_MIN_ADVANCE_S = 20.0

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
        self._enqueued_at: Dict[int, float] = {}
        self._queue_wait_warned: set = set()
        self._fast_mode = False
        #: Coalesce revise requests per channel to the latest frontier.
        self._pending_revise: Dict[str, float] = {}
        self._last_revised_frontier: Dict[str, float] = {}
        self._revise_lock = threading.Lock()

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
            self._enqueued_at.setdefault(chunk.chunk_id, time.monotonic())
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
                        self._enqueued_at.pop(item.chunk_id, None)
                        self._queue_wait_warned.discard(item.chunk_id)
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
            self._log_queue_wait(chunk)
            segments = self._transcribe_chunk(
                chunk, beam_size=self._beam_size_for_backlog()
            )
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

    def _beam_size_for_backlog(self) -> int:
        """Choose live decode quality from the current queue depth."""
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
        """Log a chunk once when it waited too long for live ASR."""
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
        """Return whether int16 audio is safely below the silence shortcut."""
        if frames.size == 0:
            return True
        peak = int(np.max(np.abs(frames.astype(np.int32, copy=False))))
        return peak <= DIGITAL_SILENCE_PEAK

    def _transcribe_chunk(
        self, chunk: SpooledChunk, beam_size: int = 5
    ) -> List[TranscriptSegment]:
        """Load, convert, and transcribe one spooled WAV chunk.

        Args:
            chunk: Durable audio chunk to transcribe.
            beam_size: Whisper decode beam size. Backlogged live processing
                uses one beam to recover; normal processing uses five.

        Returns:
            Meeting-clock-timestamped segments; empty when the chunk holds no
            speech.
        """
        frames, sample_rate = load_wav_int16(chunk.file_path)
        if self._is_digital_silence(frames):
            logger.debug("Skipping digitally silent meeting chunk %s", chunk.chunk_id)
            return []
        audio = prepare_for_whisper(frames, sample_rate)
        if audio.size == 0:
            return []

        whisper_segments, _info = self._backend.model.transcribe(
            audio,
            beam_size=beam_size,
            vad_filter=True,
            word_timestamps=False,
            # Avoid prompt feedback loops between multiple decode windows in
            # a long no-pause chunk. Each durable chunk is already a separate
            # call and therefore never inherits text from the previous chunk.
            condition_on_previous_text=False,
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

    # ------------------------------------------------------------------
    # Rolling revise
    # ------------------------------------------------------------------

    def backlog_depth(self) -> int:
        """Chunks waiting behind the in-flight worker item (0 when idle)."""
        with self._idle_cond:
            return max(0, self._outstanding - 1)

    def is_backlogged(self) -> bool:
        """True when draft ASR should prefer catching up over revise work."""
        return self.backlog_depth() >= FAST_MODE_BACKLOG_CHUNKS

    def _draft_work_precedes_revise(self) -> bool:
        """Return whether another draft decode must run before revise work.

        The normal revise call happens inside the ASR worker's durable-result
        callback, where one outstanding item is the just-finished current
        chunk. From every other thread, any outstanding item may still be using
        the model and therefore also prevents a concurrent revise decode.
        """
        with self._idle_cond:
            current_is_worker = threading.current_thread() is self._thread
            current_allowance = 1 if current_is_worker else 0
            return self._outstanding > current_allowance

    def schedule_revise(self, channel: str, frontier_s: float) -> None:
        """Coalesce a revise request for ``channel`` up to ``frontier_s``."""
        with self._revise_lock:
            prev = self._pending_revise.get(channel, -1.0)
            self._pending_revise[channel] = max(prev, float(frontier_s))

    def run_pending_revises(self, force: bool = False) -> List[Dict[str, Any]]:
        """Drain due revise passes without delaying queued draft chunks.

        Args:
            force: Ignore the normal cadence. Used once after the final draft
                drain so a short meeting or trailing partial interval still
                gets a cleanup pass.

        Returns:
            One result dict per successful revise (``items`` / ``removed_ids``).
        """
        results: List[Dict[str, Any]] = []
        if self._stopping or self._backend is None:
            return results
        while True:
            # Revise work is secondary to live draft latency. Even one chunk
            # already waiting should be transcribed before another overlapping
            # window is decoded.
            if self._draft_work_precedes_revise():
                return results
            with self._revise_lock:
                if not self._pending_revise:
                    return results
                due = next(
                    (
                        (pending_channel, pending_frontier)
                        for pending_channel, pending_frontier
                        in self._pending_revise.items()
                        if force or pending_frontier - self._last_revised_frontier.get(
                            pending_channel, 0.0
                        ) >= REVISE_MIN_ADVANCE_S
                    ),
                    None,
                )
                if due is None:
                    # Keep coalesced requests for a later chunk (or the forced
                    # end-of-meeting flush).
                    return results
                channel, frontier = due
                del self._pending_revise[channel]
            try:
                outcome = self.revise_window(channel, frontier)
            except Exception:
                logger.exception(
                    "Rolling ASR revise failed for channel %s @ %.2fs",
                    channel, frontier,
                )
                continue
            with self._revise_lock:
                self._last_revised_frontier[channel] = max(
                    frontier,
                    self._last_revised_frontier.get(channel, 0.0),
                )
            if outcome:
                results.append(outcome)

    def revise_window(
        self,
        channel: str,
        frontier_s: float,
        window_s: float = REVISION_WINDOW_S,
    ) -> Optional[Dict[str, Any]]:
        """Re-decode the trailing window and persist the matched revise plan.

        Args:
            channel: Capture channel (``mic`` / ``loopback``).
            frontier_s: Meeting-clock end of the latest draft audio.
            window_s: Trailing horizon to re-transcribe.

        Returns:
            Dict with ``items`` and ``removed_ids`` when a revise applied,
            otherwise None.
        """
        if self._backend is None or not self._backend.is_available():
            return None
        window_start, window_end = revision_window(frontier_s, window_s)
        if window_end - window_start < 0.5:
            return None
        decode_start = max(0.0, window_start - REVISION_CONTEXT_S)

        try:
            chunks = self._repository.get_audio_chunks(self.meeting_id)
        except Exception:
            logger.exception("revise_window: failed to list audio chunks")
            return None
        selected = select_chunks_for_window(
            chunks, channel, decode_start, window_end
        )
        if not selected:
            return None

        audio, audio_start = stitch_window_audio(
            selected, decode_start, window_end
        )
        if audio.size < int(0.25 * 16000):
            return None

        try:
            prior = self._repository.get_segments(
                self.meeting_id, after_start_s=-1.0
            )
        except Exception:
            logger.exception("revise_window: failed to load prior segments")
            prior = []
        prior_text = [
            seg for seg in prior
            if seg.get("channel") == channel
            and float(seg.get("end_s") or 0.0) <= decode_start + 1e-6
        ]
        initial_prompt = build_initial_prompt(prior_text[-12:])

        beam_size = 1 if self.is_backlogged() else 5
        try:
            whisper_segments, _info = self._backend.model.transcribe(
                audio,
                beam_size=beam_size,
                vad_filter=True,
                word_timestamps=False,
                condition_on_previous_text=False,
                initial_prompt=initial_prompt or None,
            )
        except Exception:
            logger.exception("revise_window: Whisper decode failed")
            return None

        decoded: List[TranscriptSegment] = []
        for ordinal, seg in enumerate(whisper_segments):
            text = (seg.text or "").strip()
            if not text:
                continue
            start_s = audio_start + float(seg.start)
            end_s = audio_start + float(seg.end)
            if end_s <= window_start:
                # Lead-in audio exists only to give the first mutable segment
                # complete linguistic context; do not duplicate rows wholly
                # outside the actual revision window.
                continue
            # Anchor new inserts to the first overlapping chunk when possible.
            chunk_id = selected[0].get("id")
            for chunk in selected:
                c0 = float(chunk.get("start_s") or 0.0)
                c1 = c0 + float(chunk.get("duration_s") or 0.0)
                if c0 <= start_s < c1:
                    chunk_id = chunk.get("id")
                    break
            decoded.append(TranscriptSegment(
                segment_id=revise_segment_id(
                    self.meeting_id, channel, start_s, ordinal
                ),
                meeting_id=self.meeting_id,
                chunk_id=int(chunk_id) if chunk_id is not None else None,
                channel=channel,
                start_s=start_s,
                end_s=end_s,
                text=text,
            ))

        try:
            existing = self._repository.get_segments_in_range(
                self.meeting_id, channel, window_start, window_end
            )
        except Exception:
            logger.exception("revise_window: failed to load window segments")
            return None

        plan = match_segments(existing, decoded)
        if not plan.upserts and not plan.remove_ids:
            return None

        # Prefer the planned absolute window so deletes stay in-horizon.
        try:
            rows, removed = self._repository.revise_segments_in_range(
                self.meeting_id,
                channel,
                window_start,
                window_end,
                plan.upserts,
                plan.remove_ids,
            )
        except Exception:
            logger.exception("revise_window: persist failed")
            return None

        logger.info(
            "Rolling ASR revise %s [%.1f–%.1f]: upserted %d, removed %d",
            channel, window_start, window_end, len(rows), len(removed),
        )
        return {"items": rows, "removed_ids": removed, "channel": channel}
