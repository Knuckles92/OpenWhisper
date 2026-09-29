"""Shared presentation of live and retried finalization steps."""
import logging
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from meeting.interfaces import AgentResult, CheckpointPayload
from services import openai_retirement

logger = logging.getLogger(__name__)

#: How long a canceled agent call gets to unwind before it is abandoned.
CANCEL_GRACE_S = 5.0


def run_agent_call(
    call: Callable[[], AgentResult],
    *,
    cancel: Callable[[], None],
    timeout_s: float,
    name: str,
    on_timeout: Optional[Callable[[], None]] = None,
) -> Optional[AgentResult]:
    """Run one blocking agent call on a daemon thread, bounded by a timeout.

    Every post-meeting agent pass (live and retried polish, consolidation)
    goes through here, so a hung call is canceled the same way everywhere.

    Args:
        call: The blocking agent call, e.g. ``lambda: core.consolidate(p)``.
        cancel: Cancels the agent's in-flight request.
        timeout_s: Seconds to wait before canceling.
        name: Worker thread name, also used in log lines.
        on_timeout: Runs before ``cancel`` on timeout, e.g. to revoke agent
            writes so a worker that ignores cancel cannot land them.

    Returns:
        The call's result (a failed one when it raised), or None on timeout.
    """
    box: dict[str, AgentResult] = {}

    def worker() -> None:
        try:
            box["result"] = call()
        except Exception as exc:
            logger.exception("Agent call %s raised", name)
            box["result"] = AgentResult(ok=False, error=str(exc))

    thread = threading.Thread(target=worker, name=name, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if not thread.is_alive():
        return box.get("result") or AgentResult(ok=False, error="no result")
    logger.warning("%s timed out after %.0fs; canceling", name, timeout_s)
    if on_timeout is not None:
        try:
            on_timeout()
        except Exception:
            logger.exception("Timeout handler for %s raised", name)
    try:
        cancel()
    except Exception:
        logger.exception("Agent cancel raised after %s timed out", name)
    thread.join(timeout=CANCEL_GRACE_S)
    return None

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


def polish_transcript(
    agent: Any,
    store: Any,
    segments: Sequence[dict[str, Any]],
    *,
    timeout_s: float = POLISH_TIMEOUT_S,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> Optional[str]:
    """Clean a transcript block by block, stopping at the first failure.

    Live End (through the meeting's own agent) and the retry (through a
    throwaway one) both run this. Blocks that already succeeded keep their
    edits, so a later failure leaves the pass incomplete rather than done.

    Args:
        agent: An initialized agent core.
        store: The meeting's store; each block carries a fresh snapshot.
        segments: The stored transcript, in timeline order.
        timeout_s: Budget per block.
        progress_cb: Optional ``cb(detail, current_block, total_blocks)``.

    Returns:
        None when every block succeeded, otherwise why the pass stopped.
    """
    blocks = polish_blocks(list(segments))
    total = len(blocks)
    for idx, block in enumerate(blocks, 1):
        if progress_cb is not None:
            try:
                progress_cb(
                    f"Cleaning transcript formatting and grammar "
                    f"(block {idx}/{total}, {len(block)} segments)...",
                    idx,
                    total,
                )
            except Exception:
                logger.exception("Transcript cleanup progress callback failed")
        payload = CheckpointPayload(
            request_id=uuid.uuid4().hex,
            state_snapshot=store.snapshot(),
            new_segments=block,
            is_polish=True,
        )
        started = time.monotonic()
        logger.info(
            "Final polish started meeting_id=%s request_id=%s block=%s/%s "
            "segments=%s timeout_s=%s",
            payload.state_snapshot.get("meeting_id", "unknown"),
            payload.request_id, idx, total, len(block), timeout_s,
        )
        result = run_agent_call(
            lambda bound=payload: agent.checkpoint(bound),
            cancel=agent.cancel,
            timeout_s=timeout_s,
            name="meeting-final-polish",
        )
        elapsed = time.monotonic() - started
        if result is None:
            logger.warning(
                "Final polish timed out request_id=%s block=%s/%s "
                "elapsed_s=%.2f timeout_s=%s; canceled",
                payload.request_id, idx, total, elapsed, timeout_s,
            )
            return (
                f"Transcript cleanup timed out after {timeout_s:g}s on block "
                f"{idx}/{total}. Request ID: {payload.request_id}."
            )
        if not result.ok:
            error = result.error or "transcript cleanup failed"
            logger.warning(
                "Final polish failed request_id=%s block=%s/%s elapsed_s=%.2f "
                "error=%s",
                payload.request_id, idx, total, elapsed, error,
            )
            return (
                f"{error} (block {idx}/{total}; request ID: "
                f"{payload.request_id})"
            )
        logger.info(
            "Final polish completed request_id=%s block=%s/%s elapsed_s=%.2f",
            payload.request_id, idx, total, elapsed,
        )
    return None


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


def saved_state_detail(stats: dict[str, Any]) -> str:
    """The finalize step's detail once the meeting state is saved."""
    return f"Saved {stats['segments']} segments ({stats['words']} words)"


def insights_ready_message(stats: dict[str, Any]) -> str:
    """The card's headline once the final report is ready."""
    parts = [f"{stats['segments']} segments"]
    for key, label in (
        ("key_points", "key points"),
        ("action_items", "action items"),
        ("decisions", "decisions"),
    ):
        if stats.get(key):
            parts.append(f"{stats[key]} {label}")
    return f"Final insights ready — {', '.join(parts)}."


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
