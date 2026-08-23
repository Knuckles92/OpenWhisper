"""Question-inbox helpers for the meeting-intelligence agent.

Small, prompt-facing logic only: rendering the open-question inbox so the
model can target ``resolve_question`` ops, plus pruning/follow-up hints. The
actual thresholds and enforcement (auto-resolve at high confidence, greyed
suggestion at medium, question cap) live in :mod:`meeting.state.patches` and
are imported from there so prompt guidance can never drift from validation.
"""
from __future__ import annotations

from typing import Any, Dict, List

from meeting.state.patches import (
    MAX_OPEN_QUESTIONS,
    RESOLVE_CONFIDENCE,
    SUGGEST_CONFIDENCE,
)

__all__ = [
    "open_questions",
    "question_capacity",
    "build_question_guidance",
]


def open_questions(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the open questions from a state snapshot, oldest first.

    Args:
        state: A ``MeetingState.to_dict()`` snapshot.

    Returns:
        Question dicts whose status is ``open``, sorted by ``asked_at``.
    """
    questions = state.get("questions") or []
    result = [q for q in questions if q.get("status") == "open"]
    result.sort(key=lambda q: q.get("asked_at") or "")
    return result


def question_capacity(state: Dict[str, Any]) -> int:
    """How many more questions the agent may ask before hitting the open cap."""
    return max(0, MAX_OPEN_QUESTIONS - len(open_questions(state)))


def build_question_guidance(state: Dict[str, Any]) -> str:
    """Render the open-question inbox section of a checkpoint prompt.

    Lists every open question with its id (so the model can emit
    ``resolve_question`` against it) and any pending suggested answer, then
    states the remaining capacity and the confidence thresholds.

    Args:
        state: A ``MeetingState.to_dict()`` snapshot.

    Returns:
        A short multi-line guidance string.
    """
    questions = open_questions(state)
    lines: List[str] = []
    if not questions:
        lines.append("Open questions: none.")
    else:
        lines.append(f"Open questions ({len(questions)}/{MAX_OPEN_QUESTIONS}):")
        for question in questions:
            line = f"- [{question['id']}] {question.get('text', '')}"
            suggested = question.get("suggested_answer")
            if suggested:
                confidence = question.get("suggested_confidence")
                confidence_txt = (
                    f"{confidence:.2f}"
                    if isinstance(confidence, (int, float)) else "?"
                )
                line += (
                    f" — unconfirmed suggested answer (confidence "
                    f"{confidence_txt}): {suggested}"
                )
            lines.append(line)
    lines.append(
        f"You may open {question_capacity(state)} more question(s). "
        f"resolve_question with confidence >= {RESOLVE_CONFIDENCE:g} marks a "
        f"question answered from audio; {SUGGEST_CONFIDENCE:g}-"
        f"{RESOLVE_CONFIDENCE:g} records a greyed suggestion; lower is "
        f"rejected. Prefer firming up questions that already have suggested "
        f"answers over asking new ones."
    )
    return "\n".join(lines)
