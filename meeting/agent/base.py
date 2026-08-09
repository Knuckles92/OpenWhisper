"""Agent-core factory and shared helpers for the meeting-intelligence layer.

The rest of the engine talks to an agent core exclusively through the
``AgentCore``/``AgentToolHost`` protocols from :mod:`meeting.interfaces`;
``create_agent_core`` picks the concrete implementation (Pi sidecar or direct
OpenRouter) and handles graceful fallback when the sidecar bundle is missing.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

# Re-exported for convenience so agent implementations and the engine can do
# ``from meeting.agent.base import AgentCore, AgentToolHost``.
from meeting.interfaces import AgentCore, AgentToolHost  # noqa: F401

logger = logging.getLogger(__name__)

#: File name of the compiled Pi sidecar bundle inside its payload directory.
SIDECAR_BUNDLE_NAME = "bundle.cjs"

_ENV_KEYS = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
}

__all__ = [
    "AgentCore",
    "AgentToolHost",
    "SIDECAR_BUNDLE_NAME",
    "create_agent_core",
    "find_provider_api_key",
]


def find_provider_api_key(provider: str) -> Optional[str]:
    """Resolve the API key for an LLM provider.

    Prefers the app's shared resolution (environment variables plus the
    ``.env`` file) via ``services.transcript_cleanup.find_api_key``; falls
    back to plain environment variables when the ``meeting`` package is used
    standalone and the services layer is unavailable.

    Args:
        provider: Provider id (``openrouter`` or ``openai``).

    Returns:
        The API key string, or None when no key is available.
    """
    try:
        from services.transcript_cleanup import find_api_key

        key = find_api_key(provider)
        if key:
            return key
    except Exception:
        logger.debug(
            "services.transcript_cleanup unavailable; falling back to "
            "environment variables for the %s API key", provider,
        )
    return os.getenv(_ENV_KEYS.get(provider, "OPENAI_API_KEY"))


def create_agent_core(kind: str, payload_dir: Optional[str] = None) -> AgentCore:
    """Create the meeting-intelligence agent core.

    Args:
        kind: ``pi`` for the bundled Node Pi sidecar, ``direct`` for the
            in-process OpenRouter/OpenAI agent.
        payload_dir: Directory holding the sidecar payload (``bundle.cjs``
            and optionally a portable ``node.exe``). Required for ``pi``.

    Returns:
        An ``AgentCore`` implementation. When ``pi`` is requested but the
        sidecar bundle is missing, falls back to the direct agent with a
        logged warning rather than failing the meeting.
    """
    # Imported lazily to avoid import cycles and keep optional dependencies
    # (the openai SDK) out of the factory's import path.
    from meeting.agent.openrouter_direct import DirectOpenRouterAgent

    if kind == "pi":
        bundle_path = (
            os.path.join(payload_dir, SIDECAR_BUNDLE_NAME) if payload_dir else None
        )
        if bundle_path and os.path.isfile(bundle_path):
            from meeting.agent.pi_sidecar import PiSidecarAgent

            return PiSidecarAgent(payload_dir)
        logger.warning(
            "Pi sidecar bundle not found (payload_dir=%r); falling back to "
            "the direct OpenRouter agent core", payload_dir,
        )
        return DirectOpenRouterAgent()

    if kind != "direct":
        logger.warning(
            "Unknown agent core kind %r; using the direct OpenRouter agent core",
            kind,
        )
    return DirectOpenRouterAgent()
