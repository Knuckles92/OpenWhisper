"""State-patch operations: the single vocabulary for every meeting-state change.

Both the agent core and human dashboard actions express changes as ops from
this vocabulary. ``apply_ops`` validates and applies them against a
``MeetingState``, enforcing the protection rules (human corrections are
authoritative; the agent can never overwrite human-touched content) and
producing per-op results with precomputed inverse ops for host undo.

There is deliberately no wholesale-replacement op: the agent can only make
targeted, validated changes.

Rejections are per-op, not per-batch, and rejection reasons are returned to
the agent so the next checkpoint can self-correct.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from meeting.interfaces import OpResult
from meeting.state.schema import (
    CARD_KEYS,
    CardItem,
    MeetingState,
    Participant,
    Question,
    new_id,
    now_iso,
)

logger = logging.getLogger(__name__)

# Validation limits
MAX_TEXT_LEN = 4000
MAX_TOPIC_LEN = 500
MAX_NAME_LEN = 120
MAX_SUMMARY_LEN = 8000
MAX_OPEN_QUESTIONS = 7
MAX_EVIDENCE_REFS = 20
#: Human ceilings. Dashboard clients are untrusted input, so the same caps that
#: keep the agent tidy also bound how much a guest can pile into the state
#: document (every applied op rewrites the whole snapshot to SQLite).
MAX_OPEN_QUESTIONS_HUMAN = 50
MAX_DATA_BYTES = 4096
MAX_CARD_ITEMS = 500
MAX_PARTICIPANTS = 200

#: Cards only humans may write to. The agent's system prompt says so, but the
#: Pi tool schema is open-ended, so the rule is enforced here as well.
HUMAN_ONLY_CARDS = frozenset({"user_notes"})

#: The only participant kind the agent may mint; ``me`` and ``guest`` identities
#: are established by the app and by humans joining, never inferred.
AGENT_PARTICIPANT_KIND = "others_cluster"

# Confidence thresholds for agent question resolution ("answered from audio")
RESOLVE_CONFIDENCE = 0.8
SUGGEST_CONFIDENCE = 0.4

#: Ops the agent may emit. Everything else is human/system only.
AGENT_OPS = frozenset({
    "add_item", "update_item", "remove_item",
    "set_topic", "set_rolling_summary",
    "upsert_participant", "suggest_participant_name",
    "ask_question", "resolve_question",
})

#: Ops only the agent may emit. ``resolve_question`` stamps an answer as
#: "answered from audio"; letting a human reach it would attribute their words
#: to the recording. Humans use ``answer_question`` instead. ``system`` keeps
#: access so host undo of an agent resolution still works.
AGENT_ONLY_OPS = frozenset({"resolve_question"})

#: Meeting-level metadata only the host may change, matching the host-only REST
#: routes. Guests edit card items, not the meeting's identity or its summary.
HOST_ONLY_OPS = frozenset({"set_topic", "set_rolling_summary", "set_title"})

#: Ops that target the transcript segment log rather than the state document.
#: They are validated here but applied by the store's segment handler.
SEGMENT_OPS = frozenset({"reassign_segment_speaker"})

#: The full vocabulary (human actions include everything below).
ALL_OPS = AGENT_OPS | SEGMENT_OPS | frozenset({
    "pin_item", "unpin_item", "confirm_item",
    "answer_question", "dismiss_question", "reopen_question",
    "rename_participant", "set_title", "set_cloud_enabled",
})


class OpContext:
    """Per-batch context for validation and attribution.

    Attributes:
        actor_type: ``agent`` | ``user`` | ``host`` | ``system``.
        actor_id: Participant id for humans, agent name for the agent.
        segment_exists: Optional predicate validating evidence segment ids.
        segment_pinned: Optional predicate reporting whether a segment already
            carries a human speaker pin.
    """

    def __init__(self, actor_type: str, actor_id: Optional[str],
                 segment_exists: Optional[Callable[[str], bool]] = None,
                 segment_pinned: Optional[Callable[[str], bool]] = None) -> None:
        self.actor_type = actor_type
        self.actor_id = actor_id
        self.segment_exists = segment_exists
        self.segment_pinned = segment_pinned

    @property
    def is_agent(self) -> bool:
        return self.actor_type == "agent"

    @property
    def is_human(self) -> bool:
        return self.actor_type in ("user", "host")


def _reject(op: Dict[str, Any], reason: str, **extra) -> OpResult:
    return OpResult(ok=False, op=op, reason=reason, **extra)


def _check_evidence(op: Dict[str, Any], ctx: OpContext) -> Optional[str]:
    """Validate the op's evidence list; returns a rejection reason or None."""
    evidence = op.get("evidence") or []
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE_REFS:
        return "invalid_evidence"
    if ctx.segment_exists is not None:
        for sg_id in evidence:
            if not isinstance(sg_id, str) or not ctx.segment_exists(sg_id):
                return "unknown_evidence"
    return None


