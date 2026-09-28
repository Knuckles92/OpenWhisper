"""Shared presentation of live and retried finalization steps."""
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from services import openai_retirement

logger = logging.getLogger(__name__)

# Shared per-block budget for transcript cleanup, including provider tool rounds.
POLISH_TIMEOUT_S = 180.0

# Cleanup emits a revision for each changed segment. Large batches can spend
# the entire RPC budget reasoning before applying even one edit. Bound both
# per-segment work and text volume, including a little boundary context.
POLISH_MAX_SEGMENTS = 80
POLISH_MAX_TEXT_CHARS = 8_000
POLISH_OVERLAP_SEGMENTS = 8


def polish_blocks(
    segments: Sequence[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Cover the transcript with bounded, overlapping cleanup windows.

    A single oversized segment stays intact: splitting it would change its
    identity and let separate revisions overwrite parts of the same speech.
    Overlap shrinks when needed so every block advances into unseen segments.
    """
    blocks: list[list[dict[str, Any]]] = []
    start = 0
    covered = 0
    while covered < len(segments):
        end = start
        chars = 0
        while end < len(segments) and end - start < POLISH_MAX_SEGMENTS:
            size = len(segments[end].get("text") or "")
            if end > start and chars + size > POLISH_MAX_TEXT_CHARS:
                break
            chars += size
            end += 1
        if end <= covered:
            # Context consumed the text budget; retry without that overlap.
            start = covered
            continue
        blocks.append(list(segments[start:end]))
        covered = end
        overlap = min(POLISH_OVERLAP_SEGMENTS, (end - start) // 4)
        start = end - overlap
    return blocks


def summary_stats(
    cards: Any,
    questions: Any,
    transcript: Sequence[Any],
    duration_s: float,
) -> dict[str, Any]:
    """Counts shown on the finalization card, shared by live End and retry.

    Soft-deleted items (``status == "removed"``) are excluded so the card
    matches what the dashboard and exports display.

    Args:
        cards: ``{card_key: [item, ...]}`` with dict or ``CardItem`` items.
        questions: Question list (dicts or ``Question`` objects).
        transcript: Segment dicts or ``TranscriptSegment`` objects.
        duration_s: Recorded meeting seconds, pauses excluded.
    """
    def _status(item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get("status") or "")
        return str(getattr(item, "status", "") or "")

    def _live(items: Any) -> int:
        return sum(1 for item in (items or []) if _status(item) != "removed")

    def _text(seg: Any) -> str:
        if isinstance(seg, dict):
            return str(seg.get("text") or "")
        return str(getattr(seg, "text", "") or "")

    cards = cards if isinstance(cards, dict) else {}
    return {
        "segments": len(transcript),
        "words": sum(len(_text(seg).split()) for seg in transcript),
        "key_points": _live(cards.get("key_points")),
        "action_items": _live(cards.get("action_items")),
        "decisions": _live(cards.get("decisions")),
        "risks": _live(cards.get("risks")),
        "questions": sum(
            1 for q in (questions or []) if _status(q) != "dismissed"
        ),
        "duration_s": max(0.0, float(duration_s or 0.0)),
    }


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


NOT_OPENAI_REASON = "Speaker identification is not set to OpenAI."
NO_CONSENT_REASON = "Audio-upload consent has not been given."
NO_KEY_REASON = "No OpenAI API key is configured."


@dataclass(frozen=True)
class SpeakerPassGate:
    """Whether the OpenAI speaker pass may upload a meeting's system audio.

    Attributes:
        ok: True only when every condition holds.
        reason: Why the pass is refused; empty when ``ok``.
        offered: The user chose OpenAI labels and OpenAI still serves them,
            so a refusal is one they can fix (consent or key).
        api_key: The key to upload with; empty unless ``ok``.
    """
    ok: bool
    reason: str = ""
    offered: bool = False
    api_key: str = field(default="", repr=False)


def speaker_pass_gate(
    *,
    backend: str,
    consent: bool,
    find_key: Optional[Callable[[], Optional[str]]] = None,
    today: Optional[date] = None,
) -> SpeakerPassGate:
    """The one eligibility check for every OpenAI speaker pass.

    Live End, the dashboard's re-run, and the desktop retry all call this, so
    none of them can upload system audio the others would refuse. A refused
    pass is skipped, never failed.

    Args:
        backend: The speaker-identification backend (``off``, ``local``,
            ``openai``).
        consent: Whether the user approved uploading meeting audio.
        find_key: Returns the OpenAI key. Called only when every other check
            passes. ``None`` means no key is needed (an injected decoder).
        today: Pins the retirement date check (tests).
    """
    if backend != "openai":
        return SpeakerPassGate(ok=False, reason=NOT_OPENAI_REASON)
    if openai_retirement.retired(today):
        return SpeakerPassGate(
            ok=False, reason=openai_retirement.SPEAKER_MODEL_RETIRED_MESSAGE,
        )
    if not consent:
        return SpeakerPassGate(ok=False, reason=NO_CONSENT_REASON, offered=True)
    api_key = ""
    if find_key is not None:
        try:
            api_key = find_key() or ""
        except Exception:
            logger.exception("Could not resolve the OpenAI API key")
        if not api_key:
            return SpeakerPassGate(ok=False, reason=NO_KEY_REASON, offered=True)
    return SpeakerPassGate(ok=True, offered=True, api_key=api_key)


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
