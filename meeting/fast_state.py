"""System-owned advisory state operations for fast meeting judgments."""
from copy import deepcopy

from meeting.interfaces import OpResult
from meeting.state.schema import now_iso


def publish_highlights(state, op, ctx):
    if ctx.actor_type != "system" or ctx.actor_id != "live-signals":
        return OpResult(False, op, reason="system_only")
    if not state.cloud_enabled or state.status not in ("active", "paused"):
        return OpResult(False, op, reason="inactive")
    pulses = op.get("pulses", [])
    if not isinstance(pulses, list) or len(pulses) > 4:
        return OpResult(False, op, reason="invalid_pulses")
    existing = {p["id"]: p for p in state.live_highlights}
    for pulse in pulses:
        if pulse.get("kind") not in ("decision", "disagreement", "commitment", "number"):
            return OpResult(False, op, reason="invalid_pulse")
        if ctx.segment_exists and not ctx.segment_exists(pulse.get("segment_id")):
            return OpResult(False, op, reason="unknown_evidence")
        existing[pulse["id"]] = deepcopy(pulse)
    state.live_highlights = sorted(existing.values(), key=lambda p: p["start_s"])[-960:]
    return OpResult(True, op, effect={"entity": "live_highlights", "pulses": deepcopy(state.live_highlights)})


def citation_check(state, op, ctx):
    if ctx.actor_type != "system" or ctx.actor_id != "citation-verifier":
        return OpResult(False, op, reason="system_only")
    item = state.find_item(op.get("id", ""))
    if not item or item.status == "removed":
        return OpResult(False, op, reason="unknown_item")
    if not state.cloud_enabled or item.revision != op.get("revision"):
        return OpResult(False, op, reason="stale_check")
    item.citation_check = deepcopy(op.get("check") or {})
    # No revision increment: this is an annotation, not an edit of the claim.
    return OpResult(True, op, effect={"entity": "item", "item": item.to_dict()})


def voice_feedback(state, op, ctx):
    if ctx.actor_type != "system" or ctx.actor_id != "voice_command":
        return OpResult(False, op, reason="system_only")
    if not state.cloud_enabled or state.status not in ("active", "paused"):
        return OpResult(False, op, reason="inactive")
    state.voice_feedback = {"message": str(op.get("message", ""))[:500], "at": now_iso()}
    return OpResult(True, op, effect={"entity": "voice_feedback", "feedback": dict(state.voice_feedback)})


def invalidate_citations(state, op, ctx):
    if ctx.actor_type != "system" or ctx.actor_id != "citation-verifier":
        return OpResult(False, op, reason="system_only")
    ids = set(op.get("segment_ids") or [])
    items = []
    for card in state.cards.values():
        for item in card:
            if item.status != "removed" and (not ids or ids.intersection(item.evidence)):
                item.citation_check = {}
                items.append(item.to_dict())
    return OpResult(True, op, effect={"entity": "review", "review": deepcopy(state.insight_review), "items": items})


FAST_HANDLERS = {
    "publish_highlights": publish_highlights,
    "citation_check": citation_check,
    "invalidate_citations": invalidate_citations,
    "voice_feedback": voice_feedback,
}
