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
* Write-through persistence completes **before** live state is replaced or
  subscribers are notified. A database failure therefore rejects the batch
  without exposing state that cannot survive a restart.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from meeting.interfaces import OpResult
from meeting.state.patches import SEGMENT_OPS, OpContext, apply_ops
from meeting.state.schema import MeetingState

logger = logging.getLogger(__name__)

#: Applies a validated segment op (``effect`` describes the intent) and
#: returns the inverse op dict, or raises on failure.
SegmentHandler = Callable[[OpResult], Optional[Dict[str, Any]]]

Subscriber = Callable[[int, List[OpResult]], None]

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
            # Applying to a round-tripped candidate makes repository failure
            # a real rejection instead of leaving an unpersisted live state.
            candidate = MeetingState.from_dict(self._state.to_dict())
            ctx = OpContext(actor_type, actor_id, self._segment_exists,
                            self._segment_pinned)
            results = apply_ops(candidate, ops, ctx)

            for result in results:
                if not result.ok:
                    continue
                if result.op.get("op") in SEGMENT_OPS:
                    self._apply_segment_op(result)
                    if not result.ok:
                        continue
                candidate.seq += 1
                result.seq = candidate.seq

            applied = [r for r in results if r.ok]
            if applied:
                if self._repository is not None:
                    try:
                        self._repository.on_ops_applied(
                            self._state.meeting_id, candidate.to_dict(), applied,
                            actor_type, actor_id,
                        )
                    except Exception:
                        logger.exception(
                            "State persistence failed (meeting %s)",
                            self._state.meeting_id,
                        )
                        for result in applied:
                            result.ok = False
                            result.reason = "persistence_error"
                            result.seq = None
                        return results
                self._state = candidate
                # Notified under the lock: two concurrent writers can never
                # hand their batches to subscribers out of seq order.
                self._notify(max(r.seq or 0 for r in applied), applied)
        return results

    def update_runtime_fields(self, **fields: Any) -> bool:
        """Persist lifecycle/status fields before making them observable."""
        with self._lock:
            candidate = MeetingState.from_dict(self._state.to_dict())
            for key, value in fields.items():
                if not hasattr(candidate, key):
                    raise AttributeError(key)
                if key == "finalization":
                    from meeting.state.schema import FinalizationState

                    value = FinalizationState.coerce(
                        value,
                        cloud_enabled=candidate.cloud_enabled,
                        meeting_status=(
                            fields.get("status", candidate.status)
                        ),
                    )
                setattr(candidate, key, value)
            if self._repository is not None:
                try:
                    self._repository.persist_state(
                        self._state.meeting_id, candidate.to_dict()
                    )
                except Exception:
                    logger.exception(
                        "Runtime state persistence failed (meeting %s)",
                        self._state.meeting_id,
                    )
                    return False
            self._state = candidate
            return True

    def replace_document(self, state: MeetingState) -> None:
        """Replace the in-memory document after an out-of-band persistence write.

        Used when the repository rewrites evidence ids during a final
        transcript replace so the live store matches SQLite.

        Args:
            state: The document already persisted for this meeting.
        """
        with self._lock:
            self._state = state

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
        with self._lock:
            event = self._repository.get_event(
                self._state.meeting_id, event_seq
            )
            if not event or not event.get("inverse"):
                return []
            already_undone = getattr(
                self._repository, "event_is_undone", lambda *_: False
            )(self._state.meeting_id, event_seq)
            if already_undone:
                return []
            # The marker is trusted only for system actors by the repository.
            # It makes one undo request idempotent while the inverse's own
            # audit event remains undoable as an explicit redo.
            inverse = dict(event["inverse"])
            inverse["_undo_event_seq"] = event_seq
            # Inverse ops carry force flags honored only for system actors, so
            # they bypass protection without weakening it for anyone else.
            return self.apply("system", actor_id, [inverse])

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
