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
from copy import copy, deepcopy
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from meeting.interfaces import OpResult
from meeting.state.patches import SEGMENT_OPS, OpContext, apply_ops
from meeting.state.schema import MeetingState

logger = logging.getLogger(__name__)

#: Applies a validated segment op (``effect`` describes the intent) and
#: returns the inverse op dict, or raises on failure.
SegmentHandler = Callable[[OpResult], Optional[Dict[str, Any]]]

Subscriber = Callable[[int, List[OpResult]], None]

#: Bulk ``{segment_id: speaker_pinned}`` for the ids that exist.
SegmentLookup = Callable[[Iterable[str]], Dict[str, bool]]

_SEGMENT_ID_KEYS = frozenset({"segment_id"})
_SEGMENT_LIST_KEYS = frozenset({"evidence", "segment_ids"})

# Patch handlers mutate these domains in place. Other fields are either read
# only or replaced on the candidate root. Include linked review/note changes
# in card edits; item_effect can revise user_notes and live_notes together.
_OP_DOMAINS = {
    **{name: ("cards", "insight_review") for name in (
        "add_item", "update_item", "remove_item", "pin_item", "unpin_item",
        "confirm_item", "review_finish", "review_answer", "citation_check",
        "invalidate_citations",
    )},
    **{name: ("insight_review",) for name in (
        "review_unavailable", "review_begin", "review_skip", "review_reopen",
    )},
    **{name: ("participants",) for name in (
        "upsert_participant", "suggest_participant_name", "rename_participant",
    )},
    **{name: ("questions",) for name in (
        "ask_question", "resolve_question", "answer_question", "dismiss_question",
        "reopen_question",
    )},
    **{name: ("custom_reports",) for name in (
        "request_custom_report", "finish_custom_report", "remove_custom_report",
    )},
    "set_topic": ("topic",),
    "publish_highlights": ("live_highlights",),
    **{name: () for name in (
        "set_rolling_summary", "set_title", "set_meeting_intent",
        "set_cloud_enabled", "voice_feedback", "reassign_segment_speaker",
        "revise_segment_text",
    )},
}


def _copy_for_ops(state: MeetingState, ops: List[Dict[str, Any]]) -> MeetingState:
    """Isolate mutated sections without round-tripping untouched reports."""
    domains = set()
    for op in ops:
        if not isinstance(op, dict):
            continue
        name = op.get("op")
        if not isinstance(name, str) or name not in _OP_DOMAINS:
            # A newly registered handler is safe until its mutation scope is
            # added here. Malformed/unknown ops retain the same rejection path.
            return deepcopy(state)
        domains.update(_OP_DOMAINS[name])
    candidate = copy(state)
    for field in domains:
        value = getattr(state, field)
        setattr(candidate, field, _copy_cards(value) if field == "cards" else deepcopy(value))
    return candidate


def _copy_cards(cards):
    """Own item objects and the containers that registered handlers mutate.

    Handlers replace evidence/review/check fields and change scalar item
    fields. Data edits replace the dict or set a top-level flag; nested data
    is read-only. Sharing those untouched payloads avoids copying whole
    report source trees for a small card edit.
    """
    result = {}
    for card, items in cards.items():
        cloned = []
        for item in items:
            candidate = copy(item)
            candidate.data = dict(item.data)
            candidate.evidence = list(item.evidence)
            candidate.review = deepcopy(item.review)
            candidate.citation_check = deepcopy(item.citation_check)
            cloned.append(candidate)
        result[card] = cloned
    return result


def _referenced_segment_ids(ops: List[Dict[str, Any]]) -> Set[str]:
    """Segment ids an op batch cites, so one query can answer them all.

    Anything missed here still goes through the per-id predicates.
    """
    ids: Set[str] = set()

    def visit(value: Any, depth: int) -> None:
        if depth > 2 or not isinstance(value, dict):
            return
        for key, item in value.items():
            if key in _SEGMENT_ID_KEYS and isinstance(item, str):
                ids.add(item)
            elif key in _SEGMENT_LIST_KEYS and isinstance(item, list):
                ids.update(sid for sid in item if isinstance(sid, str))
            elif isinstance(item, dict):
                visit(item, depth + 1)

    for op in ops:
        visit(op, 0)
    return ids


def repository_segment_lookup(
    repository: Any, meeting_id: str,
) -> Optional[SegmentLookup]:
    """Bulk segment lookup bound to one meeting, or None if unsupported."""
    flags = getattr(repository, "segment_flags", None)
    if not callable(flags):
        return None
    return lambda segment_ids: flags(meeting_id, segment_ids)