def _check_data_size(data: Dict[str, Any]) -> Optional[str]:
    """Bound an item's free-form ``data`` blob.

    ``data`` reaches here from browsers and from the model, and every applied
    op rewrites the whole state snapshot to SQLite, so an unbounded blob is
    both a memory and a write-amplification problem.
    """
    try:
        if len(json.dumps(data)) > MAX_DATA_BYTES:
            return "data_too_large"
    except (TypeError, ValueError):
        return "invalid_data"
    return None


def _check_text(value: Any, max_len: int = MAX_TEXT_LEN) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return "invalid_text"
    if len(value) > max_len:
        return "text_too_long"
    return None


# ---------------------------------------------------------------------------
# Item ops
# ---------------------------------------------------------------------------

def _op_add_item(state: MeetingState, op: Dict[str, Any], ctx: OpContext) -> OpResult:
    card = op.get("card")
    if card not in CARD_KEYS:
        return _reject(op, "unknown_card")
    if ctx.is_agent and card in HUMAN_ONLY_CARDS:
        return _reject(op, "human_only_card")
    reason = _check_text(op.get("text")) or _check_evidence(op, ctx)
    if reason:
        return _reject(op, reason)
    data = op.get("data") or {}
    if not isinstance(data, dict):
        return _reject(op, "invalid_data")
    reason = _check_data_size(data)
    if reason:
        return _reject(op, reason)
    if len(state.cards[card]) >= MAX_CARD_ITEMS:
        return _reject(op, "card_full")

    item = CardItem(
        id=new_id("it"),
        card=card,
        text=op["text"].strip(),
        data=data,
        status="edited" if ctx.is_human else "proposed",
        author_type="user" if ctx.is_human else ctx.actor_type,
        author_id=ctx.actor_id,
        evidence=list(op.get("evidence") or []),
    )
    state.cards[card].append(item)
    return OpResult(
        ok=True, op=op, target_id=item.id,
        effect={"entity": "item", "item": item.to_dict()},
        inverse={"op": "remove_item", "id": item.id},
    )


