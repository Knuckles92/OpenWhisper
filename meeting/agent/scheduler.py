"""Adaptive checkpoint scheduling for the meeting-intelligence agent.

Fires agent checkpoints only when new transcript exists, on an adaptive
interval (15s base, shrunk to 5s under segment pressure, stretched to 20s
when quiet), with a fast first pass and an early trigger on topic shift
(content-word Jaccard between the last two 60-second transcript windows).
Checkpoints run
sequentially on one worker thread, so work that becomes due while a
checkpoint is in flight coalesces naturally into the next fire.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set

from meeting.interfaces import AgentResult, CheckpointPayload

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConsolidationOutcome:
    """Structured result of an end-of-meeting consolidation pass.

    Attributes:
        status: Terminal finalization status (``completed``, ``unavailable``,
            or ``failed``). Never ``running``/``pending``.
        message: Human-readable detail for persistent UI feedback.
    """
    status: str
    message: str = ""

    def to_dict(self) -> Dict[str, str]:
        """Serialize for status payloads and tests."""
        return {"status": self.status, "message": self.message}

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
#: Card/topic freshness takes priority because both jobs share one agent.
_POLISH_EVERY_N_CHECKPOINTS = 6
#: Also fire polish when at least this many seconds have elapsed since the
#: last polish (even if checkpoint count is low).
_POLISH_MIN_INTERVAL_S = 300.0
#: How much recent transcript to prefer in a polish payload (full digest still
#: included via get_transcript; this caps enormous meetings for the prompt).
_POLISH_MAX_SEGMENTS = 400
#: Fire the dedicated note-taker pass after this many successful card
#: checkpoints (when the agent core supports it). Notes are the note taker's
#: only job, so its cadence is denser than polish.
_NOTES_EVERY_N_CHECKPOINTS = 2
#: Minimum spacing between note-taker passes: the notes page should feel
#: live without doubling agent traffic on busy meetings.
_NOTES_MIN_INTERVAL_S = 45.0
#: How much recent transcript a notes payload may carry; earlier narrative
#: lives in the existing note blocks shipped in the state snapshot.
_NOTES_MAX_SEGMENTS = 300

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
                 base_interval_s: float = 15.0,
                 min_interval_s: float = 5.0,
                 max_interval_s: float = 20.0,
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
        # Seed the dashboard shortly after the first transcript arrives. Keep
        # custom sub-second intervals useful in tests and embeddings.
        self._initial_interval_s = min(3.0, min_interval_s)
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
        self._last_notes_mono = 0.0
        self._notes_checkpoint_mark = 0
        self._notes_sent_starts: Dict[str, float] = {}
        self._notes_max_sent_start_s = -1.0

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

    def prepare_for_end(self) -> None:
        """Stop new rolling fires without blocking the meeting-end path.

        An in-flight cloud request is handled later by
        :meth:`run_consolidation`, after the durable meeting has already been
        marked ended. Calling the agent's synchronous cancellation RPC here
        would otherwise delay capture shutdown and the user's end action.
        """
        self._consolidating = True
        self._stop_event.set()
        self._wake.set()

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
            if not self._sent_starts:
                if elapsed >= self._initial_interval_s:
                    self._fire()
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
                       is_polish: bool = False,
                       is_notes: bool = False) -> Optional[CheckpointPayload]:
        store = getattr(self._engine, "store", None)
        if store is None:
            return None
        return CheckpointPayload(
            request_id=uuid.uuid4().hex,
            state_snapshot=store.snapshot(),
            new_segments=segments,
            is_consolidation=is_consolidation,
            is_polish=is_polish,
            is_notes=is_notes,
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

        # Seed structural live state before the network request. A slow or
        # uncooperative model must not leave the visible topic, summary, and
        # key points blank until the checkpoint eventually returns.
        self._maybe_backfill_live_insights()
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
            self._maybe_fire_notes()
            self._maybe_fire_polish()
        else:
            self._record_failure(claimed, result.error or "checkpoint failed")

    def _dashboard_needs_live_seed(self, snapshot: Dict[str, Any]) -> bool:
        """True when live insights have not seeded the visible dashboard yet."""
        topic = ((snapshot.get("topic") or {}).get("current") or "").strip()
        summary = (snapshot.get("rolling_summary") or "").strip()
        cards = snapshot.get("cards") or {}
        has_key_point = any(
            isinstance(item, dict) and item.get("status") != "removed"
            for item in (cards.get("key_points") or [])
        )
        return not topic or not summary or not has_key_point

    def _maybe_backfill_live_insights(self) -> None:
        """Deterministically seed an empty live dashboard from the transcript."""
        if self._consolidating or self._stop_event.is_set():
            return
        store = getattr(self._engine, "store", None)
        if store is None:
            return
        try:
            snapshot = store.snapshot()
        except Exception:
            logger.exception("Live insight backfill could not snapshot state")
            return
        if not self._dashboard_needs_live_seed(snapshot):
            return
        try:
            segments = self._engine.get_transcript()
        except Exception:
            logger.exception("Live insight backfill transcript fetch failed")
            return
        if not segments:
            return
        try:
            from meeting.state.repair import repair_meeting_state

            applied = repair_meeting_state(store, segments)
            if applied:
                logger.info("Live insight backfill applied %d op(s)", applied)
        except Exception:
            logger.exception("Live insight backfill failed")

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

    def _maybe_fire_notes(self) -> None:
        """Run the dedicated note-taker pass when due.

        Only agent cores that declare ``supports_notes_pass`` see notes
        payloads (both shipped cores implement it: the direct core in
        process, the Pi sidecar via its bundle). Failures are logged and
        never counted toward checkpoint health — the notes page simply
        catches up on the next pass, because a failed batch's segments are
        not marked as consumed.
        """
        if self._consolidating or self._stop_event.is_set():
            return
        if not getattr(self._agent, "supports_notes_pass", False):
            return
        since_mark = self._successful_checkpoints - self._notes_checkpoint_mark
        due_by_count = since_mark >= _NOTES_EVERY_N_CHECKPOINTS
        due_by_time = (
            self._last_notes_mono > 0.0
            and (time.monotonic() - self._last_notes_mono)
            >= _NOTES_MIN_INTERVAL_S
            and since_mark >= 1
        )
        if not (due_by_count or due_by_time):
            return
        if not self._agent.is_healthy():
            return
        # Same late-arrival window logic as card checkpoints: re-read a
        # window behind the newest consumed segment, drop already-sent ids.
        if self._notes_max_sent_start_s >= 0.0:
            cursor = max(
                -1.0, self._notes_max_sent_start_s - _REFETCH_WINDOW_S
            )
        else:
            cursor = -1.0
        try:
            fetched = self._engine.get_transcript(after_start_s=cursor)
        except Exception:
            logger.exception("Notes transcript fetch failed")
            return
        segments = [
            seg for seg in fetched
            if str(seg.get("id") or "") not in self._notes_sent_starts
        ]
        if not segments:
            return
        segments = segments[-_NOTES_MAX_SEGMENTS:]
        payload = self._build_payload(
            segments, is_consolidation=False, is_notes=True,
        )
        if payload is None:
            return
        logger.info(
            "Firing note-taker pass %s over %d segments",
            payload.request_id, len(segments),
        )
        try:
            result = self._agent.checkpoint(payload)
        except Exception as exc:
            logger.exception("Agent note-taker pass raised")
            result = AgentResult(ok=False, error=str(exc))
        # Advance the cadence bookkeeping whether or not the pass succeeded
        # so a failing core cannot hot-loop notes calls; only a success
        # marks the batch consumed, leaving failures to be re-covered.
        self._last_notes_mono = time.monotonic()
        self._notes_checkpoint_mark = self._successful_checkpoints
        if result.ok:
            applied = sum(1 for r in result.op_results if r.ok)
            logger.info(
                "Note-taker pass %s done: %d/%d ops applied",
                payload.request_id, applied, len(result.op_results),
            )
            for seg in segments:
                seg_id = seg.get("id")
                if seg_id:
                    self._notes_sent_starts[str(seg_id)] = float(
                        seg.get("start_s") or 0.0
                    )
            newest = max(
                (float(seg.get("start_s") or 0.0) for seg in segments),
                default=-1.0,
            )
            self._notes_max_sent_start_s = max(
                self._notes_max_sent_start_s, newest
            )
            prune_cursor = max(
                -1.0, self._notes_max_sent_start_s - _REFETCH_WINDOW_S
            )
            self._notes_sent_starts = {
                seg_id: start_s
                for seg_id, start_s in self._notes_sent_starts.items()
                if start_s > prune_cursor
            }
        else:
            logger.warning(
                "Note-taker pass %s failed: %s",
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

    def _revoke_agent_writes(self) -> None:
        """Close the engine agent-write gate before a terminal consolidation."""
        revoke = getattr(self._engine, "revoke_agent_writes", None)
        if callable(revoke):
            try:
                revoke()
            except Exception:
                logger.exception("Engine revoke_agent_writes raised")

    def run_consolidation(self, timeout_s: float = 180.0) -> ConsolidationOutcome:
        """Run the blocking end-of-meeting consolidation pass.

        Stops periodic firing, then runs one ``consolidate`` call with the
        complete transcript, bounded by ``timeout_s`` (the agent is canceled
        on timeout). Deterministic state repair still runs whenever a
        transcript is available, including agent failure paths; repair
        failures never overwrite the primary consolidation outcome.

        Agent write authority stays open only while the consolidation worker
        is still the active writer. Every terminal skip/timeout/outcome path
        revokes authority before returning so a late canceled worker cannot
        mutate durable state afterward.

        Args:
            timeout_s: Maximum seconds to wait for the consolidation pass.

        Returns:
            Structured terminal outcome for UI/finalization persistence.
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
                self._revoke_agent_writes()
                return ConsolidationOutcome(
                    status="failed",
                    message=(
                        "Final insights were skipped because a previous "
                        "checkpoint was still running."
                    ),
                )

        if not self._agent.is_healthy():
            logger.warning("Agent core unhealthy; skipping consolidation pass")
            self._revoke_agent_writes()
            return ConsolidationOutcome(
                status="unavailable",
                message=(
                    "Meeting intelligence is offline; final cloud insights "
                    "could not run."
                ),
            )
        segments: List[Dict[str, Any]] = []
        try:
            segments = self._engine.get_transcript()
        except Exception as exc:
            logger.exception("Consolidation transcript fetch failed")
            self._revoke_agent_writes()
            return ConsolidationOutcome(
                status="failed",
                message=f"Could not load the transcript for final insights: {exc}",
            )
        payload = self._build_payload(segments, is_consolidation=True)
        if payload is None:
            logger.warning("No state store available; skipping consolidation")
            self._revoke_agent_writes()
            return ConsolidationOutcome(
                status="failed",
                message="Meeting state was unavailable for final insights.",
            )

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
            # Revoke before cancel so a worker that ignores cancel cannot land
            # late mutations after the timeout decision.
            self._revoke_agent_writes()
            try:
                self._agent.cancel()
            except Exception:
                logger.exception("Agent cancel raised")
            worker.join(timeout=5.0)

        result = result_box.get("result")
        if result is None:
            logger.warning("Consolidation produced no result (timed out)")
            outcome = ConsolidationOutcome(
                status="failed",
                message=(
                    f"Final insights timed out after {int(timeout_s)}s."
                ),
            )
        elif result.ok:
            applied = sum(1 for r in result.op_results if r.ok)
            logger.info(
                "Consolidation done: %d/%d ops applied",
                applied, len(result.op_results),
            )
            outcome = ConsolidationOutcome(
                status="completed",
                message="Final cloud insights are ready.",
            )
        else:
            error = result.error or "consolidation failed"
            logger.warning("Consolidation failed: %s", error)
            outcome = ConsolidationOutcome(
                status="failed",
                message=f"Final cloud insights failed: {error}",
            )

        # Close the write gate before repair/return so only this pass's
        # already-applied ops remain authoritative.
        self._revoke_agent_writes()

        # Even when the agent fails or skips timeline, promote evidenced key
        # points (or sample the transcript) so the durable record has beats.
        # Repair errors must not replace the primary consolidation outcome.
        try:
            from meeting.state.repair import repair_meeting_state

            store = getattr(self._engine, "store", None)
            if store is not None:
                repair_meeting_state(store, segments)
        except Exception:
            logger.exception("State repair after consolidation failed")
        return outcome

    def run_final_polish(self, timeout_s: float = 60.0) -> ConsolidationOutcome:
        """Run a blocking transcript-text polish over the final segments.

        Stops periodic firing first. Does not revoke agent writes so
        :meth:`run_consolidation` can follow on the same end path.

        Args:
            timeout_s: Maximum seconds to wait for the polish pass.

        Returns:
            Structured outcome. ``completed`` means the agent returned ok
            (including zero ops); failures are non-fatal for consolidation.
        """
        self._consolidating = True
        self.stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_CONSOLIDATION_JOIN_S)
            if thread.is_alive():
                try:
                    self._agent.cancel()
                except Exception:
                    logger.exception("Agent cancel raised before final polish")
                thread.join(timeout=_CONSOLIDATION_JOIN_S)
            if thread.is_alive():
                logger.error(
                    "Checkpoint worker did not stop; skipping final polish"
                )
                return ConsolidationOutcome(
                    status="failed",
                    message=(
                        "Transcript cleanup was skipped because a previous "
                        "checkpoint was still running."
                    ),
                )

        if not self._agent.is_healthy():
            return ConsolidationOutcome(
                status="unavailable",
                message=(
                    "Meeting intelligence is offline; transcript cleanup "
                    "could not run."
                ),
            )
        try:
            segments = self._engine.get_transcript()
        except Exception as exc:
            logger.exception("Final polish transcript fetch failed")
            return ConsolidationOutcome(
                status="failed",
                message=f"Could not load the transcript for cleanup: {exc}",
            )
        if not segments:
            return ConsolidationOutcome(
                status="completed",
                message="No transcript text needed cleanup.",
            )
        if len(segments) > _POLISH_MAX_SEGMENTS:
            step = max(1, _POLISH_MAX_SEGMENTS - 40)
            blocks = [
                segments[start:start + _POLISH_MAX_SEGMENTS]
                for start in range(0, len(segments), step)
            ]
        else:
            blocks = [segments]

        last_error = ""
        applied_any = False
        for block in blocks:
            payload = self._build_payload(
                block, is_consolidation=False, is_polish=True,
            )
            if payload is None:
                return ConsolidationOutcome(
                    status="failed",
                    message="Meeting state was unavailable for transcript cleanup.",
                )
            result_box: Dict[str, AgentResult] = {}

            def _worker(bound_payload: CheckpointPayload = payload) -> None:
                try:
                    result_box["result"] = self._agent.checkpoint(bound_payload)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.exception("Agent final polish raised")
                    result_box["result"] = AgentResult(ok=False, error=str(exc))

            worker = threading.Thread(
                target=_worker, name="meeting-final-polish", daemon=True,
            )
            worker.start()
            worker.join(timeout=timeout_s)
            if worker.is_alive():
                logger.warning(
                    "Final polish timed out after %.0fs; canceling", timeout_s,
                )
                try:
                    self._agent.cancel()
                except Exception:
                    logger.exception("Agent cancel raised")
                worker.join(timeout=5.0)
                last_error = (
                    f"Transcript cleanup timed out after {int(timeout_s)}s."
                )
                break
            result = result_box.get("result")
            if result is None:
                last_error = "Transcript cleanup produced no result."
                break
            if not result.ok:
                last_error = result.error or "transcript cleanup failed"
                break
            applied_any = True
            self._last_polish_mono = time.monotonic()

        if last_error and not applied_any:
            return ConsolidationOutcome(status="failed", message=last_error)
        return ConsolidationOutcome(
            status="completed",
            message="Transcript cleanup is ready.",
        )
