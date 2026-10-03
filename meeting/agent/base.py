"""Agent-core factory and shared helpers for the meeting-intelligence layer.

The rest of the engine talks to an agent core exclusively through the
``AgentCore``/``AgentToolHost`` protocols from :mod:`meeting.interfaces`;
``create_agent_core`` picks an SDK-backed or installed coding agent. Missing
agents fail explicitly; meeting recording can continue without AI insights.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

# Re-exported for convenience so agent implementations and the engine can do
# ``from meeting.agent.base import AgentCore, AgentToolHost``.
from meeting.interfaces import AgentCore, AgentToolHost  # noqa: F401

#: File name of the compiled Pi sidecar bundle inside its payload directory.
SIDECAR_BUNDLE_NAME = "bundle.cjs"

#: How long a consolidation pass may stay silent (no Pi events, no tool
#: calls) before we treat it as hung. Flash-class reasoning can sit this
#: long before the first token; progress notifications reset the clock.
CONSOLIDATION_STALL_S = 300.0
#: Hard wall even when the agent keeps reporting progress, so a runaway
#: tool loop cannot block finalization forever.
CONSOLIDATION_TIMEOUT_CAP_S = 900.0

__all__ = [
    "AgentCore",
    "AgentToolHost",
    "SIDECAR_BUNDLE_NAME",
    "CONSOLIDATION_STALL_S",
    "CONSOLIDATION_TIMEOUT_CAP_S",
    "create_agent_core",
    "find_provider_api_key",
    "merge_usage",
]


def find_provider_api_key(provider: str, endpoint: Optional[Any] = None) -> Optional[str]:
    """Resolve the API key a meeting's text endpoint needs.

    Args:
        provider: Profile id (``openrouter``, ``openai``, or ``custom_…``).
        endpoint: The meeting's persisted endpoint snapshot, when it has one.

    Returns:
        The API key string, a placeholder for auth-free endpoints, or None
        when a required key is missing.
    """
    from services.text_llm import profile_from_agent_config, resolve_api_key

    return resolve_api_key(profile_from_agent_config(provider, endpoint))


def merge_usage(total: Dict[str, Any], usage: Any) -> None:
    """Add one model response's token usage to a running ``total``.

    Args:
        total: Accumulator; gains ``prompt_tokens``, ``completion_tokens``,
            ``total_tokens`` and a ``requests`` count.
        usage: The response's usage object, or None when it reported none.
    """
    if usage is None:
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value
    total["requests"] = total.get("requests", 0) + 1


def create_agent_core(kind: str, payload_dir: Optional[str] = None) -> AgentCore:
    """Create the meeting-intelligence agent core.

    Args:
        kind: ``pi`` or ``opencode`` for a packaged SDK, or an installed agent
            (``claude_code``, ``codex``, ``opencode_cli``). The retired
            ``direct`` value is migrated to Pi for older callers.
        payload_dir: Directory holding the sidecar payload (``bundle.cjs``
            and optionally a portable ``node.exe``). Required for ``pi``.

    Returns:
        An ``AgentCore`` implementation. No core falls back to direct API
        calls or a different agent.

    Raises:
        RuntimeError: A required packaged agent is unavailable.
        ValueError: The requested kind is not supported.
    """
    # Imported lazily to avoid import cycles.
    from services.settings import MeetingAgentCore

    if kind in MeetingAgentCore.INSTALLED:
        from meeting.agent.installed.core import InstalledAgentCore

        return InstalledAgentCore(kind)

    if kind == MeetingAgentCore.DIRECT:
        kind = MeetingAgentCore.PI

    if kind == MeetingAgentCore.OPENCODE:
        if not payload_dir:
            raise RuntimeError("Install OpenCode v2 from Downloads to enable meeting intelligence.")
        from meeting.agent.opencode_sidecar import OpenCodeSidecarAgent

        return OpenCodeSidecarAgent(payload_dir)

    if kind == MeetingAgentCore.PI:
        bundle_path = (
            os.path.join(payload_dir, SIDECAR_BUNDLE_NAME) if payload_dir else None
        )
        if bundle_path and os.path.isfile(bundle_path):
            from meeting.agent.pi_sidecar import PiSidecarAgent

            return PiSidecarAgent(payload_dir)
        raise RuntimeError(
            "Pi is unavailable. Install or update Pi from Downloads → Components, "
            "or choose an installed coding agent in Settings → Meeting Mode → "
            "Intelligence. Meetings can still record without AI insights."
        )

    raise ValueError(f"Unsupported meeting agent core: {kind!r}")
