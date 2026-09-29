"""One tool-handling policy for every meeting agent core.

The direct agent answers model tool calls in process and the sidecar bridge
answers the same calls arriving over RPC. Both route them through
:func:`run_tool`, so pass restrictions, evidence-id repair, read-tool payloads
and op-result serialization cannot drift between cores.

An op outside the pass's job comes back as a per-op rejection
(``polish_only`` / ``notes_only``) rather than being dropped silently: the
model sees why it did not land and can correct itself within the same run,
just as it does for validation rejections. :mod:`meeting.state.patches` stays
the authority for everything the policy lets through.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from meeting.agent.evidence import repair_evidence_ids
from meeting.interfaces import CheckpointPayload, OpResult
from meeting.state.patches import filter_notes_ops, live_note_ids

logger = logging.getLogger(__name__)

#: Pass kinds a checkpoint can run as, in the order they are checked.
PASS_CARDS = "cards"
PASS_NOTES = "notes"
PASS_POLISH = "polish"
PASS_CONSOLIDATION = "consolidation"

#: Rejection reasons the policy adds on top of op validation.
POLISH_ONLY = "polish_only"
NOTES_ONLY = "notes_only"
PASS_REJECTIONS = frozenset({POLISH_ONLY, NOTES_ONLY})

_POLISH_OPS = frozenset({"revise_segment_text"})
_QUESTION_TOOLS = frozenset({"ask_question", "resolve_question"})
#: Read-only tools: name -> (optional target argument, text when unavailable).
_READ_TOOLS = {
    "search_past_meetings": (
        "meeting_id", "Past-meeting recall is not available.",
    ),
    "search_context_files": (
        "relative_path", "Knowledge-folder search is not available.",
    ),
}
_DEFAULT_READ_LIMIT = 10

READ_TOOLS = frozenset(_READ_TOOLS)
TOOL_NAMES = frozenset({"patch_state", *_QUESTION_TOOLS, *READ_TOOLS})


def pass_kind_for(payload: CheckpointPayload) -> str:
    """The pass a checkpoint payload runs as."""
    if payload.is_consolidation:
        return PASS_CONSOLIDATION
    if payload.is_polish:
        return PASS_POLISH
    if payload.is_notes:
        return PASS_NOTES
    return PASS_CARDS


def citable_segment_ids(payload: CheckpointPayload) -> List[str]:
    """Segment ids the pass's prompt shows: the evidence-repair universe."""
    segments = list(payload.new_segments or []) + list(
        payload.state_snapshot.get("recent_transcript_context") or []
    )
    return [
        str(seg.get("id"))
        for seg in segments
        if isinstance(seg, dict) and seg.get("id")
    ]


@dataclass(frozen=True)
class ToolScope:
    """What one pass's tool calls may do.

    Attributes:
        pass_kind: One of the ``PASS_*`` kinds.
        note_ids: Live-notes block ids; on a notes pass the only items
            ``update_item``/``remove_item`` may target.
        citable_ids: Segment ids shown to the model, for evidence repair.
    """

    pass_kind: str = PASS_CARDS
    note_ids: frozenset = frozenset()
    citable_ids: Tuple[str, ...] = ()

    @classmethod
    def for_payload(cls, payload: CheckpointPayload) -> "ToolScope":
        kind = pass_kind_for(payload)
        return cls(
            pass_kind=kind,
            note_ids=(
                live_note_ids(payload.state_snapshot)
                if kind == PASS_NOTES else frozenset()
            ),
            citable_ids=tuple(citable_segment_ids(payload)),
        )


def serialize_op_result(result: OpResult) -> Dict[str, Any]:
    """The per-op outcome a model sees."""
    return {
        "ok": result.ok,
        "reason": result.reason,
        "target_id": result.target_id,
        "seq": result.seq,
        "current_revision": result.current_revision,
    }


def op_results_payload(results: Sequence[OpResult]) -> Dict[str, Any]:
    return {"results": [serialize_op_result(r) for r in results]}


def repair_evidence(tools: Any, ops: List[Dict[str, Any]],
                    citable_ids: Sequence[str]) -> List[Dict[str, Any]]:
    """Repair truncated or mistyped evidence ids before exact validation."""
    exists = getattr(tools, "segment_exists", None)
    if not callable(exists) or not citable_ids:
        return ops
    repaired, count = repair_evidence_ids(ops, list(citable_ids), exists)
    if count:
        logger.info("Repaired %d mistyped evidence id(s)", count)
    return repaired


