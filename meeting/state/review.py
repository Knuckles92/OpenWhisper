"""Atomic review actions. Only the review worker may publish model judgments."""
from __future__ import annotations

import json
from copy import deepcopy

from meeting.interfaces import OpResult
from meeting.state.schema import CardItem, new_id, now_iso

#: Consent the meeting must record before a review may run.
_REVIEW_CONSENT = "typesafe-text-v1"

#: Suffix that appends a review answer to a note block it clarifies.
_CLARIFICATION_PREFIX = "\n\nUser clarification (supersedes earlier wording on this point): "
_SUPERSEDED_CLARIFICATION_PREFIX = "\n\nEarlier clarification, superseded by a later insight edit: "

_REVIEW_LABELS = {"accurate": "Confirmed as accurate", "accepted": "Agreed",
                  "offered": "Only offered, not agreed", "incorrect": "Incorrect; removed",
                  "resolved": "Risk resolved", "open": "Risk remains open", "edit": "Corrected"}

_MAX_REVIEW_TEXT = 3500
_MAX_FIELD_CHARS = 120
_MAX_DATA_BYTES = 4096
_MAX_USER_NOTES = 500
_MAX_NOTE_BLOCK_CHARS = 4000


def _result(op, state, items=(), target=None):
    return OpResult(ok=True, op=op, target_id=target,
                    effect={"entity": "review", "review": deepcopy(state.insight_review),
                            "items": [item.to_dict() for item in items]})


def _reject(op, reason):
    return OpResult(ok=False, op=op, reason=reason)


def _system(state, op, ctx):
    if ctx.actor_type != "system":
        return "system_only"
    if (not state.insight_review.get("enabled") or not state.cloud_enabled
            or state.insight_review.get("consent") != _REVIEW_CONSENT):
        return "review_disabled"
    return None


def begin_review(state, op, ctx):
    reason = _system(state, op, ctx)
    if reason:
        return _reject(op, reason)
    if state.status != "ended" or state.finalization.status in ("pending", "running"):
        return _reject(op, "meeting_not_ready")
    review = state.insight_review
    if review.get("status") == "running":
        return _reject(op, "review_running")
    review.update(status="running", run_id=op["run_id"], message="Checking the final insights…")
    return _result(op, state)


def finish_review(state, op, ctx):
    if ctx.actor_type != "system":
        return _reject(op, "system_only")
    review = state.insight_review
    if review.get("run_id") != op.get("run_id") or review.get("status") != "running":
        return _reject(op, "stale_review")
    if (not state.cloud_enabled or not review.get("enabled")
            or review.get("consent") != _REVIEW_CONSENT):
        review.update(status="unavailable", message="AI insights are off. Review was stopped.")
        return _result(op, state)
    changed, eligible = _apply_assessments(state, op)
    retained = [q for q in review.get("questions", []) if q.get("status") == "answered"]
    questions = _new_review_questions(state, op, review, eligible)
    if op.get("status") in ("ready", "partial"):
        review["questions"] = retained[-50:] + questions
    review.update(status=op["status"], message=op.get("message", ""), checked_at=now_iso())
    return _result(op, state, changed)


def _apply_assessments(state, op):
    """Store each assessment on its still-current item; returns (items, their ids)."""
    changed = []
    eligible = set()
    for assessment in op.get("assessments", []):
        item = state.find_item(assessment["item_id"])
        if (item is None or item.protected or item.status == "removed"
                or item.revision != assessment["revision"]):
            continue
        item.review = deepcopy(assessment["review"])
        eligible.add(item.id)
        changed.append(item)
    return changed, eligible


def _new_review_questions(state, op, review, eligible):
    """One question per eligible item, skipping repeats of the same insight text."""
    # A skipped field stays skipped across retries of the same insight revision.
    skipped = {(q["item_id"], q["revision"], q["field"]) for q in review.get("questions", [])
               if q.get("status") == "skipped"}
    questions = []
    seen = set()
    seen_text = set()
    for q in op.get("questions", []):
        if q["item_id"] not in eligible or q["item_id"] in seen:
            continue
        text_key = " ".join(state.find_item(q["item_id"]).text.lower().split())
        if text_key in seen_text:
            continue
        seen_text.add(text_key)
        q = deepcopy(q)
        if (q["item_id"], q["revision"], q["field"]) in skipped:
            q["status"] = "skipped"
        seen.add(q["item_id"])
        questions.append(q)
    return questions