def _op_update_item(state: MeetingState, op: Dict[str, Any], ctx: OpContext) -> OpResult:
    item = state.find_item(op.get("id", ""))
    if item is None:
        return _reject(op, "unknown_item")
    force = bool(op.get("force")) and ctx.actor_type == "system"
    if ctx.is_agent:
        if item.protected:
            return _reject(op, "human_edited", target_id=item.id)
        if op.get("base_revision") != item.revision:
            return _reject(op, "revision_mismatch", target_id=item.id,
                           current_revision=item.revision)
    changes = op.get("set") or {}
    if not isinstance(changes, dict) or not (changes or op.get("restore_status")):
        return _reject(op, "empty_update", target_id=item.id)
    if "text" in changes:
        reason = _check_text(changes["text"])
        if reason:
            return _reject(op, reason, target_id=item.id)
    if "data" in changes:
        if not isinstance(changes["data"], dict):
            return _reject(op, "invalid_data", target_id=item.id)
        reason = _check_data_size(changes["data"])
        if reason:
            return _reject(op, reason, target_id=item.id)
    reason = _check_evidence(op, ctx)
    if reason:
        return _reject(op, reason, target_id=item.id)

    prev = {
        "text": item.text,
        "data": dict(item.data),
        "evidence": list(item.evidence),
    }
    prev_status = item.status
    if "text" in changes:
        item.text = changes["text"].strip()
    if "data" in changes:
        item.data = dict(changes["data"])
    if force and isinstance(changes.get("evidence"), list):
        # Undo path: restore the exact prior anchor set.
        item.evidence = list(changes["evidence"])
    elif op.get("evidence"):
        # Evidence anchors are additive. The consolidation pass is told to
        # reword and merge items, so replacing the list would silently drop
        # anchors the item already carried — and undo could not bring them back.
        item.evidence = list(dict.fromkeys(
            list(item.evidence) + list(op["evidence"])
        ))
    if op.get("restore_status") and force:
        item.status = op["restore_status"]
    elif ctx.is_human:
        item.status = "edited"
    item.revision += 1
    item.updated_at = now_iso()
    return OpResult(
        ok=True, op=op, target_id=item.id,
        effect={"entity": "item", "item": item.to_dict()},
        inverse={"op": "update_item", "id": item.id, "set": prev,
                 "restore_status": prev_status, "force": True},
    )


def _op_remove_item(state: MeetingState, op: Dict[str, Any], ctx: OpContext) -> OpResult:
    item = state.find_item(op.get("id", ""))
    if item is None:
        return _reject(op, "unknown_item")
    if item.status == "removed":
        return _reject(op, "already_removed", target_id=item.id)
    if ctx.is_agent:
        if item.protected:
            return _reject(op, "human_edited", target_id=item.id)
        if op.get("base_revision") != item.revision:
            return _reject(op, "revision_mismatch", target_id=item.id,
                           current_revision=item.revision)
    prev_status = item.status
    item.status = "removed"
    item.revision += 1
    item.updated_at = now_iso()
    return OpResult(
        ok=True, op=op, target_id=item.id,
        effect={"entity": "item", "item": item.to_dict()},
        inverse={"op": "update_item", "id": item.id, "set": {},
                 "restore_status": prev_status, "force": True},
    )


def _op_pin_item(state: MeetingState, op: Dict[str, Any], ctx: OpContext,
                 pinned: bool = True) -> OpResult:
    item = state.find_item(op.get("id", ""))
    if item is None:
        return _reject(op, "unknown_item")
    item.pinned = pinned
    item.updated_at = now_iso()
    return OpResult(
        ok=True, op=op, target_id=item.id,
        effect={"entity": "item", "item": item.to_dict()},
        inverse={"op": "unpin_item" if pinned else "pin_item", "id": item.id},
    )


def _op_confirm_item(state: MeetingState, op: Dict[str, Any], ctx: OpContext) -> OpResult:
    item = state.find_item(op.get("id", ""))
    if item is None:
        return _reject(op, "unknown_item")
    prev_status = item.status
    item.status = "confirmed"
    item.revision += 1
    item.updated_at = now_iso()
    return OpResult(
        ok=True, op=op, target_id=item.id,
        effect={"entity": "item", "item": item.to_dict()},
        inverse={"op": "update_item", "id": item.id, "set": {},
                 "restore_status": prev_status, "force": True},
    )


# ---------------------------------------------------------------------------
# Topic / summary / title
# ---------------------------------------------------------------------------

def _op_set_topic(state: MeetingState, op: Dict[str, Any], ctx: OpContext) -> OpResult:
    reason = _check_text(op.get("text"), MAX_TOPIC_LEN) or _check_evidence(op, ctx)
    if reason:
        return _reject(op, reason)
    prev = state.topic.current
    prev_evidence = state.topic.history[-1]["evidence"] if state.topic.history else []
    state.topic.history.append({
        "text": op["text"].strip(),
        "ts": now_iso(),
        "evidence": list(op.get("evidence") or []),
        "actor_type": ctx.actor_type,
    })
    state.topic.current = op["text"].strip()
    return OpResult(
        ok=True, op=op,
        effect={"entity": "topic", "topic": state.topic.to_dict()},
        inverse={"op": "set_topic", "text": prev, "evidence": prev_evidence}
        if prev else None,
    )


