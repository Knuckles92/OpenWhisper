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
    REVISION_WINDOW_S,
    align_revision_start,
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

#: Number of preceding recognized words supplied to the next draft decode on
#: the same channel. Ten-meeting dogfood reduced strict tcWER on every tested
#: meeting (0.9--4.4 absolute points) with no measurable throughput cost.
DRAFT_PROMPT_WORDS = 50

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

    def __init__(
        self,
        model_name: str,
        meeting_id: str,
        repository: Any,
        language: Optional[str] = None,
        enable_revisions: bool = False,
    ) -> None:
        """Load a dedicated Whisper model for one meeting.

        Args:
            model_name: Whisper model name (``auto`` resolves to turbo on GPU,
                base on CPU inside ``LocalWhisperBackend``).
            meeting_id: Owning meeting session id.
            repository: ``MeetingRepository`` for chunk status bookkeeping.
            language: Optional ISO-639-1 language code. ``None`` keeps
                Whisper's automatic per-decode detection.
            enable_revisions: Whether experimental rolling transcript rewrites
                may run. Disabled by default because real-meeting dogfood found
                meeting-dependent regressions; the benchmark can opt in for
                continued research.
        """
        self.meeting_id = meeting_id
        self._repository = repository
        self.language = language.strip().lower() if language else None
        self.revisions_enabled = bool(enable_revisions)
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
        # A small, per-meeting/channel transcript tail restores linguistic
        # continuity across the short WAV files required for live latency.
        # The key includes meeting_id because the benchmark deliberately
        # reuses one loaded model for several independent meetings.
        self._draft_context: Dict[tuple[str, str], List[str]] = {}
        #: Coalesce revise requests per channel to the latest frontier.
        self._pending_revise: Dict[str, float] = {}
        self._last_revised_frontier: Dict[str, float] = {}
        self._revise_lock = threading.Lock()

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
            language=self.language,
            progress_cb=progress_cb,
        )

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
        self._draft_context.clear()

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

    def _worker(self) -> None:
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
                chunk,
                beam_size=self._beam_size_for_backlog(),
                initial_prompt=self._draft_prompt(chunk),
            )
            if self._on_chunk_result is None:
                raise RuntimeError("No durable ASR result callback is registered")
            self._on_chunk_result(chunk, segments)
            # Advance context only after the result callback returns, because
            # that callback is the durability boundary. A failed commit and
            # retry must see the same preceding prompt as the first attempt.
            self._remember_draft_segments(chunk, segments)
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
        prompt = " ".join(self._draft_context[key][-DRAFT_PROMPT_WORDS:]).strip()
        return prompt or None

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
        if not self.revisions_enabled:
            return
        with self._revise_lock:
            prev = self._pending_revise.get(channel, -1.0)
            self._pending_revise[channel] = max(prev, float(frontier_s))

    def run_pending_revises(
        self,
        force: bool = False,
        deadline_mono: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Drain due revise passes without delaying queued draft chunks.

        Args:
            force: Ignore the normal cadence. Used once after the final draft
                drain so a short meeting or trailing partial interval still
                gets a cleanup pass.
            deadline_mono: Optional ``time.monotonic()`` deadline; when set,
                stop before starting another revise window once the deadline
                has passed (in-flight window still finishes).

        Returns:
            One result dict per successful revise (``items`` / ``removed_ids``).
        """
        results: List[Dict[str, Any]] = []
        if not self.revisions_enabled or self._stopping or self._backend is None:
            return results
        while True:
            if (
                deadline_mono is not None
                and time.monotonic() >= float(deadline_mono)
            ):
                logger.warning(
                    "ASR revise flush hit deadline with %d channel(s) still "
                    "pending",
                    len(self._pending_revise),
                )
                return results
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
        nominal_start, window_end = revision_window(frontier_s, window_s)
        if window_end - nominal_start < 0.5:
            return None

        # Never bisect a previously persisted Whisper segment at the sliding
        # boundary. A crossing row contains words before the nominal horizon;
        # replacing or deleting the whole row after decoding only its suffix
        # progressively erases the transcript on every rolling pass.
        try:
            boundary_segments = self._repository.get_segments_in_range(
                self.meeting_id, channel, nominal_start, window_end
            )
        except Exception:
            logger.exception("revise_window: failed to align mutable boundary")
            return None
        window_start = align_revision_start(nominal_start, boundary_segments)
        decode_start = window_start

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
                language=self.language,
                # The stitched revise window can span several internal
                # 30-second Whisper windows. Preserve decoder context within
                # this one bounded call; no state carries into the next call,
                # so the long-meeting feedback loop avoided by draft decoding
                # remains impossible.
                condition_on_previous_text=True,
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