def _question(state, op, ctx):
    if ctx.actor_type != "host":
        return None, None, "host_only"
    if state.status != "ended" or state.finalization.status in ("pending", "running"):
        return None, None, "meeting_not_ready"
    question = next((q for q in state.insight_review.get("questions", [])
                     if q["id"] == op.get("question_id")), None)
    if question is None:
        return None, None, "unknown_review_question"
    item = state.find_item(question["item_id"])
    if (question.get("superseded") or item is None or item.revision != question["revision"]
            or item.evidence != question["evidence"]
            or (item.status == "removed" and not question.get("correction"))):
        return question, item, "review_changed"
    return question, item, None


def skip_review(state, op, ctx):
    q, item, reason = _question(state, op, ctx)
    if reason:
        return _reject(op, reason)
    if q["status"] != "open":
        return _reject(op, "already_closed")
    q["status"] = "skipped"
    return _result(op, state, target=item.id)


def reopen_review(state, op, ctx):
    q, item, reason = _question(state, op, ctx)
    if reason:
        return _reject(op, reason)
    q["status"] = "open"
    return _result(op, state, target=item.id)


def answer_review(state, op, ctx):
    q, item, reason = _question(state, op, ctx)
    if reason:
        return _reject(op, reason)
    reason = _answer_rejection(q, item, op)
    if reason:
        return _reject(op, reason)
    action = op.get("answer")
    text = op.get("text", item.text).strip()
    field = q["field"]
    data = dict(item.data)
    if action == "edit" and field == "owner":
        reason = _edit_owner(state, op, item, text, data)
    elif action == "edit" and field == "deadline":
        reason = _edit_deadline(op, item, text, data)
    if reason:
        return _reject(op, reason)
    if len(json.dumps(data)) > _MAX_DATA_BYTES:
        return _reject(op, "data_too_large")
    text = _answered_text(action, text, item, data)
    note_text = _review_note_text(state, action, field, text, data)
    note = _linked_note(state, q)
    if note is None and len(state.cards["user_notes"]) >= _MAX_USER_NOTES:
        return _reject(op, "item_limit")
    before = item.to_dict()
    item.text = text
    item.data = data
    item.status = "removed" if action == "incorrect" else "edited"
    item.revision += 1
    item.updated_at = now_iso()
    item.review = {"state": "human", "field": field, "answer": action,
                   "reviewed_by": ctx.actor_id, "reviewed_at": now_iso()}
    changed = [item]
    note = _write_review_note(state, note, note_text, item, ctx)
    changed.append(note)
    changed.extend(_clarify_note_blocks(state, item, q, note_text))
    q.update(status="answered", answer=_REVIEW_LABELS[action], answer_source="user", correction=note_text,
             answered_at=now_iso(), answered_by=ctx.actor_id, note_id=note.id,
             revision=item.revision, insight=item.to_dict())
    # The audit op stores the previous insight without changing transcript evidence.
    q.setdefault("history", []).append({"before": before, "answer": action, "at": now_iso(),
                                        "actor_id": ctx.actor_id, "actor_type": ctx.actor_type})
    q["history"] = q["history"][-10:]
    return _result(op, state, changed, item.id)


def _answer_rejection(q, item, op):
    """Why this answer cannot apply to the open question, or None."""
    if q["status"] != "open":
        return "already_closed"
    if op.get("answer") not in q["choices"]:
        return "invalid_review_answer"
    text = op.get("text", item.text)
    if not isinstance(text, str) or not text.strip() or len(text) > _MAX_REVIEW_TEXT:
        return "invalid_review_text"
    return None


def _edit_owner(state, op, item, text, data):
    """Set the corrected owner on ``data``; returns a rejection reason or None."""
    owner = op.get("owner_participant_id")
    if owner is not None and (not isinstance(owner, str) or owner not in state.participants):
        return "invalid_owner"
    owner_name = op.get("owner_name", "")
    if not isinstance(owner_name, str) or len(owner_name) > _MAX_FIELD_CHARS:
        return "invalid_owner"
    if owner != data.get("owner_participant_id") and text == item.text:
        return "review_wording_required"
    data["owner_participant_id"] = owner
    data["owner_name"] = owner_name.strip() if owner is None else ""
    return None


def _edit_deadline(op, item, text, data):
    """Set the corrected deadline on ``data``; returns a rejection reason or None."""
    deadline = op.get("deadline", "")
    if not isinstance(deadline, str) or len(deadline) > _MAX_FIELD_CHARS:
        return "invalid_deadline"
    current = str(data.get("deadline") or data.get("due_date") or "")
    if deadline.strip() != current and text == item.text:
        return "review_wording_required"
    data["deadline"] = deadline.strip() or None
    data.pop("due_date", None)
    return None


