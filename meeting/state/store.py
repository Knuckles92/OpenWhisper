"""Single-writer meeting-state store: locking, seq numbering, audit, fan-out.

Every mutation — agent checkpoint output, human dashboard action, diarizer
relabel, host undo — flows through ``MeetingStateStore.apply``. That single
choke point is what makes attribution, the audit trail, human-overrides-agent
protection, and multi-client broadcast all consistent by construction.

Two ordering rules keep concurrent writers honest:

* Subscribers are notified **while the state lock is held**, so the fan-out
  order can never invert the seq order the batches were assigned. Subscribers
  must therefore be non-blocking (the web hub only marshals the batch onto its
  event loop).
* Write-through persistence runs **outside** the state lock, drained from a
  FIFO queue under a dedicated persistence lock. Queue order is seq order
  because entries are appended under the state lock, so the stored snapshot
  still advances monotonically — while readers (``snapshot``) are never blocked
  by a slow SQLite transaction.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from meeting.interfaces import OpResult
from meeting.state.patches import SEGMENT_OPS, OpContext, apply_ops
from meeting.state.schema import MeetingState

logger = logging.getLogger(__name__)

#: Applies a validated segment op (``effect`` describes the intent) and
#: returns the inverse op dict, or raises on failure.
SegmentHandler = Callable[[OpResult], Optional[Dict[str, Any]]]

Subscriber = Callable[[int, List[OpResult]], None]

#: One queued write-through: (snapshot, applied results, actor_type, actor_id).
PersistEntry = Tuple[Dict[str, Any], List[OpResult], str, Optional[str]]


class MeetingStateStore:
    """Thread-safe owner of one meeting's ``MeetingState`` document."""

    def __init__(
        self,
        state: MeetingState,
        repository: Optional[Any] = None,
        segment_handler: Optional[SegmentHandler] = None,
        segment_exists: Optional[Callable[[str], bool]] = None,
        segment_pinned: Optional[Callable[[str], bool]] = None,
    ) -> None:
        """Args:
            state: The state document this store owns.
            repository: Optional ``MeetingRepository`` for write-through
                persistence and the audit trail.
            segment_handler: Applies segment-log ops (speaker reassignment);
                required for ``reassign_segment_speaker`` to succeed.
            segment_exists: Predicate validating evidence segment ids.
            segment_pinned: Predicate reporting whether a segment already
                carries a human speaker pin, so automated relabels cannot
                revert a human correction.
        """
        self._state = state
        self._repository = repository
        self._segment_handler = segment_handler
        self._segment_exists = segment_exists
        self._segment_pinned = segment_pinned
        self._lock = threading.RLock()
        self._subscribers: List[Subscriber] = []
        self._persist_lock = threading.Lock()
        self._persist_queue: Deque[PersistEntry] = deque()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def meeting_id(self) -> str:
        return self._state.meeting_id

    @property
    def seq(self) -> int:
        with self._lock:
            return self._state.seq

    def snapshot(self) -> Dict[str, Any]:
        """Full state as a fresh, serialization-safe dict."""
        with self._lock:
            return self._state.to_dict()

    def with_state(self, fn: Callable[[MeetingState], Any]) -> Any:
        """Run a read-only function against the live state under the lock."""
        with self._lock:
            return fn(self._state)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def apply(self, actor_type: str, actor_id: Optional[str],
              ops: List[Dict[str, Any]]) -> List[OpResult]:
        """Validate and apply ops; persist, audit, and broadcast the outcome.

        Args:
            actor_type: ``agent`` | ``user`` | ``host`` | ``system``.
            actor_id: Participant id / agent name for attribution.
            ops: Op dicts from the shared vocabulary.

        Returns:
            One ``OpResult`` per op. Rejected ops carry ``reason``; applied
            ops carry their assigned ``seq`` and broadcastable ``effect``.
        """
        with self._lock:
            ctx = OpContext(actor_type, actor_id, self._segment_exists,
                            self._segment_pinned)
            results = apply_ops(self._state, ops, ctx)

            for result in results:
                if not result.ok:
                    continue
                if result.op.get("op") in SEGMENT_OPS:
                    self._apply_segment_op(result)
                    if not result.ok:
                        continue
                self._state.seq += 1
                result.seq = self._state.seq

            applied = [r for r in results if r.ok]
            if applied:
                if self._repository is not None:
                    # Snapshot under the lock so the persisted document matches
                    # this batch exactly; the write itself happens below.
                    self._persist_queue.append(
                        (self._state.to_dict(), applied, actor_type, actor_id)
                    )
                # Notified under the lock: two concurrent writers can never
                # hand their batches to subscribers out of seq order.
                self._notify(max(r.seq or 0 for r in applied), applied)

        if applied:
            self._drain_persist_queue()
        return results

    def undo(self, event_seq: int, actor_id: Optional[str]) -> List[OpResult]:
        """Host undo: apply the recorded inverse of a past event.

        Args:
            event_seq: The ``seq`` of the event to revert.
            actor_id: The host participant id, for attribution.

        Returns:
            The results of applying the inverse op (empty list when the event
            is unknown or has no recorded inverse).
        """
        if self._repository is None:
            return []
        event = self._repository.get_event(self._state.meeting_id, event_seq)
        if not event or not event.get("inverse"):
            return []
        # Inverse ops carry force flags honored only for system actors, so
        # they bypass protection without weakening it for anyone else.
        return self.apply("system", actor_id, [event["inverse"]])

    # ------------------------------------------------------------------
    # Fan-out
    # ------------------------------------------------------------------

    def subscribe(self, cb: Subscriber) -> None:
        with self._lock:
            if cb not in self._subscribers:
                self._subscribers.append(cb)

    def unsubscribe(self, cb: Subscriber) -> None:
        with self._lock:
            if cb in self._subscribers:
                self._subscribers.remove(cb)

    def _notify(self, seq: int, applied: List[OpResult]) -> None:
        """Hand one applied batch to every subscriber, in seq order.

        Called with ``self._lock`` held — that is what keeps the fan-out order
        equal to the seq order. Subscribers must not block.

        Args:
            seq: The highest seq assigned within this batch.
            applied: The batch's successful results.
        """
        for cb in list(self._subscribers):
            try:
                cb(seq, applied)
            except Exception:
                logger.exception("State subscriber raised")

    # ------------------------------------------------------------------
    # Write-through persistence
    # ------------------------------------------------------------------

    def _drain_persist_queue(self) -> None:
        """Flush queued batches to the repository without holding the state lock.

        Entries were queued under the state lock, so FIFO order is seq order;
        the persistence lock preserves that order across concurrent writers
        while leaving ``snapshot`` readers free during the SQLite transaction.
        Returns once this thread's own batch has been written.
        """
        if self._repository is None:
            return
        with self._persist_lock:
            while True:
                try:
                    snapshot, applied, actor_type, actor_id = \
                        self._persist_queue.popleft()
                except IndexError:
                    return
                try:
                    self._repository.on_ops_applied(
                        self._state.meeting_id, snapshot,
                        applied, actor_type, actor_id,
                    )
                except Exception:
                    logger.exception("State persistence failed (meeting %s)",
                                     self._state.meeting_id)

    # ------------------------------------------------------------------
    # Segment ops
    # ------------------------------------------------------------------

    def _apply_segment_op(self, result: OpResult) -> None:
        """Route a validated segment op to the segment handler."""
        if self._segment_handler is None:
            result.ok = False
            result.reason = "segments_unavailable"
            return
        try:
            result.inverse = self._segment_handler(result)
        except Exception:
            logger.exception("Segment op failed: %s", result.op)
            result.ok = False
            result.reason = "segment_error"