def _op_set_rolling_summary(state: MeetingState, op: Dict[str, Any],
                            ctx: OpContext) -> OpResult:
    text = op.get("text", "")
    if not isinstance(text, str) or len(text) > MAX_SUMMARY_LEN:
        return _reject(op, "invalid_text")
    if not text.strip() and ctx.actor_type != "system":
        # A blank summary silently wipes human-visible content. Only the undo
        # path (a system actor restoring a prior empty value) may clear it.
        return _reject(op, "invalid_text")
    prev = state.rolling_summary
    state.rolling_summary = text
    return OpResult(
        ok=True, op=op,
        effect={"entity": "rolling_summary", "text": text},
        inverse={"op": "set_rolling_summary", "text": prev},
    )


def _op_set_title(state: MeetingState, op: Dict[str, Any], ctx: OpContext) -> OpResult:
    # Match the host-only REST rename route: guests must not retitle.
    if ctx.actor_type not in ("host", "system"):
        return _reject(op, "host_only")
    reason = _check_text(op.get("text"), MAX_NAME_LEN)
    if reason:
        return _reject(op, reason)
    prev = state.title
    state.title = op["text"].strip()
    return OpResult(
        ok=True, op=op,
        effect={"entity": "title", "text": state.title},
        inverse={"op": "set_title", "text": prev} if prev else None,
    )


def _op_set_cloud_enabled(state: MeetingState, op: Dict[str, Any],
                          ctx: OpContext) -> OpResult:
    if ctx.actor_type not in ("host", "system"):
        return _reject(op, "host_only")
    enabled = bool(op.get("enabled"))
    prev = state.cloud_enabled
    state.cloud_enabled = enabled
    return OpResult(
        ok=True, op=op,
        effect={"entity": "cloud_enabled", "enabled": enabled},
        inverse={"op": "set_cloud_enabled", "enabled": prev},
    )


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------

def _op_upsert_participant(state: MeetingState, op: Dict[str, Any],
                           ctx: OpContext) -> OpResult:
    reason = _check_text(op.get("display_name"), MAX_NAME_LEN) or _check_evidence(op, ctx)
    if reason:
        return _reject(op, reason)
    # Everything the agent generates stays visibly provisional, and it may only
    # ever mint remote-speaker clusters — never a second "me" or a fake guest.
    if ctx.is_agent:
        is_provisional = True
        kind = op.get("kind", AGENT_PARTICIPANT_KIND)
        if kind != AGENT_PARTICIPANT_KIND:
            return _reject(op, "invalid_kind")
    else:
        is_provisional = bool(op.get("is_provisional", False))
        kind = op.get("kind", "others_cluster")

    pid = op.get("id")
    if pid:
        participant = state.participants.get(pid)
        if participant is None:
            return _reject(op, "unknown_participant")
        if ctx.is_agent and participant.name_source == "human":
            return _reject(op, "human_named", target_id=pid)
        prev_name = participant.display_name
        prev_source = participant.name_source
        participant.display_name = op["display_name"].strip()
        participant.name_source = "agent_inferred" if ctx.is_agent else "human"
        participant.is_provisional = is_provisional
        participant.updated_at = now_iso()
        inverse = {"op": "rename_participant", "participant_id": pid,
                   "display_name": prev_name, "_name_source": prev_source}
    else:
        # "me" and "guest" identities are established by the app itself (both
        # via actor_type="system"); a dashboard client minting one would let a
        # guest impersonate the host, whose participant is resolved by
        # scanning for the first kind == "me".
        if kind in ("me", "guest") and ctx.actor_type != "system":
            return _reject(op, "invalid_kind")
        if len(state.participants) >= MAX_PARTICIPANTS:
            return _reject(op, "participant_limit")
        participant = Participant(
            id=new_id("p"),
            display_name=op["display_name"].strip(),
            kind=kind,
            name_source="agent_inferred" if ctx.is_agent else "human",
            is_provisional=is_provisional,
        )
        if participant.kind not in ("me", "others_cluster", "guest"):
            return _reject(op, "invalid_kind")
        state.participants[participant.id] = participant
        inverse = None  # participant removal is not supported; renames only
    return OpResult(
        ok=True, op=op, target_id=participant.id,
        effect={"entity": "participant", "participant": participant.to_dict()},
        inverse=inverse,
    )


