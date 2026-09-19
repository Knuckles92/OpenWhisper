"""Meeting-side TypeSafe questions, worded exactly as benchmarked.

Each helper wraps one narrow judgment over explicit state and returns
``None`` whenever the judge is unavailable or fails, so callers keep their
deterministic fallback. Thresholds are the values measured against AMI human
labels on September 18, 2026; they are application policy, not properties of
the model.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

from services.typesafe import ChoiceAnswer, TypeSafeJudge

# -- topic boundary --------------------------------------------------------------

#: Measured on 97 human-labelled minute pairs: AUROC 0.845, precision 0.69 and
#: recall 0.78 at this threshold. The shipped Jaccard rule scored precision
#: 0.35 at recall 0.97 on the same windows.
TOPIC_SHIFT_THRESHOLD = 0.5
TOPIC_CHANGED_INSTRUCTIONS = (
    "Does the discussion in `window` move on to a different agenda topic or "
    "activity than the one in `previous_window`? Elaborating the same topic, "
    "or a brief aside that returns to it, is not a change."
)
#: A minute of speech is about a thousand characters; this leaves headroom
#: for a dense window without letting the request grow with the meeting.
_WINDOW_MAX_CHARS = 6_000


def topic_shift_probability(judge: TypeSafeJudge, previous_window: str,
                            window: str) -> Optional[float]:
    """Probability that ``window`` left the topic of ``previous_window``."""
    older = (previous_window or "").strip()
    newer = (window or "").strip()
    if not older or not newer:
        return None
    if len(older) > _WINDOW_MAX_CHARS:
        older = older[-_WINDOW_MAX_CHARS:]
    if len(newer) > _WINDOW_MAX_CHARS:
        newer = newer[:_WINDOW_MAX_CHARS]
    return judge.noul({"previous_window": older, "window": newer},
                      TOPIC_CHANGED_INSTRUCTIONS)


# -- voice commands ------------------------------------------------------------

#: Measured on 884 real ASR segments: zero false positives at any confidence,
#: 41/41 authored commands recognised, 40/41 at 0.8. Person-directed requests
#: such as "write that down for me" also fire, which is why callers must gate
#: on a wake name in code before asking.
VOICE_COMMAND_CONFIDENCE = 0.6
VOICE_COMMAND_CRITERIA: Mapping[str, str] = {
    "none": "Ordinary meeting speech between participants, including talking about notes, decisions or topics without instructing the assistant.",
    "mark_decision": "Instructs the note-taking assistant to record the preceding point as a decision.",
    "mark_action": "Instructs the assistant to record an action item or to-do, possibly with an owner or deadline.",
    "note_this": "Instructs the assistant to write down or capture the preceding point in the notes.",
    "recap": "Asks the assistant to summarize, read back, or recap decisions, actions or progress.",
    "fix_transcript": "Tells the assistant that a word, name or number in the transcript is wrong and gives the correction.",
    "set_topic": "Tells the assistant that the meeting is moving to a new topic or section.",
}
VOICE_COMMAND_INSTRUCTIONS = (
    "Is `segment` an instruction directed at the automated note-taking "
    "assistant, which participants address using one of `assistant_names` or "
    "by referring explicitly to the notes it keeps? Speech directed at another "
    "person is none, even if it mentions notes, decisions or topics. "
    "`previous_segments` is context only."
)


def voice_command_choice(judge: TypeSafeJudge, segment: str,
                         previous_segments: Sequence[str],
                         assistant_names: Sequence[str]) -> Optional[ChoiceAnswer]:
    """Classify a wake-named segment as one command or ``none``."""
    text = (segment or "").strip()
    if not text:
        return None
    state: Dict[str, Any] = {
        "previous_segments": [p for p in previous_segments if p][-3:],
        "segment": text,
        "assistant_names": list(assistant_names),
    }
    return judge.choice(state, VOICE_COMMAND_INSTRUCTIONS, VOICE_COMMAND_CRITERIA)