def _answered_text(action, text, item, data):
    """Record a commitment/resolution answer on ``data`` and label the text to match."""
    if action == "offered":
        data["commitment"] = "offered"
        if not text.startswith("Offered, not agreed: "):
            text = f"Offered, not agreed: {text}"
    elif action == "accepted":
        data["commitment"] = "accepted"
        if item.review.get("answer") == "offered":
            text = text.removeprefix("Offered, not agreed: ")
    elif action in ("resolved", "open"):
        data["resolution"] = action
        if action == "resolved" and not text.startswith("Resolved: "):
            text = f"Resolved: {text}"
        elif action == "open" and item.review.get("answer") == "resolved":
            text = text.removeprefix("Resolved: ")
    return text


def _review_note_text(state, action, field, text, data):
    note_text = f"{_REVIEW_LABELS[action]} — {text}"
    if action == "edit" and field == "owner":
        person = state.participants.get(data.get("owner_participant_id"))
        owner_label = person.display_name if person else data.get("owner_name") or "Unassigned"
        note_text += f" Owner: {owner_label}."
    elif action == "edit" and field == "deadline":
        note_text += f" Deadline: {data.get('deadline') or 'None agreed'}."
    return note_text


def _linked_note(state, q):
    """The user note an earlier answer to ``q`` created, if it still exists."""
    note_id = q.get("note_id")
    if not note_id:
        return None
    return state.find_item(note_id)


def _write_review_note(state, note, note_text, item, ctx):
    """Create the answer's user note, or rewrite the existing one."""
    if note is None:
        note = CardItem(id=new_id("it"), card="user_notes", text=note_text,
                        status="edited", author_type="user", author_id=ctx.actor_id,
                        evidence=list(item.evidence), data={"review_item_id": item.id})
        state.cards["user_notes"].append(note)
    else:
        note.text = note_text
        note.status = "edited"
        note.revision += 1
        note.updated_at = now_iso()
    return note


def _clarify_note_blocks(state, item, q, note_text):
    """Append (or refresh) the clarification on note blocks sharing the item's evidence.

    Clarifications are explicit additions: do not guess which prose to replace.
    The report also carries the authoritative correction when no note is anchored.
    """
    changed = []
    for block in state.cards["live_notes"]:
        if block.status == "removed" or not set(block.evidence).intersection(item.evidence):
            continue
        suffix = f"{_CLARIFICATION_PREFIX}{note_text}"
        previous = f"{_CLARIFICATION_PREFIX}{q.get('correction', '')}"
        if q.get("correction") and previous in block.text:
            updated = block.text.replace(previous, suffix)
        else:
            updated = block.text + suffix
        if len(updated) <= _MAX_NOTE_BLOCK_CHARS:
            block.text = updated
            block.status = "edited"
            block.revision += 1
            block.updated_at = now_iso()
            changed.append(block)
    return changed


def unavailable_review(state, op, ctx):
    if ctx.actor_type != "system":
        return _reject(op, "system_only")
    state.insight_review.update(
        status="unavailable",
        message="AI insights and insight review must both be on for this meeting.",
    )
    return _result(op, state)


REVIEW_HANDLERS = {
    "review_unavailable": unavailable_review,
    "review_begin": begin_review,
    "review_finish": finish_review,
    "review_answer": answer_review,
    "review_skip": skip_review,
    "review_reopen": reopen_review,
}


def item_effect(state, item):
    """Invalidate linked reviews when a separate edit supersedes their revision."""
    related = [q for q in state.insight_review.get("questions", [])
               if q["item_id"] == item.id and not q.get("superseded")]
    if not related:
        return {"entity": "item", "item": item.to_dict()}
    changed = [item]
    for q in related:
        q["superseded"] = True
        note = state.find_item(q.get("note_id", ""))
        if note and note.status != "removed":
            old_suffix = f"{_CLARIFICATION_PREFIX}{note.text}"
            superseded_suffix = f"{_SUPERSEDED_CLARIFICATION_PREFIX}{note.text}"
            for block in state.cards["live_notes"]:
                if old_suffix in block.text:
                    block.text = block.text.replace(old_suffix, superseded_suffix)
                    block.revision += 1
                    block.updated_at = now_iso()
                    changed.append(block)
            note.text = f"Earlier review, superseded by a later insight edit: {note.text}"
            note.revision += 1
            note.updated_at = now_iso()
            changed.append(note)
    return {"entity": "review", "review": deepcopy(state.insight_review),
            "items": [changed_item.to_dict() for changed_item in changed]}


def invalidate_checks(state):
    for items in state.cards.values():
        for item in items:
            item.citation_check = {}
    review = state.insight_review
    if not review.get("enabled"):
        return
    review.update(status="pending", message="Waiting for the updated final insights.")
    for q in review.get("questions", []):
        if q.get("status") != "answered":
            q["superseded"] = True
    for items in state.cards.values():
        for item in items:
            if item.review.get("state") != "human":
                item.review = {}