def _op_suggest_participant_name(state: MeetingState, op: Dict[str, Any],
                                 ctx: OpContext) -> OpResult:
    participant = state.participants.get(op.get("participant_id", ""))
    if participant is None:
        return _reject(op, "unknown_participant")
    if participant.name_source == "human":
        return _reject(op, "human_named", target_id=participant.id)
    reason = _check_text(op.get("display_name"), MAX_NAME_LEN) or _check_evidence(op, ctx)
    if reason:
        return _reject(op, reason, target_id=participant.id)
    prev_name = participant.display_name
    prev_source = participant.name_source
    participant.display_name = op["display_name"].strip()
    participant.name_source = "agent_inferred"
    participant.is_provisional = True
    participant.updated_at = now_iso()
    return OpResult(
        ok=True, op=op, target_id=participant.id,
        effect={"entity": "participant", "participant": participant.to_dict()},
        inverse={"op": "rename_participant", "participant_id": participant.id,
                 "display_name": prev_name, "_name_source": prev_source},
    )


def _op_rename_participant(state: MeetingState, op: Dict[str, Any],
                           ctx: OpContext) -> OpResult:
    if ctx.is_agent:
        return _reject(op, "agent_forbidden")
    participant = state.participants.get(op.get("participant_id", ""))
    if participant is None:
        return _reject(op, "unknown_participant")
    reason = _check_text(op.get("display_name"), MAX_NAME_LEN)
    if reason:
        return _reject(op, reason, target_id=participant.id)
    prev_name = participant.display_name
    prev_source = participant.name_source
    participant.display_name = op["display_name"].strip()
    # The undo path restores the original name_source via the private field.
    participant.name_source = op.get("_name_source", "human") \
        if ctx.actor_type == "system" else "human"
    participant.is_provisional = False
    participant.updated_at = now_iso()
    return OpResult(
        ok=True, op=op, target_id=participant.id,
        effect={"entity": "participant", "participant": participant.to_dict()},
        inverse={"op": "rename_participant", "participant_id": participant.id,
                 "display_name": prev_name, "_name_source": prev_source},
    )


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------

def _op_ask_question(state: MeetingState, op: Dict[str, Any], ctx: OpContext) -> OpResult:
    reason = _check_text(op.get("text")) or _check_evidence(op, ctx)
    if reason:
        return _reject(op, reason)
    # The agent is kept to a quiet inbox; humans get a far higher ceiling that
    # exists only to bound an abusive client.
    if ctx.actor_type != "system":
        cap = MAX_OPEN_QUESTIONS if ctx.is_agent else MAX_OPEN_QUESTIONS_HUMAN
        if state.open_question_count() >= cap:
            return _reject(op, "question_limit")
    question = Question(
        id=new_id("q"),
        text=op["text"].strip(),
        evidence=list(op.get("evidence") or []),
    )
    state.questions[question.id] = question
    return OpResult(
        ok=True, op=op, target_id=question.id,
        effect={"entity": "question", "question": question.to_dict()},
        inverse={"op": "dismiss_question", "question_id": question.id},
    )


