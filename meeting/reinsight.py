"""Headless re-run of the meeting-intelligence consolidation pass.

A meeting recorded with cloud intelligence off — or one whose agent core was
offline while it ran — ends up with a faithful transcript and an empty
dashboard. This module regenerates that dashboard afterwards, from history,
without a ``MeetingEngine``: it rebuilds the stored ``MeetingState``, wraps it
in a ``MeetingStateStore`` (so write-through persistence, the audit trail, and
human-overrides-agent protection all behave exactly as they do live), and runs
one ``consolidate`` pass over the complete stored transcript.

The state store is the only writer, so a re-run can never overwrite items a
human pinned, edited, or confirmed — the same guarantee a live checkpoint has.

No Qt imports; this package stays standalone-extractable.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Any, Dict, List, Optional

from meeting.agent.base import create_agent_core
from meeting.agent.prompts import build_system_prompt
from meeting.interfaces import AgentConfig, AgentResult, CheckpointPayload, OpResult
from meeting.state.repair import repair_meeting_state
from meeting.state.schema import MeetingState
from meeting.state.store import MeetingStateStore

logger = logging.getLogger(__name__)

#: Budget for one re-run consolidation pass. Generous: it covers a whole
#: meeting's transcript, not a rolling checkpoint window.
DEFAULT_TIMEOUT_S = 180.0

__all__ = ["rerun_insights", "DEFAULT_TIMEOUT_S"]


class _OfflineToolHost:
    """``AgentToolHost`` for a stored meeting: state patches, nothing else.

    Mirrors ``MeetingEngine``'s tool-host implementation op-for-op so the
    validation layer behaves identically to a live checkpoint. Speaker
    reassignment is deliberately unavailable — consolidation never touches the
    segment log — so the store is built without a segment handler and such ops
    are rejected with ``segments_unavailable``.

    Attributes:
        applied: Running count of ops the store actually applied.
    """

    def __init__(self, store: MeetingStateStore) -> None:
        """Args:
            store: The store owning the meeting's state document.
        """
        self._store = store
        self.applied = 0

    def apply_agent_ops(self, ops: List[Dict[str, Any]]) -> List[OpResult]:
        """Validate and apply state-patch ops on behalf of the agent."""
        return self._record(self._store.apply("agent", "agent", list(ops)))

    def ask_question(self, text: str, evidence: List[str]) -> OpResult:
        """Add a question to the quiet inbox (agent tool)."""
        return self._apply_single({
            "op": "ask_question", "text": text,
            "evidence": list(evidence or []),
        })

    def resolve_question(self, question_id: str, answer_text: str,
                         confidence: float, evidence: List[str]) -> OpResult:
        """Answer an open question from audio evidence (agent tool)."""
        return self._apply_single({
            "op": "resolve_question", "question_id": question_id,
            "answer_text": answer_text, "confidence": confidence,
            "evidence": list(evidence or []),
        })

    def _apply_single(self, op: Dict[str, Any]) -> OpResult:
        return self._record(self._store.apply("agent", "agent", [op]))[0]

    def _record(self, results: List[OpResult]) -> List[OpResult]:
        self.applied += sum(1 for result in results if result.ok)
        return results


def _load_state(meeting: Dict[str, Any], meeting_id: str) -> MeetingState:
    """Rebuild the meeting's state document from its stored snapshot.

    Args:
        meeting: The repository meeting row.
        meeting_id: Id of the meeting being re-analyzed.

    Returns:
        The deserialized ``MeetingState``, or a fresh one when the meeting has
        no snapshot yet (the transcript-only case) or the snapshot is corrupt.
    """
    raw = meeting.get("state_json")
    if raw:
        try:
            return MeetingState.from_dict(json.loads(raw))
        except Exception:
            logger.exception(
                "Corrupt state_json for meeting %s; starting from a fresh state",
                meeting_id,
            )
    return MeetingState(
        meeting_id=meeting_id,
        title=meeting.get("title", ""),
        # Carried over so the store's write-through cannot flip the recorded
        # cloud flag on a meeting that simply never got a snapshot.
        cloud_enabled=bool(meeting.get("cloud_enabled")),
    )


def _consolidate(core: Any, payload: CheckpointPayload,
                 timeout_s: float) -> AgentResult:
    """Run one bounded ``consolidate`` call on a worker thread.

    Args:
        core: The initialized ``AgentCore``.
        payload: The consolidation payload.
        timeout_s: Maximum seconds to wait before canceling the agent.

    Returns:
        The agent's result, or a failed ``AgentResult`` on timeout or raise.
    """
    box: Dict[str, AgentResult] = {}

    def worker() -> None:
        try:
            box["result"] = core.consolidate(payload)
        except Exception as exc:
            logger.exception("Agent consolidate raised during insight re-run")
            box["result"] = AgentResult(ok=False, error=str(exc))

    thread = threading.Thread(target=worker, name="meeting-reinsight",
                              daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        logger.warning("Insight re-run timed out after %.0fs; canceling",
                       timeout_s)
        try:
            core.cancel()
        except Exception:
            logger.exception("Agent cancel raised during insight re-run")
        thread.join(timeout=5.0)
        return AgentResult(ok=False, error=f"timed out after {timeout_s:.0f}s")
    return box.get("result") or AgentResult(ok=False, error="no result")


def rerun_insights(repository: Any, meeting_id: str, *, provider: str,
                   model: str, agent_core_kind: str = "pi",
                   sidecar_payload_dir: Optional[str] = None,
                   timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """Regenerate a past meeting's insights from its stored transcript.

    Runs one consolidation pass through a throwaway agent core. Every change
    goes through the meeting's state store, so persistence, the audit trail,
    and protection of human-touched content are identical to a live meeting.

    Args:
        repository: A ``MeetingRepository``.
        meeting_id: The meeting to re-analyze.
        provider: LLM provider id for the agent core (e.g. ``openrouter``).
        model: Model id for the agent core.
        agent_core_kind: ``pi`` for the bundled sidecar, ``direct`` otherwise.
        sidecar_payload_dir: Directory holding the Pi sidecar payload.
        timeout_s: Budget for the consolidation pass.

    Returns:
        ``{'ok': bool, 'state': dict, 'applied': int, 'error': str | None}`` —
        ``state`` is the post-pass snapshot and ``applied`` counts the ops the
        store accepted. Agent failures are reported here, not raised.

    Raises:
        ValueError: When the meeting is unknown or has no transcript.
    """
    meeting = repository.get_meeting(meeting_id)
    if meeting is None:
        raise ValueError("unknown meeting")
    segments = repository.get_segments(meeting_id)
    if not segments:
        raise ValueError("meeting has no transcript")

    store = MeetingStateStore(
        _load_state(meeting, meeting_id),
        repository=repository,
        segment_exists=lambda segment_id: repository.segment_exists(
            meeting_id, segment_id
        ),
    )
    tools = _OfflineToolHost(store)

    try:
        core = create_agent_core(agent_core_kind, sidecar_payload_dir)
    except Exception as exc:
        logger.exception("Agent core unavailable for insight re-run")
        return {"ok": False, "state": store.snapshot(), "applied": 0,
                "error": str(exc)}

    ok = False
    error: Optional[str] = None
    try:
        core.initialize(
            AgentConfig(
                meeting_id=meeting_id,
                provider=provider,
                model=model,
                api_key=None,  # resolved inside the agent layer
                system_prompt=build_system_prompt(),
            ),
            tools,
        )
        payload = CheckpointPayload(
            request_id=uuid.uuid4().hex,
            state_snapshot=store.snapshot(),
            new_segments=segments,
            is_consolidation=True,
        )
        logger.info(
            "Re-running insights for meeting %s over %d segments "
            "(core=%s provider=%s model=%s)",
            meeting_id, len(segments), agent_core_kind, provider, model,
        )
        result = _consolidate(core, payload, timeout_s)
        ok = bool(result.ok)
        error = None if ok else (result.error or "agent failed")
    except Exception as exc:
        logger.exception("Insight re-run failed for meeting %s", meeting_id)
        error = str(exc)
    finally:
        # A leaked sidecar process outlives the request, so shutdown is
        # unconditional.
        try:
            core.shutdown()
        except Exception:
            logger.exception("Agent core shutdown failed after insight re-run")

    # Structural repair: gpt-4o-mini often ships key points + summary but
    # leaves timeline empty. Promote evidenced key points (or sample the
    # transcript) so the durable record always has story beats.
    repaired = repair_meeting_state(store, segments)
    tools.applied += repaired

    # No explicit snapshot write: the store's write-through already persists
    # state_json/state_seq on every applied batch (see
    # SqlMeetingRepository.on_ops_applied), so a second write would only
    # duplicate it.
    logger.info("Insight re-run for meeting %s finished: ok=%s applied=%d",
                meeting_id, ok, tools.applied)
    return {"ok": ok, "state": store.snapshot(), "applied": tools.applied,
            "error": error}
