"""Adaptive checkpoint scheduling for the meeting-intelligence agent.

Fires agent checkpoints only when new transcript exists, on an adaptive
interval (45s base, shrunk to 30s under segment pressure, stretched to 60s
when quiet), with an early trigger on topic shift (content-word Jaccard
between the last two 60-second transcript windows). Checkpoints run
sequentially on one worker thread, so work that becomes due while a
checkpoint is in flight coalesces naturally into the next fire.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Set

from meeting.interfaces import AgentResult, CheckpointPayload

logger = logging.getLogger(__name__)

#: How often the worker loop re-evaluates its firing conditions.
_TICK_S = 1.0
#: Minimum spacing between topic-shift computations.
_SHIFT_CHECK_SPACING_S = 10.0
#: Jaccard similarity below which the topic is considered to have shifted.
_SHIFT_JACCARD_THRESHOLD = 0.15
#: Minimum distinct content words per window for a meaningful comparison.
_SHIFT_MIN_WORDS = 8
#: Segment-pressure thresholds for interval adaptation.
_PRESSURE_HIGH = 8
_PRESSURE_LOW = 3
#: Consecutive checkpoint failures before intelligence is declared offline.
_MAX_CONSECUTIVE_FAILURES = 3
_RETRY_BACKOFF_S = (30.0, 60.0, 120.0, 300.0)
#: How long consolidation waits for an in-flight checkpoint to finish — once
#: politely, then again after canceling it.
_CONSOLIDATION_JOIN_S = 10.0
#: How far behind the newest already-sent segment each fetch reaches back.
#: Mic and loopback are separate spools whose chunks pass through one shared ASR
#: FIFO, so a segment starting well before the newest one can still be stored
#: after it. The fetch re-reads this window and drops ids already sent, which is
#: what keeps whole stretches of one channel from becoming invisible forever.
_REFETCH_WINDOW_S = 180.0
#: Fire a transcript polish pass after this many successful card checkpoints.
_POLISH_EVERY_N_CHECKPOINTS = 2
#: Also fire polish when at least this many seconds have elapsed since the
#: last polish (even if checkpoint count is low).
_POLISH_MIN_INTERVAL_S = 90.0
#: How much recent transcript to prefer in a polish payload (full digest still
#: included via get_transcript; this caps enormous meetings for the prompt).
_POLISH_MAX_SEGMENTS = 400

_WORD_RE = re.compile(r"[a-z']+")

#: Tiny inline stopword list; only words of length >= 4 are considered, so
#: shorter function words never reach this filter.
_STOPWORDS = frozenset({
    "that", "this", "with", "have", "will", "from", "they", "been", "were",
    "what", "when", "your", "just", "like", "know", "think", "about", "there",
    "their", "would", "could", "should", "because", "really", "going", "yeah",
    "okay", "right", "well", "then", "them", "than", "some", "also", "into",
    "over", "only", "very", "more", "most", "other", "which", "while", "where",
    "after", "before", "thing", "things", "something", "actually", "basically",
    "little", "kind", "sort", "want", "need", "make", "made", "doing", "done",
    "gonna", "mean", "maybe", "here", "these", "those", "anyway", "again",
    "still", "said", "says", "talk", "talking", "look", "looking", "good",
    "great", "sure", "does", "much", "many", "even", "back", "being", "each",
})


def _content_words(text: str) -> Set[str]:
    """Lowercased content words (length >= 4, stopwords stripped)."""
    return {
        word for word in _WORD_RE.findall(text.lower())
        if len(word) >= 4 and word not in _STOPWORDS
    }


class CheckpointScheduler:
    """Drives agent checkpoints from transcript activity.

    The engine calls :meth:`notify_segments` per segment batch; the worker
    thread fires a checkpoint when enough time has passed for the current
    segment pressure (or earlier on a detected topic shift), building a
    ``CheckpointPayload`` from the engine's state snapshot and the transcript
    segments not yet sent.
    """

    def __init__(self, engine: Any, agent_core: Any,
                 base_interval_s: float = 45.0,
                 min_interval_s: float = 30.0,
                 max_interval_s: float = 60.0,
                 on_health: Optional[Callable[[bool], None]] = None) -> None:
        """Args:
            engine: The ``MeetingEngine`` (provides ``store``, ``clock``, and
                ``get_transcript``).
            agent_core: An ``AgentCore`` implementation.
            base_interval_s: Default spacing between checkpoints.
            min_interval_s: Floor used under high segment pressure and as the
                hard minimum for topic-shift early fires.
            max_interval_s: Ceiling used when few segments are waiting.
            on_health: Optional callback invoked with True/False when the
                intelligence loop comes online or is declared offline after
                repeated failures.
        """
        self._engine = engine
        self._agent = agent_core
        self._base_interval_s = base_interval_s
        self._min_interval_s = min_interval_s
        self._max_interval_s = max_interval_s
        self._on_health = on_health

        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._pending_segments = 0
        self._last_fire_mono = time.monotonic()
        self._last_shift_check_mono = 0.0
        #: start_s of every segment already handed to the agent, pruned to the
        #: re-fetch window so it stays bounded over a long meeting.
        self._sent_starts: Dict[str, float] = {}
        self._max_sent_start_s = -1.0
        self._consecutive_failures = 0
        self._online = True
        self._retry_not_before = 0.0
        self._consolidating = False
        self._successful_checkpoints = 0
        self._last_polish_mono = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the worker thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._last_fire_mono = time.monotonic()
        self._thread = threading.Thread(
            target=self._run_loop, name="meeting-checkpoint-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Checkpoint scheduler started (base=%.0fs min=%.0fs max=%.0fs)",
            self._base_interval_s, self._min_interval_s, self._max_interval_s,
        )

    def stop(self) -> None:
        """Stop periodic firing. Does not cancel an in-flight checkpoint."""
        self._stop_event.set()
        self._wake.set()
        thread = self._thread
        if (thread is not None and thread.is_alive()
                and thread is not threading.current_thread()):
            thread.join(timeout=5.0)

    def notify_segments(self, count: int) -> None:
        """Record newly transcribed segments; called by the engine per batch.

        Args:
            count: Number of segments in the batch.
        """
        if count <= 0:
            return
        with self._lock:
            self._pending_segments += int(count)
        self._wake.set()

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    def _interval_for(self, pending: int) -> float:
        """Adaptive interval: shrink under pressure, stretch when quiet."""
        if pending >= _PRESSURE_HIGH:
            return self._min_interval_s
        if pending < _PRESSURE_LOW:
            return self._max_interval_s
        return self._base_interval_s

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake.wait(timeout=_TICK_S)
            self._wake.clear()
            if self._stop_event.is_set():
                break
            if self._consolidating:
                continue
            with self._lock:
                pending = self._pending_segments
            if pending <= 0:
                continue
            elapsed = time.monotonic() - self._last_fire_mono
            if time.monotonic() < self._retry_not_before:
                continue
            if elapsed < self._min_interval_s:
                continue
            due = elapsed >= self._interval_for(pending)
            if not due:
                due = self._detect_topic_shift()
            if due:
                self._fire()
        logger.debug("Checkpoint scheduler loop exited")

    def _detect_topic_shift(self) -> bool:
        """Compare the last two 60s transcript windows for a topic shift."""
        now_mono = time.monotonic()
        if now_mono - self._last_shift_check_mono < _SHIFT_CHECK_SPACING_S:
            return False
        self._last_shift_check_mono = now_mono

        clock = getattr(self._engine, "clock", None)
        if clock is None:
            return False
        now_s = clock.now_s()
        if now_s < 120.0:
            return False
        try:
            recent = self._engine.get_transcript(after_start_s=now_s - 120.0)
        except Exception:
            logger.exception("Topic-shift transcript fetch failed")
            return False
        cut_s = now_s - 60.0
        older_text = " ".join(
            seg.get("text") or "" for seg in recent
            if float(seg.get("start_s") or 0.0) < cut_s
        )
        newer_text = " ".join(
            seg.get("text") or "" for seg in recent
            if float(seg.get("start_s") or 0.0) >= cut_s
        )
        older_words = _content_words(older_text)
        newer_words = _content_words(newer_text)
        if len(older_words) < _SHIFT_MIN_WORDS or len(newer_words) < _SHIFT_MIN_WORDS:
            return False
        union = older_words | newer_words
        jaccard = len(older_words & newer_words) / len(union) if union else 1.0
        if jaccard < _SHIFT_JACCARD_THRESHOLD:
            logger.info(
                "Topic shift detected (jaccard=%.3f); firing checkpoint early",
                jaccard,
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Firing
    # ------------------------------------------------------------------

    def _build_payload(self, segments: List[Dict[str, Any]],
                       is_consolidation: bool,
                       is_polish: bool = False) -> Optional[CheckpointPayload]:
        store = getattr(self._engine, "store", None)
        if store is None:
            return None
        return CheckpointPayload(
            request_id=uuid.uuid4().hex,
            state_snapshot=store.snapshot(),
            new_segments=segments,
            is_consolidation=is_consolidation,
            is_polish=is_polish,
        )

    def _fetch_cursor_s(self) -> float:
        """The ``after_start_s`` cursor for the next fetch.

        Sits ``_REFETCH_WINDOW_S`` behind the newest segment already sent so
        late-arriving segments from the other channel are still picked up;
        already-sent ids are filtered out of the result.
        """
        if self._max_sent_start_s < 0.0:
            return -1.0
        return max(-1.0, self._max_sent_start_s - _REFETCH_WINDOW_S)

    def _mark_sent(self, segments: List[Dict[str, Any]]) -> None:
        """Record a batch as delivered and prune ids that can never re-appear."""
        for seg in segments:
            seg_id = seg.get("id")
            if not seg_id:
                continue
            start_s = float(seg.get("start_s") or 0.0)
            self._sent_starts[str(seg_id)] = start_s
            if start_s > self._max_sent_start_s:
                self._max_sent_start_s = start_s
        cursor = self._fetch_cursor_s()
        # Anything at or before the cursor is outside every future fetch.
        self._sent_starts = {
            seg_id: start_s
            for seg_id, start_s in self._sent_starts.items()
            if start_s > cursor
        }

    def _fire(self) -> None:
        """Run one checkpoint with everything accumulated since the last send."""
        with self._lock:
            claimed = self._pending_segments
            self._pending_segments = 0
        # Mark the fire time at the start of the run so work that becomes due
        # while the checkpoint executes fires immediately after completion.
        self._last_fire_mono = time.monotonic()

        try:
            fetched = self._engine.get_transcript(
                after_start_s=self._fetch_cursor_s()
            )
        except Exception as exc:
            logger.exception("Checkpoint transcript fetch failed")
            self._record_failure(claimed, str(exc))
            return
        segments = [
            seg for seg in fetched
            if str(seg.get("id") or "") not in self._sent_starts
        ]
        if not segments:
            return

        payload = self._build_payload(segments, is_consolidation=False)
        if payload is None:
            with self._lock:
                self._pending_segments += claimed
            return
        logger.debug(
            "Firing checkpoint %s (%d segments, %d notified)",
            payload.request_id, len(segments), claimed,
        )
        try:
            result = self._agent.checkpoint(payload)
        except Exception as exc:
            logger.exception("Agent checkpoint raised")
            result = AgentResult(ok=False, error=str(exc))

        if result.ok:
            self._consecutive_failures = 0
            self._mark_sent(segments)
            self._set_online(True)
            applied = sum(1 for r in result.op_results if r.ok)
            logger.info(
                "Checkpoint %s done: %d/%d ops applied",
                payload.request_id, applied, len(result.op_results),
            )
            self._successful_checkpoints += 1
            self._maybe_fire_polish()
        else:
            self._record_failure(claimed, result.error or "checkpoint failed")

    def _maybe_fire_polish(self) -> None:
        """Run a slower transcript-text polish pass when due."""
        if self._consolidating or self._stop_event.is_set():
            return
        due_by_count = (
            self._successful_checkpoints > 0
            and self._successful_checkpoints % _POLISH_EVERY_N_CHECKPOINTS == 0
        )
        due_by_time = (
            self._last_polish_mono > 0.0
            and (time.monotonic() - self._last_polish_mono) >= _POLISH_MIN_INTERVAL_S
            and self._successful_checkpoints >= 1
        ) or (
            self._last_polish_mono <= 0.0
            and self._successful_checkpoints >= _POLISH_EVERY_N_CHECKPOINTS
        )
        if not (due_by_count or due_by_time):
            return
        if not self._agent.is_healthy():
            return
        try:
            segments = self._engine.get_transcript()
        except Exception:
            logger.exception("Polish transcript fetch failed")
            return
        if not segments:
            return
        if len(segments) > _POLISH_MAX_SEGMENTS:
            segments = segments[-_POLISH_MAX_SEGMENTS:]
        payload = self._build_payload(segments, is_consolidation=False, is_polish=True)
        if payload is None:
            return
        logger.info(
            "Firing transcript polish %s over %d segments",
            payload.request_id, len(segments),
        )
        try:
            result = self._agent.checkpoint(payload)
        except Exception as exc:
            logger.exception("Agent polish raised")
            result = AgentResult(ok=False, error=str(exc))
        self._last_polish_mono = time.monotonic()
        if result.ok:
            applied = sum(1 for r in result.op_results if r.ok)
            logger.info(
                "Polish %s done: %d/%d ops applied",
                payload.request_id, applied, len(result.op_results),
            )
        else:
            logger.warning(
                "Polish %s failed: %s",
                payload.request_id, result.error or "unknown",
            )

    def _record_failure(self, claimed: int, error: str) -> None:
        """Restore claimed work and schedule a bounded health retry."""
        with self._lock:
            self._pending_segments += claimed
        self._consecutive_failures += 1
        delay = _RETRY_BACKOFF_S[min(
            self._consecutive_failures - 1, len(_RETRY_BACKOFF_S) - 1
        )]
        self._retry_not_before = time.monotonic() + delay
        logger.warning(
            "Checkpoint failed (%d consecutive); retrying in %.0fs: %s",
            self._consecutive_failures, delay, error,
        )
        if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            logger.error(
                "Intelligence declared offline after %d consecutive "
                "checkpoint failures", self._consecutive_failures,
            )
            self._set_online(False)

    def _set_online(self, online: bool) -> None:
        if online == self._online:
            return
        self._online = online
        if online:
            self._consecutive_failures = 0
            self._retry_not_before = 0.0
        if self._on_health is not None:
            try:
                self._on_health(online)
            except Exception:
                logger.exception("Scheduler on_health callback raised")

    # ------------------------------------------------------------------
    # Consolidation
    # ------------------------------------------------------------------

    def run_consolidation(self, timeout_s: float = 120.0) -> None:
        """Run the blocking end-of-meeting consolidation pass.

        Stops periodic firing, then runs one ``consolidate`` call with the
        complete transcript, bounded by ``timeout_s`` (the agent is canceled
        on timeout).

        Args:
            timeout_s: Maximum seconds to wait for the consolidation pass.
        """
        self._consolidating = True
        self.stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            # A periodic checkpoint may still be in flight; give it a moment,
            # then cancel it so consolidation is not raced.
            thread.join(timeout=_CONSOLIDATION_JOIN_S)
            if thread.is_alive():
                logger.warning(
                    "In-flight checkpoint still running; canceling before "
                    "consolidation"
                )
                try:
                    self._agent.cancel()
                except Exception:
                    logger.exception("Agent cancel raised")
                thread.join(timeout=_CONSOLIDATION_JOIN_S)
            if thread.is_alive():
                # Two agent runs sharing one core would interleave tool calls
                # and corrupt the final pass; a stale rolling checkpoint is far
                # less damaging than that.
                logger.error(
                    "Checkpoint worker did not stop; skipping the consolidation "
                    "pass to avoid two concurrent agent runs"
                )
                return

        if not self._agent.is_healthy():
            logger.warning("Agent core unhealthy; skipping consolidation pass")
            return
        try:
            segments = self._engine.get_transcript()
        except Exception:
            logger.exception("Consolidation transcript fetch failed")
            return
        payload = self._build_payload(segments, is_consolidation=True)
        if payload is None:
            logger.warning("No state store available; skipping consolidation")
            return

        logger.info(
            "Running consolidation %s over %d segments (timeout %.0fs)",
            payload.request_id, len(segments), timeout_s,
        )
        result_box: Dict[str, AgentResult] = {}

        def _worker() -> None:
            try:
                result_box["result"] = self._agent.consolidate(payload)
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Agent consolidate raised")
                result_box["result"] = AgentResult(ok=False, error=str(exc))

        worker = threading.Thread(
            target=_worker, name="meeting-consolidation", daemon=True,
        )
        worker.start()
        worker.join(timeout_s)
        if worker.is_alive():
            logger.warning(
                "Consolidation timed out after %.0fs; canceling", timeout_s,
            )
            try:
                self._agent.cancel()
            except Exception:
                logger.exception("Agent cancel raised")
            worker.join(timeout=5.0)

        result = result_box.get("result")
        if result is None:
            logger.warning("Consolidation produced no result (timed out)")
        elif result.ok:
            applied = sum(1 for r in result.op_results if r.ok)
            logger.info(
                "Consolidation done: %d/%d ops applied",
                applied, len(result.op_results),
            )
        else:
            logger.warning("Consolidation failed: %s", result.error)

        # Even when the agent fails or skips timeline, promote evidenced key
        # points (or sample the transcript) so the durable record has beats.
        try:
            from meeting.state.repair import repair_meeting_state

            store = getattr(self._engine, "store", None)
            if store is not None:
                repair_meeting_state(store, segments)
        except Exception:
            logger.exception("State repair after consolidation failed")