def _op_resolve_question(state: MeetingState, op: Dict[str, Any],
                         ctx: OpContext) -> OpResult:
    """Agent resolution from audio: thresholded by confidence."""
    question = state.questions.get(op.get("question_id", ""))
    if question is None:
        return _reject(op, "unknown_question")
    if question.status != "open":
        return _reject(op, "already_closed", target_id=question.id)
    reason = _check_text(op.get("answer_text")) or _check_evidence(op, ctx)
    if reason:
        return _reject(op, reason, target_id=question.id)
    try:
        confidence = float(op.get("confidence", 0.0))
    except (TypeError, ValueError):
        return _reject(op, "invalid_confidence", target_id=question.id)

    if confidence >= RESOLVE_CONFIDENCE:
        question.status = "resolved"
        question.answer = op["answer_text"].strip()
        question.answer_source = "audio"
        question.confidence = confidence
        question.resolved_at = now_iso()
        question.resolved_by = ctx.actor_id
        question.evidence = list(
            dict.fromkeys(question.evidence + list(op.get("evidence") or []))
        )
        inverse = {"op": "reopen_question", "question_id": question.id}
    elif confidence >= SUGGEST_CONFIDENCE:
        question.suggested_answer = op["answer_text"].strip()
        question.suggested_confidence = confidence
        inverse = None
    else:
        return _reject(op, "low_confidence", target_id=question.id)
    return OpResult(
        ok=True, op=op, target_id=question.id,
        effect={"entity": "question", "question": question.to_dict()},
        inverse=inverse,
    )


def _op_answer_question(state: MeetingState, op: Dict[str, Any],
                        ctx: OpContext) -> OpResult:
    if ctx.is_agent:
        return _reject(op, "agent_forbidden")
    question = state.questions.get(op.get("question_id", ""))
    if question is None:
        return _reject(op, "unknown_question")
    reason = _check_text(op.get("answer_text"))
    if reason:
        return _reject(op, reason, target_id=question.id)
    question.thread.append({
        "author_type": ctx.actor_type, "author_id": ctx.actor_id,
        "text": op["answer_text"].strip(), "ts": now_iso(),
    })
    question.status = "resolved"
    question.answer = op["answer_text"].strip()
    question.answer_source = "user"
    question.suggested_answer = None
    question.suggested_confidence = None
    question.resolved_at = now_iso()
    question.resolved_by = ctx.actor_id
    return OpResult(
        ok=True, op=op, target_id=question.id,
        effect={"entity": "question", "question": question.to_dict()},
        inverse={"op": "reopen_question", "question_id": question.id},
    )


def _op_dismiss_question(state: MeetingState, op: Dict[str, Any],
                         ctx: OpContext) -> OpResult:
    question = state.questions.get(op.get("question_id", ""))
    if question is None:
        return _reject(op, "unknown_question")
    if question.status == "dismissed":
        return _reject(op, "already_closed", target_id=question.id)
    prev_status = question.status
    question.status = "dismissed"
    question.resolved_at = now_iso()
    question.resolved_by = ctx.actor_id
    return OpResult(
        ok=True, op=op, target_id=question.id,
        effect={"entity": "question", "question": question.to_dict()},
        inverse={"op": "reopen_question", "question_id": question.id}
        if prev_status == "open" else None,
    )


def _op_reopen_question(state: MeetingState, op: Dict[str, Any],
                        ctx: OpContext) -> OpResult:
    if ctx.is_agent:
        return _reject(op, "agent_forbidden")
    question = state.questions.get(op.get("question_id", ""))
    if question is None:
        return _reject(op, "unknown_question")
    if question.status == "open":
        return _reject(op, "already_open", target_id=question.id)
    question.status = "open"
    question.resolved_at = None
    question.resolved_by = None
    return OpResult(
        ok=True, op=op, target_id=question.id,
        effect={"entity": "question", "question": question.to_dict()},
        inverse=None,
    )


# ---------------------------------------------------------------------------
# Segment ops (validated here, applied by the store's segment handler)
# ---------------------------------------------------------------------------

