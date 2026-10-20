"""Adaptive checkpoint scheduling for the meeting-intelligence agent.

Fires agent checkpoints only when new transcript exists, on an adaptive
interval (15s base, shrunk to 5s under segment pressure, stretched to 20s
when quiet), after two minutes of meeting context, with an early trigger on topic shift
between the last two 60-second transcript windows. The shift judgment comes
from an optional semantic ``topic_judge`` (TypeSafe) when the engine supplies
one, and otherwise from content-word Jaccard. Checkpoints run
sequentially on one worker thread, so work that becomes due while a
checkpoint is in flight coalesces naturally into the next fire.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set

from meeting.finalization import (
    POLISH_MAX_SEGMENTS,
    POLISH_TIMEOUT_S,
    polish_blocks,
    polish_transcript,
    run_agent_call,
)
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
#: Default checkpoint intervals when neither the caller nor the core sets one.
_BASE_INTERVAL_S = 15.0
_MIN_INTERVAL_S = 5.0
_MAX_INTERVAL_S = 20.0
#: Let the opening discussion establish context before automatic generation.
_INITIAL_CONTEXT_S = 120.0
#: Minimum spacing between topic-shift computations.
_SHIFT_CHECK_SPACING_S = 10.0
#: Jaccard similarity below which the topic is considered to have shifted.
_SHIFT_JACCARD_THRESHOLD = 0.15
#: Semantic judge probability at or above which the topic has shifted. On 97
#: human-labelled AMI minute pairs this gave precision 0.69 at recall 0.78,
#: against precision 0.35 for the Jaccard rule, which fired on 89 of 96.
_SHIFT_SEMANTIC_THRESHOLD = 0.5
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
#: How long ``stop()`` waits for the periodic worker to exit.
_STOP_JOIN_S = 5.0
#: How far behind the newest already-sent segment each fetch reaches back.
#: Mic and loopback are separate spools whose chunks pass through one shared ASR
#: FIFO, so a segment starting well before the newest one can still be stored
#: after it. The fetch re-reads this window and drops ids already sent, which is
#: what keeps whole stretches of one channel from becoming invisible forever.
_REFETCH_WINDOW_S = 180.0
#: An agent payload is bounded even when transcription catches up after a
#: long outage. The extra row tells the scheduler to continue on the next tick.
_DELIVERY_PAGE_SIZE = 300
#: Fire a transcript polish pass after this many successful card checkpoints.
#: Card/topic freshness takes priority because both jobs share one agent.
_POLISH_EVERY_N_CHECKPOINTS = 6
#: Also fire polish when at least this many seconds have elapsed since the
#: last polish (even if checkpoint count is low).
_POLISH_MIN_INTERVAL_S = 45.0
_POLISH_INITIAL_DELAY_S = 15.0
#: Fire the dedicated note-taker pass after this many successful card
#: checkpoints (when the agent core supports it). Notes are the note taker's
#: only job, so its cadence is denser than polish.
_NOTES_EVERY_N_CHECKPOINTS = 2
#: Time-based catch-up and failure retry interval for the note taker.
_NOTES_MIN_INTERVAL_S = 45.0
#: How much recent transcript a notes payload may carry; earlier narrative
#: lives in the existing note blocks shipped in the state snapshot.
_NOTES_MAX_SEGMENTS = 300
#: Bounds on explicit note requests from the dashboard.
_NOTE_REQUEST_MAX_CHARS = 4000
_MAX_QUEUED_NOTE_REQUESTS = 4


@dataclass(frozen=True)
class CheckpointCadence:
    """How often a core's passes run, for cores that need their own pace.

    An agent core may expose one as ``cadence``; without it the module
    constants above apply, which suit the built-in engines.
    """

    base_interval_s: float
    min_interval_s: float
    max_interval_s: float
    polish_every_n: int
    polish_min_interval_s: float
    notes_every_n: int
    notes_min_interval_s: float


#: An installed agent (Claude Code, Codex, OpenCode) takes 10-20 s per run and
#: draws on the user's own plan, so cards refresh about once a minute, notes
#: every other checkpoint, and polish every few minutes.
INSTALLED_AGENT_CADENCE = CheckpointCadence(
    base_interval_s=45.0,
    min_interval_s=30.0,
    max_interval_s=90.0,
    polish_every_n=4,
    polish_min_interval_s=180.0,
    notes_every_n=2,
    notes_min_interval_s=120.0,
)

#: Trailing transcript window re-sent when a human correction or insight asks
#: the agent to reconsider its state without waiting for new speech. The full
#: dashboard state travels alongside, so a bounded recent window is enough
#: context and keeps a long meeting's guidance pass inside the model budget.
_GUIDANCE_MAX_SEGMENTS = 300

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


def _applied_count(result: AgentResult) -> int:
    return sum(1 for r in result.op_results if r.ok)


def _revised_transcript(result: AgentResult) -> bool:
    """Whether the pass rewrote any transcript segment text."""
    return any(r.ok and r.op.get("op") == "revise_segment_text"
               for r in result.op_results)


@dataclass
class _NotesPass:
    """A note-taker pass about to run, and where its delivery resumes."""

    payload: CheckpointPayload
    segments: List[Dict[str, Any]]
    guidance: bool
    more_delivery: bool
    delivery_seq: int


def _consolidation_outcome(result: Optional[AgentResult],
                           timeout_s: float) -> ConsolidationOutcome:
    """The terminal outcome for a consolidation result (None: timed out)."""
    if result is None:
        logger.warning("Consolidation produced no result (timed out)")
        return ConsolidationOutcome(
            status="failed",
            message=(
                f"Final insights timed out after {int(timeout_s)}s."
            ),
        )
    if result.ok:
        logger.info(
            "Consolidation done: %d/%d ops applied",
            _applied_count(result), len(result.op_results),
        )
        return ConsolidationOutcome(
            status="completed",
            message="Final insights are ready.",
        )
    error = result.error or "consolidation failed"
    logger.warning("Consolidation failed: %s", error)
    return ConsolidationOutcome(
        status="failed",
        message=f"Final insights failed: {error}",
    )


class CheckpointScheduler:
    """Drives agent checkpoints from transcript activity.

    The engine calls :meth:`notify_segments` per segment batch; the worker
    thread fires a checkpoint when enough time has passed for the current
    segment pressure (or earlier on a detected topic shift), building a
    ``CheckpointPayload`` from the engine's state snapshot and the transcript
    segments not yet sent.
    """

    def __init__(self, engine: Any, agent_core: Any,
                 base_interval_s: Optional[float] = None,
                 min_interval_s: Optional[float] = None,
                 max_interval_s: Optional[float] = None,
                 on_health: Optional[Callable[[bool], None]] = None,
                 monotonic: Optional[Callable[[], float]] = None,
                 topic_judge: Optional[
                     Callable[[str, str], Optional[float]]
                 ] = None) -> None:
        self._monotonic = monotonic or (lambda: time.monotonic())
        self._engine = engine
        self._agent = agent_core
        cadence = getattr(agent_core, "cadence", None)
        self._cadence: Optional[CheckpointCadence] = (
            cadence if isinstance(cadence, CheckpointCadence) else None
        )
        self._base_interval_s = self._interval_setting(
            base_interval_s, "base_interval_s", _BASE_INTERVAL_S)
        self._min_interval_s = self._interval_setting(
            min_interval_s, "min_interval_s", _MIN_INTERVAL_S)
        self._max_interval_s = self._interval_setting(
            max_interval_s, "max_interval_s", _MAX_INTERVAL_S)
        self._on_health = on_health
        #: ``(previous_window_text, window_text) -> P(topic changed)``; ``None``
        #: from the judge means "no answer", and the lexical rule decides.
        self._topic_judge = topic_judge

        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._guidance_pending = False
        self._notes_guidance_pending = False
        self._guidance_generation = 0
        self._notes_retry_not_before = 0.0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._pending_segments = 0
        self._last_fire_mono = self._monotonic()
        self._started_mono = self._last_fire_mono
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
        self._polish_checkpoint_mark = 0
        self._polish_retry_pending = False
        self._last_notes_mono = 0.0
        self._notes_checkpoint_mark = 0
        self._notes_sent_starts: Dict[str, float] = {}
        self._notes_max_sent_start_s = -1.0
        self._note_requests = deque()
        self._delivery_enabled = all(callable(getattr(engine, name, None)) for name in (
            "get_transcript_delivery_page", "get_transcript_delivery_cursor",
            "advance_transcript_delivery_cursor", "get_transcript_delivery_high_water",
        ))
        self._card_cursor_seq = (
            int(engine.get_transcript_delivery_cursor("cards"))
            if self._delivery_enabled else 0
        )
        self._notes_cursor_seq = (
            int(engine.get_transcript_delivery_cursor("notes"))
            if self._delivery_enabled else 0
        )
        self._notes_resume_pending = False

    def _interval_setting(self, explicit: Optional[float], name: str,
                          default: float) -> float:
        """The caller's interval, else the core's cadence, else the default."""
        if explicit is not None:
            return explicit
        if self._cadence:
            return getattr(self._cadence, name)
        return default

    def _pace(self, name: str, default: float) -> float:
        """A cadence value: the core's own, else the module default.

        The default is read at call time so it follows the module constant.
        """
        if self._cadence is not None:
            return getattr(self._cadence, name)
        return default

    def start(self) -> None:
        """Start the worker thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        # Query the durable backlog before starting: a failed database read
        # must not leave an orphan scheduler thread behind.
        high_water = (
            self._engine.get_transcript_delivery_high_water()
            if self._delivery_enabled else 0
        )
        has_backlog = high_water > self._card_cursor_seq
        self._notes_resume_pending = high_water > self._notes_cursor_seq
        self._stop_event.clear()
        self._last_fire_mono = self._monotonic()
        self._started_mono = self._last_fire_mono
        self._thread = threading.Thread(
            target=self._run_loop, name="meeting-checkpoint-scheduler",
            daemon=True,
        )
        self._thread.start()
        if has_backlog:
            self.notify_segments(1)
        elif self._notes_resume_pending:
            self._wake.set()
        logger.info(
            "Checkpoint scheduler started (base=%.0fs min=%.0fs max=%.0fs)",
            self._base_interval_s, self._min_interval_s, self._max_interval_s,
        )

    def stop(self) -> None:
        """Stop periodic firing. Does not cancel an in-flight checkpoint."""
        self._stop_event.set()
        self._cancel_note_requests()
        self._wake.set()
        thread = self._thread
        if (thread is not None and thread.is_alive()
                and thread is not threading.current_thread()):
            thread.join(timeout=_STOP_JOIN_S)

    def prepare_for_end(self) -> None:
        """Stop new rolling fires without blocking the meeting-end path.

        An in-flight cloud request is handled later by
        :meth:`run_consolidation`, after the durable meeting has already been
        marked ended. Calling the agent's synchronous cancellation RPC here
        would otherwise delay capture shutdown and the user's end action.
        """
        self._consolidating = True
        self._stop_event.set()
        self._cancel_note_requests()
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

    def seed_sent_segments(self, segments: List[Dict[str, Any]]) -> None:
        """Mark a meeting-to-date transcript as already delivered.

        Used when intelligence restarts mid-meeting so neither the card nor
        the notes pass re-ships everything that came before.

        Args:
            segments: Transcript rows (``id`` and ``start_s``) to mark.
        """
        self._mark_sent(segments)
        for seg in segments:
            seg_id = seg.get("id")
            if not seg_id:
                continue
            start_s = float(seg.get("start_s") or 0.0)
            self._notes_sent_starts[str(seg_id)] = start_s
            if start_s > self._notes_max_sent_start_s:
                self._notes_max_sent_start_s = start_s
        self._prune_notes_sent()
        if self._delivery_enabled:
            high_water = self._engine.get_transcript_delivery_high_water()
            self._engine.advance_transcript_delivery_cursor("cards", high_water)
            self._engine.advance_transcript_delivery_cursor("notes", high_water)
            self._card_cursor_seq = max(self._card_cursor_seq, high_water)
            self._notes_cursor_seq = max(self._notes_cursor_seq, high_water)
            self._notes_resume_pending = False

    def _delivery_page(self, consumer: str) -> tuple[List[Dict[str, Any]], bool, int]:
        """Return a bounded insertion-order page and its acknowledgment point."""
        cursor = self._card_cursor_seq if consumer == "cards" else self._notes_cursor_seq
        fetched = self._engine.get_transcript_delivery_page(
            after_seq=cursor, limit=_DELIVERY_PAGE_SIZE + 1,
        )
        page = fetched[:_DELIVERY_PAGE_SIZE]
        last_seq = int(page[-1]["delivery_seq"]) if page else cursor
        # The agent still reads speech in timeline order. The acknowledgment
        # stays in commit order, so a late old-start segment cannot disappear.
        page.sort(key=lambda seg: (float(seg.get("start_s") or 0.0), str(seg.get("id") or "")))
        return page, len(fetched) > _DELIVERY_PAGE_SIZE, last_seq

    def request_note_adjustment(self, text: str) -> Future:
        """Queue an explicit request on the same worker as periodic passes."""
        if not isinstance(text, str) or not text.strip() or len(text) > _NOTE_REQUEST_MAX_CHARS:
            raise ValueError(
                f"Enter a note request of 1 to {_NOTE_REQUEST_MAX_CHARS} characters.")
        with self._lock:
            if (self._consolidating or self._stop_event.is_set()
                    or self._thread is None or not self._thread.is_alive()):
                raise RuntimeError("The note agent is not available.")
            if len(self._note_requests) >= _MAX_QUEUED_NOTE_REQUESTS:
                raise RuntimeError("The note agent already has several requests queued.")
            future = Future()
            self._note_requests.append((text.strip(), future))
        self._wake.set()
        return future

    def _cancel_note_requests(self) -> None:
        with self._lock:
            queued = list(self._note_requests)
            self._note_requests.clear()
        for _, future in queued:
            if not future.done():
                future.set_result(AgentResult(ok=False, error="Meeting agent stopped."))

    def _fire_note_request(self) -> None:
        with self._lock:
            if not self._note_requests:
                return
            text, future = self._note_requests.popleft()
        if not future.set_running_or_notify_cancel():
            return
        try:
            if self._stop_event.is_set() or self._consolidating:
                raise RuntimeError("Meeting agent stopped.")
            if not self._agent.is_healthy():
                raise RuntimeError("The note agent is offline.")
            # Requests may target any existing block, including older notes.
            # Do not advance automatic transcript watermarks for this pass.
            segments = self._engine.get_transcript()
            payload = self._build_payload(segments, False, is_notes=True)
            if payload is None:
                raise RuntimeError("No active meeting notes.")
            payload.state_snapshot["note_adjustment_request"] = text
            result = self._agent.checkpoint(payload)
        except Exception as exc:
            logger.exception("Note adjustment request failed")
            result = AgentResult(ok=False, error=str(exc))
        future.set_result(result)

    def notify_guidance(self) -> None:
        """Review human context promptly, including during a quiet meeting."""
        with self._lock:
            self._guidance_pending = True
            self._notes_guidance_pending = True
            self._guidance_generation += 1
            # Fresh human input gets one immediate attempt even when an
            # earlier automatic pass entered provider backoff.
            self._retry_not_before = 0.0
            self._notes_retry_not_before = 0.0
            self._pending_segments += 1
        self._wake.set()

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
            self.run_due()
        logger.debug("Checkpoint scheduler loop exited")

    def run_due(self) -> None:
        """Run one scheduler tick; the worker and deterministic replay share it."""
        if self._stop_event.is_set():
            return
        if self._consolidating:
            return
        self._fire_note_request()
        if self._stop_event.is_set() or self._consolidating:
            return
        # Keep pending segments and guidance intact during warm-up. The
        # meeting clock excludes pauses and survives scheduler restarts.
        if self._meeting_elapsed_s() < _INITIAL_CONTEXT_S:
            return
        with self._lock:
            if self._note_requests:
                self._wake.set()
                return
            pending = self._pending_segments
        if pending <= 0:
            self._maybe_fire_notes()
            self._maybe_fire_polish()
            return
        if self._checkpoint_due(pending):
            self._fire()

    def _meeting_elapsed_s(self) -> float:
        clock = getattr(self._engine, "clock", None)
        if clock is not None:
            return clock.now_s()
        return self._monotonic() - self._started_mono

    def _checkpoint_due(self, pending: int) -> bool:
        """Whether ``pending`` new segments warrant a card checkpoint now."""
        elapsed = self._monotonic() - self._last_fire_mono
        if self._monotonic() < self._retry_not_before:
            return False
        if self._guidance_pending:
            return True
        if not self._sent_starts:
            return True
        if elapsed < self._min_interval_s:
            return False
        return elapsed >= self._interval_for(pending) or self._detect_topic_shift()

    def _detect_topic_shift(self) -> bool:
        """Compare the last two 60s transcript windows for a topic shift."""
        now_mono = self._monotonic()
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
        semantic = self._semantic_topic_shift(older_text, newer_text)
        if semantic is not None:
            return semantic
        union = older_words | newer_words
        jaccard = len(older_words & newer_words) / len(union) if union else 1.0
        if jaccard < _SHIFT_JACCARD_THRESHOLD:
            logger.info(
                "Topic shift detected (jaccard=%.3f); firing checkpoint early",
                jaccard,
            )
            return True
        return False

    def _semantic_topic_shift(self, older_text: str,
                              newer_text: str) -> Optional[bool]:
        """Ask the semantic judge; ``None`` hands the decision to the lexical rule.

        Runs on the scheduler worker, outside the state-store lock, and is
        bounded by the judge's own timeout. Any exception is treated as
        "no answer" so a remote outage can never stall checkpoints.
        """
        judge = self._topic_judge
        if judge is None:
            return None
        try:
            probability = judge(older_text, newer_text)
        except Exception:
            logger.exception("Semantic topic-shift judge failed; using Jaccard")
            return None
        if probability is None:
            return None
        shifted = float(probability) >= _SHIFT_SEMANTIC_THRESHOLD
        if shifted:
            logger.info(
                "Topic shift detected (semantic p=%.2f); firing checkpoint early",
                probability,
            )
        return shifted

    def _build_payload(self, segments: List[Dict[str, Any]],
                       is_consolidation: bool,
                       is_polish: bool = False,
                       is_notes: bool = False) -> Optional[CheckpointPayload]:
        store = getattr(self._engine, "store", None)
        if store is None:
            return None
        snapshot = store.snapshot()
        if not (is_consolidation or is_polish or is_notes):
            # Carry exact earlier lines as context without advancing their
            # delivery cursor again. Later speech can disambiguate an ASR name.
            supplied_ids = {seg.get("id") for seg in segments}
            try:
                recent = self._engine.get_transcript(after_start_s=self._fetch_cursor_s())
            except Exception:
                logger.exception("Recent transcript context fetch failed")
                recent = []
            snapshot["recent_transcript_context"] = [
                seg for seg in recent if seg.get("id") not in supplied_ids
            ][-24:]
        return CheckpointPayload(
            request_id=uuid.uuid4().hex,
            state_snapshot=snapshot,
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
            guidance = self._guidance_pending
            self._guidance_pending = False
        # Mark the fire time at the start of the run so work that becomes due
        # while the checkpoint executes fires immediately after completion.
        self._last_fire_mono = self._monotonic()

        try:
            fetched, more_delivery, delivery_seq = self._fetch_for_checkpoint(guidance)
        except Exception as exc:
            logger.exception("Checkpoint transcript fetch failed")
            self._checkpoint_failed(claimed, guidance, str(exc))
            return
        segments = self._checkpoint_segments(fetched, guidance)
        if not segments and not guidance:
            return

        # Only the agent can judge which speech deserves a capture. Seeding
        # cards with transcript snippets promotes filler into insights and
        # anchors the model on it before the model has read the context.
        payload = self._build_payload(segments, is_consolidation=False)
        if payload is None:
            if guidance:
                self.notify_guidance()
            with self._lock:
                self._pending_segments += claimed
            return
        logger.info(
            "Firing checkpoint %s (%d segments, %d notified, "
            "consecutive_failures=%d)",
            payload.request_id, len(segments), claimed,
            self._consecutive_failures,
        )
        result = self._run_agent_pass(payload, "Agent checkpoint raised")
        if not result.ok:
            self._checkpoint_failed(
                claimed, guidance,
                result.error or "checkpoint failed",
                request_id=payload.request_id,
            )
            return

        if self._delivery_enabled and not guidance:
            try:
                self._engine.advance_transcript_delivery_cursor("cards", delivery_seq)
            except Exception as exc:
                logger.exception("Could not save checkpoint transcript cursor")
                self._record_failure(claimed, str(exc), request_id=payload.request_id)
                return
            self._card_cursor_seq = delivery_seq
        self._record_checkpoint_success(payload.request_id, segments, result)
        self._maybe_fire_notes()
        self._maybe_fire_polish()
        if more_delivery:
            self.notify_segments(1)

    def _fetch_for_checkpoint(
        self, guidance: bool,
    ) -> tuple[List[Dict[str, Any]], bool, int]:
        """The checkpoint's transcript, whether more is queued, and its ack point.

        Guidance re-reads the whole meeting; otherwise the durable delivery
        ledger is paged, or the time-window cursor for older engines.
        """
        delivery_seq = self._card_cursor_seq
        if self._delivery_enabled and not guidance:
            return self._delivery_page("cards")
        fetched = self._engine.get_transcript(
            after_start_s=-1.0 if guidance else self._fetch_cursor_s()
        )
        return fetched, False, delivery_seq

    def _checkpoint_segments(self, fetched: List[Dict[str, Any]],
                             guidance: bool) -> List[Dict[str, Any]]:
        if self._delivery_enabled and not guidance:
            return fetched
        if guidance:
            # Segments are read with term corrections applied, so a guidance
            # pass shows the agent the corrected recent transcript.
            return list(fetched)[-_GUIDANCE_MAX_SEGMENTS:]
        return [
            seg for seg in fetched
            if str(seg.get("id") or "") not in self._sent_starts
        ]

    def _run_agent_pass(self, payload: CheckpointPayload,
                        failure_log: str) -> AgentResult:
        """``checkpoint`` on the agent; an exception becomes a failed result."""
        try:
            return self._agent.checkpoint(payload)
        except Exception as exc:
            logger.exception(failure_log)
            return AgentResult(ok=False, error=str(exc))

    def _checkpoint_failed(self, claimed: int, guidance: bool, error: str,
                           request_id: str = "") -> None:
        """Requeue guidance and record a failed card checkpoint."""
        if guidance:
            self.notify_guidance()
        self._record_failure(claimed, error, request_id=request_id)

    def _record_checkpoint_success(self, request_id: str,
                                   segments: List[Dict[str, Any]],
                                   result: AgentResult) -> None:
        self._consecutive_failures = 0
        self._mark_sent(segments)
        self._set_online(True)
        logger.info(
            "Checkpoint %s done: %d/%d ops applied",
            request_id, _applied_count(result), len(result.op_results),
        )
        self._successful_checkpoints += 1
        if _revised_transcript(result):
            with self._lock:
                self._notes_guidance_pending = True
                self._guidance_generation += 1

    def _background_blocked(self) -> bool:
        """Whether background passes must wait: ending, stopped, or human input queued."""
        return (
            self._consolidating
            or self._stop_event.is_set()
            or self._interactive_pending()
        )

    def _maybe_fire_polish(self) -> None:
        """Review transcript text on count/time cadence, including during silence."""
        if self._background_blocked():
            return
        if not self._polish_due():
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
        # Fill from the newest speech, then restore chronological order.
        recent = list(reversed(segments[-POLISH_MAX_SEGMENTS:]))
        segments = list(reversed(polish_blocks(recent)[0]))
        payload = self._build_payload(segments, is_consolidation=False, is_polish=True)
        if payload is None:
            return
        logger.info(
            "Firing transcript polish %s over %d segments",
            payload.request_id, len(segments),
        )
        result = self._run_agent_pass(payload, "Agent polish raised")
        self._last_polish_mono = self._monotonic()
        # Count attempted work too; retries wait for the next cadence.
        self._polish_checkpoint_mark = self._successful_checkpoints
        self._polish_retry_pending = not result.ok
        if not result.ok:
            logger.warning(
                "Polish %s failed: %s",
                payload.request_id, result.error or "unknown",
            )
            return
        if _revised_transcript(result):
            self.notify_guidance()
        logger.info(
            "Polish %s done: %d/%d ops applied",
            payload.request_id, _applied_count(result), len(result.op_results),
        )

    def _polish_due(self) -> bool:
        since_mark = self._successful_checkpoints - self._polish_checkpoint_mark
        if since_mark <= 0 and not self._polish_retry_pending:
            return False
        now = self._monotonic()
        due_by_count = since_mark >= self._pace("polish_every_n", _POLISH_EVERY_N_CHECKPOINTS)
        if self._last_polish_mono:
            min_interval = self._pace("polish_min_interval_s", _POLISH_MIN_INTERVAL_S)
            due_by_time = now - self._last_polish_mono >= min_interval
        else:
            due_by_time = now - self._last_fire_mono >= _POLISH_INITIAL_DELAY_S
        return due_by_count or due_by_time

    def _interactive_pending(self) -> bool:
        """Yield background work to human input waiting on the shared worker."""
        with self._lock:
            return bool(self._note_requests or self._guidance_pending)

    def _notes_interval_s(self) -> float:
        return self._pace("notes_min_interval_s", _NOTES_MIN_INTERVAL_S)

    def _maybe_fire_notes(self) -> None:
        """Run the dedicated note-taker pass when due.

        Failures are logged and never counted toward checkpoint health — the
        notes page simply catches up on the next pass, because a failed
        batch's segments are not marked as consumed.
        """
        if self._background_blocked():
            return
        if self._monotonic() < self._notes_retry_not_before:
            return
        with self._lock:
            guidance = self._notes_guidance_pending
            generation = self._guidance_generation
        if not self._notes_due(guidance):
            return
        if not self._agent.is_healthy():
            return
        notes_pass = self._prepare_notes_pass(guidance, generation)
        if notes_pass is None:
            return
        payload = notes_pass.payload
        logger.info(
            "Firing note-taker pass %s over %d segments",
            payload.request_id, len(notes_pass.segments),
        )
        result = self._run_agent_pass(payload, "Agent note-taker pass raised")
        # Advance the cadence bookkeeping whether or not the pass succeeded
        # so a failing core cannot hot-loop notes calls; only a success
        # marks the batch consumed, leaving failures to be re-covered.
        self._last_notes_mono = self._monotonic()
        self._notes_checkpoint_mark = self._successful_checkpoints
        if result.ok:
            self._finish_notes_pass(notes_pass, result)
        else:
            self._fail_notes_pass(notes_pass, result)

    def _notes_due(self, guidance: bool) -> bool:
        since_mark = self._successful_checkpoints - self._notes_checkpoint_mark
        due_by_count = since_mark >= self._pace("notes_every_n", _NOTES_EVERY_N_CHECKPOINTS)
        due_by_time = (
            self._last_notes_mono > 0.0
            and (self._monotonic() - self._last_notes_mono) >= self._notes_interval_s()
        ) or (self._last_notes_mono == 0.0 and self._successful_checkpoints >= 1)
        resume_due = self._notes_resume_pending and (
            self._last_notes_mono == 0.0
            or self._monotonic() - self._last_notes_mono >= self._notes_interval_s()
        )
        return guidance or due_by_count or due_by_time or resume_due

    def _prepare_notes_pass(self, guidance: bool,
                            generation: int) -> Optional[_NotesPass]:
        """Fetch the notes batch and build its payload; None when there is nothing to do."""
        try:
            fetched, more_delivery, delivery_seq = self._fetch_for_notes(guidance)
        except Exception:
            logger.exception("Notes transcript fetch failed")
            return None
        segments = self._notes_segments(fetched, guidance)
        if not segments and not guidance:
            return None
        payload = self._build_payload(
            segments, is_consolidation=False, is_notes=True,
        )
        if payload is None:
            return None
        if guidance:
            # Clear before the network call so guidance arriving during it
            # remains queued for the next pass.
            with self._lock:
                if generation == self._guidance_generation:
                    self._notes_guidance_pending = False
            payload.state_snapshot["notes_review_requested"] = True
        return _NotesPass(payload, segments, guidance, more_delivery, delivery_seq)

    def _fetch_for_notes(
        self, guidance: bool,
    ) -> tuple[List[Dict[str, Any]], bool, int]:
        delivery_seq = self._notes_cursor_seq
        # New installations page the durable insertion ledger. Older engine
        # fakes retain the time-window API for compatibility.
        if not guidance and self._notes_max_sent_start_s >= 0.0:
            cursor = max(-1.0, self._notes_max_sent_start_s - _REFETCH_WINDOW_S)
        else:
            cursor = -1.0
        if self._delivery_enabled and not guidance:
            return self._delivery_page("notes")
        return self._engine.get_transcript(after_start_s=cursor), False, delivery_seq

    def _notes_segments(self, fetched: List[Dict[str, Any]],
                        guidance: bool) -> List[Dict[str, Any]]:
        """Unsent segments, oldest first; a guidance review takes the newest instead."""
        if guidance:
            return list(fetched)[-_NOTES_MAX_SEGMENTS:]
        segments = [
            seg for seg in fetched
            if self._delivery_enabled
            or str(seg.get("id") or "") not in self._notes_sent_starts
        ]
        return segments[:_NOTES_MAX_SEGMENTS]

    def _finish_notes_pass(self, notes_pass: _NotesPass,
                           result: AgentResult) -> None:
        if self._delivery_enabled and not notes_pass.guidance:
            try:
                self._engine.advance_transcript_delivery_cursor(
                    "notes", notes_pass.delivery_seq)
            except Exception:
                logger.exception("Could not save note-taker transcript cursor")
                self._defer_notes_retry()
                return
            self._notes_cursor_seq = notes_pass.delivery_seq
            self._notes_resume_pending = notes_pass.more_delivery
        logger.info(
            "Note-taker pass %s done: %d/%d ops applied",
            notes_pass.payload.request_id, _applied_count(result),
            len(result.op_results),
        )
        if notes_pass.guidance:
            # A bounded review can skip older, still-unprocessed speech.
            # Never advance the automatic notes cursor past that backlog.
            return
        self._mark_notes_sent(notes_pass.segments)
        if notes_pass.more_delivery:
            self._wake.set()

    def _fail_notes_pass(self, notes_pass: _NotesPass,
                         result: AgentResult) -> None:
        self._defer_notes_retry()
        if notes_pass.guidance:
            with self._lock:
                self._notes_guidance_pending = True
        logger.warning(
            "Note-taker pass %s failed: %s",
            notes_pass.payload.request_id, result.error or "unknown",
        )

    def _defer_notes_retry(self) -> None:
        self._notes_retry_not_before = self._monotonic() + self._notes_interval_s()

    def _mark_notes_sent(self, segments: List[Dict[str, Any]]) -> None:
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
        self._prune_notes_sent()

    def _prune_notes_sent(self) -> None:
        """Forget sent notes segments that no future fetch can return."""
        prune_cursor = max(-1.0, self._notes_max_sent_start_s - _REFETCH_WINDOW_S)
        self._notes_sent_starts = {
            seg_id: start_s
            for seg_id, start_s in self._notes_sent_starts.items()
            if start_s > prune_cursor
        }

    def _record_failure(
        self, claimed: int, error: str, request_id: str = "",
    ) -> None:
        """Restore claimed work and schedule a bounded health retry.

        Args:
            claimed: Segment notifications that this failed fire consumed.
            error: Failure text from the agent or transcript fetch.
            request_id: Checkpoint id when the agent ran; empty on a
                fetch failure before a payload exists.
        """
        with self._lock:
            self._pending_segments += claimed
        self._consecutive_failures += 1
        delay = _RETRY_BACKOFF_S[min(
            self._consecutive_failures - 1, len(_RETRY_BACKOFF_S) - 1
        )]
        self._retry_not_before = self._monotonic() + delay
        logger.warning(
            "Checkpoint failed (%d consecutive, claimed=%d request_id=%s); "
            "retrying in %.0fs: %s",
            self._consecutive_failures, claimed, request_id or "-",
            delay, error,
        )
        if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            logger.error(
                "Intelligence declared offline after %d consecutive "
                "checkpoint failures (request_id=%s)",
                self._consecutive_failures, request_id or "-",
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

    def _stop_inflight_checkpoint(self) -> bool:
        """Let a rolling checkpoint still in flight finish, or cancel it.

        Two agent runs sharing one core would interleave tool calls and
        corrupt a final pass; a stale rolling checkpoint is far less damaging
        than that, so a final pass starts only once the worker is gone.

        Returns:
            True when no rolling checkpoint is running any more.
        """
        thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        thread.join(timeout=_CONSOLIDATION_JOIN_S)
        if thread.is_alive():
            logger.warning(
                "In-flight checkpoint still running; canceling before the "
                "final pass"
            )
            try:
                self._agent.cancel()
            except Exception:
                logger.exception("Agent cancel raised")
            thread.join(timeout=_CONSOLIDATION_JOIN_S)
        return not thread.is_alive()

    def _revoke_agent_writes(self) -> None:
        """Close the engine agent-write gate before a terminal consolidation."""
        revoke = getattr(self._engine, "revoke_agent_writes", None)
        if callable(revoke):
            try:
                revoke()
            except Exception:
                logger.exception("Engine revoke_agent_writes raised")

    def run_consolidation(
        self,
        timeout_s: Optional[float] = None,
        *,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> ConsolidationOutcome:
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
            timeout_s: Hard wall for the worker join. Defaults to
                ``CONSOLIDATION_TIMEOUT_CAP_S``. The sidecar also fails
                earlier after ``CONSOLIDATION_STALL_S`` of silence (no Pi
                ``subscribe`` events or tool-bridge calls).
            progress_cb: Optional callback for status messages from those
                session events (``thinking_delta``, tool starts, …).

        Returns:
            Structured terminal outcome for UI/finalization persistence.
        """
        self._consolidating = True
        self.stop()
        skipped = self._consolidation_precheck()
        if skipped is not None:
            self._revoke_agent_writes()
            return skipped
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

        if timeout_s is None:
            from meeting.agent.base import CONSOLIDATION_TIMEOUT_CAP_S

            timeout_s = CONSOLIDATION_TIMEOUT_CAP_S
        result = self._consolidate(payload, len(segments), timeout_s, progress_cb)
        outcome = _consolidation_outcome(result, timeout_s)

        # Close the write gate before repair/return so only this pass's
        # already-applied ops remain authoritative.
        self._revoke_agent_writes()
        self._repair_after_consolidation(segments)
        return outcome

    def _consolidation_precheck(self) -> Optional[ConsolidationOutcome]:
        """Why the final pass cannot run, or None when it can."""
        if not self._stop_inflight_checkpoint():
            logger.error(
                "Checkpoint worker did not stop; skipping the consolidation "
                "pass to avoid two concurrent agent runs"
            )
            return ConsolidationOutcome(
                status="failed",
                message=(
                    "Final insights were skipped because a previous "
                    "checkpoint was still running."
                ),
            )
        if not self._agent.is_healthy():
            logger.warning("Agent core unhealthy; skipping consolidation pass")
            return ConsolidationOutcome(
                status="unavailable",
                message=(
                    "AI insights are offline; final insights "
                    "could not run."
                ),
            )
        return None

    def _consolidate(
        self,
        payload: CheckpointPayload,
        segment_count: int,
        timeout_s: float,
        progress_cb: Optional[Callable[[str], None]],
    ) -> Optional[AgentResult]:
        """Run ``consolidate`` with progress wired up; None when it timed out."""
        setter = getattr(self._agent, "set_progress_callback", None)
        if callable(setter):
            setter(progress_cb)
        if progress_cb is not None:
            try:
                progress_cb(
                    "Synthesizing executive summary, key decisions, and action "
                    f"items over {segment_count} segments..."
                )
            except Exception:
                logger.exception("Consolidation progress callback failed")

        logger.info(
            "Running consolidation %s over %d segments (timeout %.0fs)",
            payload.request_id, segment_count, timeout_s,
        )

        def _detach_progress() -> None:
            if callable(setter):
                setter(None)

        def _on_timeout() -> None:
            # Mute progress, then revoke before cancel, so a worker that
            # ignores cancel can neither report nor land late mutations.
            _detach_progress()
            self._revoke_agent_writes()

        try:
            return run_agent_call(
                lambda: self._agent.consolidate(payload),
                cancel=self._agent.cancel,
                timeout_s=timeout_s,
                name="meeting-consolidation",
                on_timeout=_on_timeout,
            )
        finally:
            _detach_progress()

    def _repair_after_consolidation(self, segments: List[Dict[str, Any]]) -> None:
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

    def run_final_polish(
        self,
        timeout_s: float = POLISH_TIMEOUT_S,
        *,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> ConsolidationOutcome:
        """Run a blocking transcript-text polish over the final segments.

        Stops periodic firing first. Does not revoke agent writes so
        :meth:`run_consolidation` can follow on the same end path.

        Args:
            timeout_s: Maximum seconds to wait per cleanup block.
            progress_cb: Optional callback ``cb(detail_msg, current_block, total_blocks)``.

        Returns:
            Structured outcome. ``completed`` means the agent returned ok
            (including zero ops); failures are non-fatal for consolidation.
        """
        self._consolidating = True
        self.stop()
        if not self._stop_inflight_checkpoint():
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
        store = getattr(self._engine, "store", None)
        if store is None:
            return ConsolidationOutcome(
                status="failed",
                message="Meeting state was unavailable for transcript cleanup.",
            )
        error = polish_transcript(
            self._agent, store, segments,
            timeout_s=timeout_s, progress_cb=progress_cb,
        )
        if error:
            return ConsolidationOutcome(status="failed", message=error)
        return ConsolidationOutcome(
            status="completed",
            message="Transcript cleanup is ready.",
        )