def pass_rejection(scope: ToolScope, op: Any) -> Optional[str]:
    """The reason ``op`` is outside the pass's job, or None."""
    if scope.pass_kind == PASS_POLISH:
        if not isinstance(op, dict) or op.get("op") not in _POLISH_OPS:
            return POLISH_ONLY
    elif scope.pass_kind == PASS_NOTES:
        if not filter_notes_ops([op], scope.note_ids):
            return NOTES_ONLY
    return None


def apply_patch_ops(tools: Any, ops: Any, scope: ToolScope) -> List[OpResult]:
    """Apply a ``patch_state`` batch; the results line up with ``ops``.

    Raises:
        ValueError: When ``ops`` is not a list.
    """
    if not isinstance(ops, list):
        raise ValueError("'ops' must be a list of op objects")
    denied: Dict[int, OpResult] = {}
    allowed: List[Any] = []
    for index, op in enumerate(ops):
        reason = pass_rejection(scope, op)
        if reason is None:
            allowed.append(op)
        else:
            denied[index] = OpResult(ok=False, op=op, reason=reason)
    applied = (
        list(tools.apply_agent_ops(
            repair_evidence(tools, allowed, scope.citable_ids)
        ))
        if allowed else []
    )
    if not denied:
        return applied
    remaining = iter(applied)
    return [
        denied[index] if index in denied else next(remaining)
        for index in range(len(ops))
    ]


def _answer_question(tools: Any, name: str, args: Dict[str, Any],
                     scope: ToolScope) -> OpResult:
    if scope.pass_kind == PASS_POLISH:
        return OpResult(ok=False, op={"op": name}, reason=POLISH_ONLY)
    if scope.pass_kind == PASS_NOTES:
        return OpResult(ok=False, op={"op": name}, reason=NOTES_ONLY)
    evidence = list(args.get("evidence") or [])
    evidence = repair_evidence(
        tools, [{"op": name, "evidence": evidence}], scope.citable_ids,
    )[0].get("evidence") or evidence
    if name == "ask_question":
        return tools.ask_question(str(args.get("text") or ""), evidence)
    return tools.resolve_question(
        str(args.get("question_id") or ""),
        str(args.get("answer_text") or ""),
        float(args.get("confidence") or 0.0),
        evidence,
    )


def _read_limit(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return _DEFAULT_READ_LIMIT


def run_read_tool(tools: Any, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Run a read-only search; allowed on every pass and never tallied."""
    target, unavailable = _READ_TOOLS[name]
    search = getattr(tools, name, None)
    if not callable(search):
        return {"ok": False, "disabled": True, "text": unavailable, "hits": []}
    result = search(
        query=str(args.get("query") or ""),
        limit=_read_limit(args.get("limit")),
        **{target: str(args.get(target) or "").strip() or None},
    )
    if isinstance(result, dict):
        return result
    return {"ok": True, "text": str(result or ""), "hits": []}


def run_tool(tools: Any, name: str, args: Dict[str, Any],
             scope: ToolScope) -> Tuple[Dict[str, Any], List[OpResult]]:
    """Answer one model tool call.

    Args:
        tools: The ``AgentToolHost`` that validates and applies ops.
        name: Tool name without any transport prefix (``patch_state``).
        args: The call's arguments.
        scope: The calling pass's authority.

    Returns:
        The payload to hand back to the model, and the write results to
        report upward (empty for read-only tools).

    Raises:
        ValueError: For an unknown tool or a malformed ``ops`` list.
    """
    if name in _READ_TOOLS:
        return run_read_tool(tools, name, args), []
    if name == "patch_state":
        results = apply_patch_ops(tools, args.get("ops"), scope)
        return op_results_payload(results), results
    if name in _QUESTION_TOOLS:
        result = _answer_question(tools, name, args, scope)
        return serialize_op_result(result), [result]
    raise ValueError(f"unknown tool: {name}")


def tool_result_text(name: str, payload: Dict[str, Any]) -> str:
    """The tool message a chat model reads for ``payload``."""
    if name in _READ_TOOLS:
        return str(payload.get("text") or json.dumps(payload))
    return json.dumps(payload)
