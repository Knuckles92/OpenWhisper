"""Atomic review actions. Only the review worker may publish model judgments."""
from __future__ import annotations

import json
from copy import deepcopy

from meeting.interfaces import OpResult
from meeting.state.schema import CardItem, new_id, now_iso


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
            or state.insight_review.get("consent") != "typesafe-text-v1"):
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
    if not state.cloud_enabled or not review.get("enabled") or review.get("consent") != "typesafe-text-v1":
        review.update(status="unavailable", message="Cloud intelligence is off. Review was stopped.")
        return _result(op, state)
    changed = []
    eligible = set()
    for assessment in op.get("assessments", []):
        item = state.find_item(assessment["item_id"])
        if item is None or item.protected or item.status == "removed" or item.revision != assessment["revision"]:
            continue
        item.review = deepcopy(assessment["review"])
        eligible.add(item.id)
        changed.append(item)
    retained = [q for q in review.get("questions", []) if q.get("status") == "answered"]
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
        if len(questions) >= review.get("max_questions", 3):
            break
    if op.get("status") in ("ready", "partial"):
        review["questions"] = retained[-50:] + questions
    review.update(status=op["status"], message=op.get("message", ""), checked_at=now_iso())
    return _result(op, state, changed)


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
            or item.evidence != question["evidence"] or (item.status == "removed" and not question.get("correction"))):
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
    if q["status"] != "open":
        return _reject(op, "already_closed")
    action = op.get("answer")
    if action not in q["choices"]:
        return _reject(op, "invalid_review_answer")
    text = op.get("text", item.text)
    if not isinstance(text, str) or not text.strip() or len(text) > 3500:
        return _reject(op, "invalid_review_text")
    text = text.strip()
    field = q["field"]
    data = dict(item.data)
    if action == "edit":
        if field == "owner":
            owner = op.get("owner_participant_id")
            if owner is not None and (not isinstance(owner, str) or owner not in state.participants):
                return _reject(op, "invalid_owner")
            owner_name = op.get("owner_name", "")
            if not isinstance(owner_name, str) or len(owner_name) > 120:
                return _reject(op, "invalid_owner")
            if owner != data.get("owner_participant_id") and text == item.text:
                return _reject(op, "review_wording_required")
            data["owner_participant_id"] = owner
            data["owner_name"] = owner_name.strip() if owner is None else ""
        if field == "deadline":
            deadline = op.get("deadline", "")
            if not isinstance(deadline, str) or len(deadline) > 120:
                return _reject(op, "invalid_deadline")
            if deadline.strip() != str(data.get("deadline") or data.get("due_date") or "") and text == item.text:
                return _reject(op, "review_wording_required")
            data["deadline"] = deadline.strip() or None
            data.pop("due_date", None)
    if len(json.dumps(data)) > 4096:
        return _reject(op, "data_too_large")
    labels = {"accurate": "Confirmed as accurate", "accepted": "Agreed",
              "offered": "Only offered, not agreed", "incorrect": "Incorrect; removed",
              "resolved": "Risk resolved", "open": "Risk remains open", "edit": "Corrected"}
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
    note_text = f"{labels[action]} — {text}"
    if action == "edit" and field == "owner":
        person = state.participants.get(data.get("owner_participant_id"))
        owner_label = person.display_name if person else data.get("owner_name") or "Unassigned"
        note_text += f" Owner: {owner_label}."
    elif action == "edit" and field == "deadline":
        note_text += f" Deadline: {data.get('deadline') or 'None agreed'}."
    note_id = q.get("note_id")
    note = state.find_item(note_id) if note_id else None
    if note is None and len(state.cards["user_notes"]) >= 500:
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
    changed.append(note)
    # Clarifications are explicit additions: do not guess which prose to replace.
    # The report also carries the authoritative correction when no note is anchored.
    for block in state.cards["live_notes"]:
        if block.status == "removed" or not set(block.evidence).intersection(item.evidence):
            continue
        suffix = f"\n\nUser clarification (supersedes earlier wording on this point): {note_text}"
        previous = f"\n\nUser clarification (supersedes earlier wording on this point): {q.get('correction', '')}"
        updated = block.text.replace(previous, suffix) if q.get("correction") and previous in block.text else block.text + suffix
        if len(updated) <= 4000:
            block.text = updated
            block.status = "edited"
            block.revision += 1
            block.updated_at = now_iso()
            changed.append(block)
    q.update(status="answered", answer=labels[action], answer_source="user", correction=note_text,
             answered_at=now_iso(), answered_by=ctx.actor_id, note_id=note.id,
             revision=item.revision, insight=item.to_dict())
    # The audit op stores the previous insight without changing transcript evidence.
    q.setdefault("history", []).append({"before": before, "answer": action,
                                        "at": now_iso(), "actor_id": ctx.actor_id, "actor_type": ctx.actor_type})
    q["history"] = q["history"][-10:]
    return _result(op, state, changed, item.id)


def unavailable_review(state, op, ctx):
    if ctx.actor_type != "system":
        return _reject(op, "system_only")
    state.insight_review.update(status="unavailable", message="Cloud intelligence and TypeSafe sharing must both be enabled for this meeting.")
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
            old_suffix = f"\n\nUser clarification (supersedes earlier wording on this point): {note.text}"
            for block in state.cards["live_notes"]:
                if old_suffix in block.text:
                    block.text = block.text.replace(old_suffix, f"\n\nEarlier clarification, superseded by a later insight edit: {note.text}")
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