def _op_reassign_segment_speaker(state: MeetingState, op: Dict[str, Any],
                                 ctx: OpContext) -> OpResult:
    if ctx.is_agent:
        return _reject(op, "agent_forbidden")
    segment_id = op.get("segment_id")
    participant_id = op.get("participant_id")
    if not isinstance(segment_id, str) or not segment_id:
        return _reject(op, "invalid_segment")
    if participant_id is not None and participant_id not in state.participants:
        return _reject(op, "unknown_participant")
    # Human corrections are authoritative: a diarizer re-cluster batch computed
    # moments before a pin landed must not silently revert it (and, because the
    # store writes pinned=ctx.is_human, would also clear the pin flag). Humans
    # may still overwrite an earlier pin — that is a later correction.
    if not ctx.is_human and not op.get("force") and ctx.segment_pinned is not None:
        try:
            already_pinned = ctx.segment_pinned(segment_id)
        except Exception:
            logger.exception("segment_pinned predicate failed for %s", segment_id)
            already_pinned = True  # fail closed rather than clobber a pin
        if already_pinned:
            return _reject(op, "segment_pinned", target_id=segment_id)
    # The store performs the actual segment mutation; this result just carries
    # the validated intent.
    return OpResult(
        ok=True, op=op, target_id=segment_id,
        effect={"entity": "segment_speaker", "segment_id": segment_id,
                "participant_id": participant_id,
                "source": "human" if ctx.is_human else "diarizer",
                "pinned": ctx.is_human},
        inverse=None,  # inverse filled in by the store, which knows prior state
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_HANDLERS: Dict[str, Callable[[MeetingState, Dict[str, Any], OpContext], OpResult]] = {
    "add_item": _op_add_item,
    "update_item": _op_update_item,
    "remove_item": _op_remove_item,
    "pin_item": lambda s, o, c: _op_pin_item(s, o, c, pinned=True),
    "unpin_item": lambda s, o, c: _op_pin_item(s, o, c, pinned=False),
    "confirm_item": _op_confirm_item,
    "set_topic": _op_set_topic,
    "set_rolling_summary": _op_set_rolling_summary,
    "set_title": _op_set_title,
    "set_cloud_enabled": _op_set_cloud_enabled,
    "upsert_participant": _op_upsert_participant,
    "suggest_participant_name": _op_suggest_participant_name,
    "rename_participant": _op_rename_participant,
    "ask_question": _op_ask_question,
    "resolve_question": _op_resolve_question,
    "answer_question": _op_answer_question,
    "dismiss_question": _op_dismiss_question,
    "reopen_question": _op_reopen_question,
    "reassign_segment_speaker": _op_reassign_segment_speaker,
}


def apply_ops(state: MeetingState, ops: List[Dict[str, Any]],
              ctx: OpContext) -> List[OpResult]:
    """Validate and apply a batch of ops against ``state``.

    Each op succeeds or fails independently. Callers (the store) own locking,
    seq numbering, persistence, and broadcast.

    Args:
        state: The meeting state document to mutate.
        ops: Op dicts, each with at least an ``op`` key.
        ctx: Actor attribution and validation hooks.

    Returns:
        One ``OpResult`` per submitted op, in order.
    """
    results: List[OpResult] = []
    for op in ops:
        if not isinstance(op, dict) or "op" not in op:
            results.append(_reject(op if isinstance(op, dict) else {"op": op},
                                   "malformed_op"))
            continue
        name = op["op"]
        handler = _HANDLERS.get(name)
        if handler is None:
            results.append(_reject(op, "unknown_op"))
            continue
        if ctx.is_agent and name not in AGENT_OPS:
            results.append(_reject(op, "agent_forbidden"))
            continue
        if name in AGENT_ONLY_OPS and ctx.actor_type not in ("agent", "system"):
            results.append(_reject(op, "agent_only"))
            continue
        if name in HOST_ONLY_OPS and ctx.actor_type == "user":
            results.append(_reject(op, "host_only"))
            continue
        try:
            results.append(handler(state, op, ctx))
        except Exception:
            logger.exception("State op %s raised; rejecting", name)
            results.append(_reject(op, "internal_error"))
    return results