class MeetingStateStore:
    """Thread-safe owner of one meeting's ``MeetingState`` document."""

    def __init__(
        self,
        state: MeetingState,
        repository: Optional[Any] = None,
        segment_handler: Optional[SegmentHandler] = None,
        segment_exists: Optional[Callable[[str], bool]] = None,
        segment_pinned: Optional[Callable[[str], bool]] = None,
        segment_lookup: Optional[SegmentLookup] = None,
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
            segment_lookup: Optional bulk form of both predicates. When set,
                each batch answers its cited ids with one query; the
                predicates still decide anything the batch did not cite.
        """
        self._state = state
        self._repository = repository
        self._segment_handler = segment_handler
        self._segment_exists = segment_exists
        self._segment_pinned = segment_pinned
        self._segment_lookup = segment_lookup
        self._lock = threading.RLock()
        self._subscribers: List[Subscriber] = []

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
            # The live document remains untouched until persistence succeeds.
            candidate = _copy_for_ops(self._state, ops)
            exists, pinned = self._batch_predicates(ops)
            ctx = OpContext(actor_type, actor_id, exists, pinned)
            # Handlers may attach nested data from an incoming op to an item.
            # Own that small payload, independent of callers retaining it.
            results = apply_ops(candidate, deepcopy(ops), ctx)

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
                        persisted = self._repository.on_ops_applied(
                            self._state.meeting_id, candidate.to_dict(), applied,
                            actor_type, actor_id,
                        )
                        if isinstance(persisted, dict):
                            candidate = self._reconcile_persisted(candidate, persisted)
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
            candidate = copy(self._state)
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
                    if value.status == "running" and candidate.finalization.status != "running":
                        from meeting.state.review import invalidate_checks
                        candidate.cards = _copy_cards(candidate.cards)
                        candidate.insight_review = deepcopy(candidate.insight_review)
                        invalidate_checks(candidate)
                setattr(candidate, key, deepcopy(value))
            if self._repository is not None:
                try:
                    persisted = self._repository.persist_state(
                        self._state.meeting_id, candidate.to_dict()
                    )
                    if isinstance(persisted, dict):
                        candidate = self._reconcile_persisted(candidate, persisted)
                except Exception:
                    logger.exception(
                        "Runtime state persistence failed (meeting %s)",
                        self._state.meeting_id,
                    )
                    return False
            self._state = candidate
            return True

    def _reconcile_persisted(self, candidate: MeetingState,
                             persisted: Dict[str, Any]) -> MeetingState:
        """Adopt canonical metadata without rebuilding the isolated document.

        Other repository implementations may transform the whole snapshot, so
        they retain the full reconciliation contract unless they opt in.
        """
        if getattr(self._repository, "state_metadata_only", False) is True:
            candidate.title = str(persisted.get("title") or "")
            return candidate
        return MeetingState.from_dict(persisted)

    def refresh_title(self) -> bool:
        """Refresh independently edited metadata without rewriting a stale snapshot."""
        if self._repository is None:
            return False
        with self._lock:
            meeting = self._repository.get_meeting(self._state.meeting_id)
            if meeting is None:
                return False
            title = meeting.get("title") or ""
            changed = title != self._state.title
            self._state.title = title
            return changed

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

    def _batch_predicates(
        self, ops: List[Dict[str, Any]],
    ) -> Tuple[Optional[Callable[[str], bool]], Optional[Callable[[str], bool]]]:
        """Segment predicates for one batch, answered from a single lookup.

        Validation sees the segment log as it was before the batch either
        way: segment ops only run after every op has been validated.
        """
        exists_fallback = self._segment_exists
        pinned_fallback = self._segment_pinned
        if self._segment_lookup is None or (
            exists_fallback is None and pinned_fallback is None
        ):
            return exists_fallback, pinned_fallback
        cited = _referenced_segment_ids(ops)
        if not cited:
            return exists_fallback, pinned_fallback
        try:
            flags = self._segment_lookup(cited)
        except Exception:
            logger.exception("Bulk segment lookup failed; checking ids one by one")
            return exists_fallback, pinned_fallback
        if not isinstance(flags, dict):
            return exists_fallback, pinned_fallback

        def exists(segment_id: str) -> bool:
            if segment_id in cited:
                return segment_id in flags
            return bool(exists_fallback(segment_id)) if exists_fallback else True

        def pinned(segment_id: str) -> bool:
            if segment_id in cited:
                return bool(flags.get(segment_id))
            return bool(pinned_fallback(segment_id)) if pinned_fallback else False

        return (
            exists if exists_fallback is not None else None,
            pinned if pinned_fallback is not None else None,
        )

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
