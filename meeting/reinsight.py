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

import logging
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional

from meeting.agent.base import create_agent_core
from meeting.agent.prompts import build_system_prompt
from meeting.finalization import run_agent_call
from meeting.interfaces import AgentConfig, AgentResult, CheckpointPayload, OpResult
from meeting.state.repair import repair_meeting_state
from meeting.state.store import MeetingStateStore
from meeting.stored import meeting_endpoint as _meeting_endpoint, open_store

logger = logging.getLogger(__name__)


#: Hard wall for one re-run consolidation pass. The sidecar also stalls
#: after ``CONSOLIDATION_STALL_S`` of silence (no Pi events / tool calls).
DEFAULT_TIMEOUT_S = 900.0

__all__ = [
    "AgentUnavailable",
    "StoreToolHost",
    "stored_agent",
    "rerun_insights",
    "DEFAULT_TIMEOUT_S",
]


class StoreToolHost:
    """``AgentToolHost`` over one meeting's state store: patches plus reads.

    The live meeting and every headless re-run hand their agent this host,
    so validation behaves identically in both. The store is built with a
    segment handler so system/diarizer speaker ops can persist; the agent
    still cannot emit ``reassign_segment_speaker`` (agent_forbidden).

    Attributes:
        applied: Running count of ops the store actually applied.
    """

    def __init__(
        self,
        store: Optional[MeetingStateStore] = None,
        repository: Any = None,
        *,
        get_store: Optional[Callable[[], Optional[MeetingStateStore]]] = None,
        writes_allowed: Optional[Callable[[], bool]] = None,
    ) -> None:
        """
        Args:
            store: A stored meeting's store.
            repository: Serves evidence and recall lookups; defaults to the
                store's own.
            get_store: Returns the live meeting's current store (None while
                it has none); used instead of ``store``.
            writes_allowed: Returns False once agent writes are revoked, so
                a late or canceled agent cannot mutate durable state.
        """
        self._current_store = get_store or (lambda: store)
        if repository is None:
            repository = getattr(store, "_repository", None)
        self._repository = repository
        self._writes_allowed = writes_allowed or (lambda: True)
        self.applied = 0

    def _meeting_id(self) -> str:
        return str(getattr(self._current_store(), "meeting_id", "") or "")

    def _refusal(self) -> Optional[str]:
        """Why an agent write must not land now, or None."""
        if self._current_store() is None:
            return "inactive"
        if not self._writes_allowed():
            return "agent_writes_revoked"
        return None

    def apply_agent_ops(self, ops: List[Dict[str, Any]]) -> List[OpResult]:
        """Validate and apply state-patch ops on behalf of the agent."""
        reason = self._refusal()
        if reason:
            return [
                OpResult(ok=False,
                         op=op if isinstance(op, dict) else {"op": op},
                         reason=reason)
                for op in ops
            ]
        return self._record(
            self._current_store().apply("agent", "agent", list(ops))
        )

    def segment_exists(self, segment_id: str) -> bool:
        """Exact-match stored-segment lookup for agent evidence repair."""
        meeting_id = self._meeting_id()
        if not meeting_id or not hasattr(self._repository, "segment_exists"):
            return False
        try:
            return bool(self._repository.segment_exists(meeting_id, segment_id))
        except Exception:
            logger.exception("Segment existence probe failed")
            return False

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

    def search_past_meetings(
        self,
        query: str = "",
        meeting_id: Optional[str] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """Bounded, consent-gated recall of earlier meeting transcripts."""
        from meeting.recall import search_past_meetings as recall

        return recall(
            self._repository,
            query=query,
            current_meeting_id=self._meeting_id(),
            meeting_id=meeting_id,
            limit=limit,
        )

    def search_context_files(
        self,
        query: str = "",
        relative_path: Optional[str] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """Bounded, consent-gated search of the configured knowledge folder."""
        from meeting.context_folder import search_context_files as search

        return search(
            query=query,
            relative_path=relative_path,
            limit=limit,
        )

    def _apply_single(self, op: Dict[str, Any]) -> OpResult:
        reason = self._refusal()
        if reason:
            return OpResult(ok=False, op=op, reason=reason)
        return self._record(self._current_store().apply("agent", "agent", [op]))[0]

    def _record(self, results: List[OpResult]) -> List[OpResult]:
        self.applied += sum(1 for result in results if result.ok)
        return results


class AgentUnavailable(RuntimeError):
    """No agent core could be created for a headless pass."""


@contextmanager
def stored_agent(
    meeting_id: str,
    meeting: Dict[str, Any],
    tools: StoreToolHost,
    *,
    provider: str,
    model: str,
    endpoint: Optional[Dict[str, Any]] = None,
    agent_core_kind: str = "pi",
    sidecar_payload_dir: Optional[str] = None,
) -> Iterator[Any]:
    """A throwaway agent core over a stored meeting, always shut down after.

    The polish retry and the insight re-run both run their pass inside one.

    Args:
        meeting_id: The meeting the agent works on.
        meeting: Its stored row; supplies the recorded endpoint.
        tools: The host the agent acts through.
        provider: LLM provider id.
        model: Model id.
        endpoint: Endpoint snapshot; the row's when omitted.
        agent_core_kind: ``pi``, ``direct``, or an installed agent id.
        sidecar_payload_dir: Directory holding the Pi sidecar payload.

    Yields:
        The initialized core.

    Raises:
        AgentUnavailable: When no core could be created.
    """
    try:
        core = create_agent_core(agent_core_kind, sidecar_payload_dir)
    except Exception as exc:
        logger.exception("Agent core unavailable for a re-run of %s", meeting_id)
        raise AgentUnavailable(str(exc)) from exc
    try:
        core.initialize(
            AgentConfig(
                meeting_id=meeting_id,
                provider=provider,
                model=model,
                api_key=None,  # resolved inside the agent layer
                system_prompt=build_system_prompt(),
                endpoint=endpoint or _meeting_endpoint(meeting),
            ),
            tools,
        )
        yield core
    finally:
        # A leaked sidecar process outlives the request, so shutdown is
        # unconditional.
        try:
            core.shutdown()
        except Exception:
            logger.exception(
                "Agent core shutdown failed after a re-run of %s", meeting_id,
            )


def rerun_insights(repository: Any, meeting_id: str, *, provider: str,
                   model: str, endpoint: Optional[Dict[str, Any]] = None,
                   agent_core_kind: str = "pi",
                   sidecar_payload_dir: Optional[str] = None,
                   store: Optional[MeetingStateStore] = None,
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
        agent_core_kind: ``pi``, ``direct``, or an installed agent id.
        sidecar_payload_dir: Directory holding the Pi sidecar payload.
        store: Optional existing ``MeetingStateStore`` (e.g. from an active engine).
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

    if store is None:
        store = open_store(repository, meeting_id, meeting)
    tools = StoreToolHost(store, repository)

    ok = False
    error: Optional[str] = None
    try:
        with stored_agent(
            meeting_id, meeting, tools,
            provider=provider, model=model, endpoint=endpoint,
            agent_core_kind=agent_core_kind,
            sidecar_payload_dir=sidecar_payload_dir,
        ) as core:
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
            result = run_agent_call(
                lambda: core.consolidate(payload),
                cancel=core.cancel, timeout_s=timeout_s,
                name="meeting-reinsight",
            ) or AgentResult(ok=False, error=f"timed out after {timeout_s:.0f}s")
            ok = bool(result.ok)
            error = None if ok else (result.error or "agent failed")
    except AgentUnavailable as exc:
        return {"ok": False, "state": store.snapshot(), "applied": 0,
                "error": str(exc)}
    except Exception as exc:
        logger.exception("Insight re-run failed for meeting %s", meeting_id)
        error = str(exc)

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
