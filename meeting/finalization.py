"""Shared presentation of live and retried finalization steps."""
from collections.abc import Sequence
from typing import Any

# Shared per-block budget for transcript cleanup, including provider tool rounds.
POLISH_TIMEOUT_S = 180.0


def sparse_redecode_detail(new_words: int, old_words: int) -> str:
    return (
        f"Re-transcription produced {new_words} words versus {old_words} in the "
        "live transcript (below the 80% coverage threshold). Kept the live "
        "transcript to avoid losing speech."
    )


STEP_ORDER = (
    "redecode",
    "speaker_id",
    "polish",
    "consolidation",
    "finalize",
)
STEP_NAMES = {
    "redecode": "Audio Re-transcription",
    "speaker_id": "Speaker Identification",
    "polish": "Transcript Cleanup",
    "consolidation": "Summary & Action Items",
    "finalize": "State Finalization",
}
STEP_DETAILS = {
    "redecode": "High-accuracy full session Whisper decode",
    "speaker_id": "OpenAI labels on the system-audio recording",
    "polish": "AI grammar, punctuation, and speaker formatting",
    "consolidation": (
        "Synthesizing executive summary, key points, decisions, "
        "and action items"
    ),
    "finalize": "Saving final transcript and consolidating meeting state",
}


def make_step(step_id: str, status: str = "pending") -> dict[str, Any]:
    return {
        "id": step_id,
        "name": STEP_NAMES[step_id],
        "status": status,
        "detail": STEP_DETAILS[step_id],
    }


def failed_steps_message(steps: Sequence[dict[str, Any]]) -> str:
    names = [
        str(step.get("name") or step.get("id"))
        for step in steps if step.get("status") == "failed"
    ]
    if not names:
        return ""
    details = " ".join(
        f"{step.get('name') or step.get('id')}: {step['detail']}"
        for step in steps
        if step.get("status") == "failed" and step.get("detail")
    )
    return (
        f"{', '.join(names)} failed. "
        "The recording and transcript were kept."
        + (f" {details}" if details else "")
    )
