"""Offer a detected Claude Code or Codex as the AI cleanup runner.

Cleanup starts on the built-in OpenRouter default, which needs an API key.
A coding agent the user is already signed in to needs none, so the first
time cleanup is on and no runner was ever chosen, the UI asks once whether
to use the agent instead. Nothing here switches without that answer.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from services.installed_agents import InstalledAgent
from services.settings import SettingsKey, TranscriptCleanupProvider, settings_manager

#: Agents offered, preferred first.
OFFERED = (TranscriptCleanupProvider.CLAUDE_CODE, TranscriptCleanupProvider.CODEX)


def pending(settings: Optional[Dict[str, Any]] = None) -> bool:
    """True while cleanup is on, its runner is the untouched default, and
    the offer has not been answered."""
    if settings is None:
        settings = settings_manager.load_all_settings()
    return (
        SettingsKey.TRANSCRIPT_CLEANUP_ENABLED.read(settings)
        and SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER.stored(settings) is None
        and not SettingsKey.CLEANUP_AGENT_OFFER_ANSWERED.read(settings)
    )


def offerable(scan: Optional[Mapping[str, Optional[InstalledAgent]]]) -> List[InstalledAgent]:
    """The offered agents ``scan`` found installed and signed in."""
    agents = [(scan or {}).get(agent_id) for agent_id in OFFERED]
    return [
        agent for agent in agents
        if agent is not None and not agent.problem and agent.signed_in is True
    ]


def accept(agent_id: str, manager=None) -> None:
    """Run cleanup through ``agent_id`` with the model last used for it,
    or the agent's own default."""
    if agent_id not in OFFERED:
        raise ValueError(f"Not an offered cleanup agent: {agent_id}")

    def merge(settings: dict) -> None:
        memory = dict(SettingsKey.CLEANUP_MODEL_MEMORY.read(settings))
        model = memory.get(agent_id, "")
        memory[agent_id] = model if isinstance(model, str) else ""
        settings[SettingsKey.CLEANUP_MODEL_MEMORY] = memory
        settings[SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER] = agent_id
        settings[SettingsKey.TRANSCRIPT_CLEANUP_MODEL] = memory[agent_id]
        settings[SettingsKey.CLEANUP_AGENT_OFFER_ANSWERED] = True

    (manager or settings_manager).mutate_settings(merge)


def decline(manager=None) -> None:
    """Keep the current runner and never ask again."""
    (manager or settings_manager).save_setting(
        SettingsKey.CLEANUP_AGENT_OFFER_ANSWERED, True,
    )
