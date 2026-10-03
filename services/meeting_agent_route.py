"""Which agent runs a meeting's intelligence, and with what model.

One answer for the live meeting, the archived dashboard, re-runs, and custom
reports. A meeting recorded with an installed agent (Claude Code, Codex,
OpenCode) keeps it, as a meeting recorded on a text endpoint keeps that
endpoint; otherwise the current settings decide.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from services.settings import (
    MeetingAgentCore,
    resolve_meeting_agent_core,
    resolve_meeting_agent_model,
    resolve_meeting_llm_endpoint,
    resolve_meeting_llm_model,
    resolve_meeting_llm_provider,
    settings_manager,
)

@dataclass(frozen=True)
class MeetingAgentRoute:
    """The agent core and model for one meeting.

    Attributes:
        kind: ``MeetingAgentCore`` value.
        provider: Text-endpoint profile id; for an installed agent, its id.
        model: Model id; "" lets an installed agent use its own default.
        endpoint: Text-endpoint snapshot, or None for an installed agent.
        payload_dir: Packaged Pi or OpenCode SDK payload, when selected.
    """

    kind: str
    provider: str
    model: str
    endpoint: Optional[Dict[str, Any]]
    payload_dir: Optional[str] = None

    @property
    def installed(self) -> bool:
        return self.kind in MeetingAgentCore.INSTALLED


def resolve_meeting_agent_route(
    settings: Optional[Mapping[str, Any]] = None,
    meeting: Optional[Mapping[str, Any]] = None,
) -> MeetingAgentRoute:
    """The route for a new meeting, or for ``meeting`` when given.

    Args:
        settings: Loaded settings; read from disk when omitted.
        meeting: A stored meeting row, for re-runs and reports on it.
    """
    from services.components import meeting_agent_payload_dir

    if settings is None:
        settings = settings_manager.load_all_settings()
    meeting = meeting or {}
    recorded = str(meeting.get("agent_provider") or "")
    if recorded in MeetingAgentCore.INSTALLED:
        return MeetingAgentRoute(recorded, recorded, str(meeting.get("agent_model") or ""), None)

    kind = resolve_meeting_agent_core(settings)
    if kind in MeetingAgentCore.INSTALLED:
        return MeetingAgentRoute(kind, kind, resolve_meeting_agent_model(kind, settings), None)

    payload_dir = meeting_agent_payload_dir(kind)
    if not meeting:
        return MeetingAgentRoute(
            kind,
            resolve_meeting_llm_provider(settings),
            resolve_meeting_llm_model(settings),
            resolve_meeting_llm_endpoint(settings),
            payload_dir,
        )
    from services.text_llm import snapshot_from_meeting

    provider = recorded or resolve_meeting_llm_provider(settings)
    return MeetingAgentRoute(
        kind,
        provider,
        str(meeting.get("agent_model") or "") or resolve_meeting_llm_model(settings),
        snapshot_from_meeting(dict(meeting), settings, fallback_provider=provider).to_dict(),
        payload_dir,
    )
