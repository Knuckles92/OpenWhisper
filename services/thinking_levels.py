"""Which thinking levels a runner and model accept, and the one that runs.

One saved level (``TranscriptCleanupReasoning``) is shared by every runner,
but runners accept different sets: Claude Code goes up to Max, a Codex model
lists its own, an API model's catalog entry adds or removes levels, and
agents have no Off. Settings offers :func:`levels_for`; the runtime sends
:func:`effective_level`, the saved level or the nearest one offered.
"""
from __future__ import annotations

from typing import Optional, Tuple

from services.settings import TranscriptCleanupProvider, TranscriptCleanupReasoning

ORDER: tuple[str, ...] = TranscriptCleanupReasoning.ALL

LABELS = {
    "off": "Off",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
    "max": "Max",
    "ultra": "Ultra",
}

#: What an API reasoning model takes unless its catalog entry says otherwise.
_ENDPOINT_BASE = ("off", "low", "medium", "high")
#: ``claude --effort`` (checked with Claude Code 2.1.306).
_CLAUDE_CODE = ("low", "medium", "high", "xhigh", "max")
#: Codex models outside its cache, and OpenCode, whose levels depend on the
#: model and are only known inside a session.
_AGENT_FALLBACK = ("low", "medium", "high")


def _ordered(levels) -> tuple[str, ...]:
    return tuple(level for level in ORDER if level in set(levels))


def agent_levels(agent_id: str, model: str) -> tuple[str, ...]:
    """Levels an installed agent takes as its reasoning effort for ``model``."""
    if agent_id == TranscriptCleanupProvider.CLAUDE_CODE:
        return _CLAUDE_CODE
    if agent_id == TranscriptCleanupProvider.CODEX:
        from services.installed_agents import (
            codex_model_efforts,
            configured_default_model,
        )

        efforts = codex_model_efforts()
        chosen = model or configured_default_model(agent_id)
        if chosen in efforts:
            return _ordered(efforts[chosen])
        if efforts:
            # An unknown or unnamed model: what every listed model takes.
            common = set.intersection(*(set(levels) for levels in efforts.values()))
            if common:
                return _ordered(common)
    return _AGENT_FALLBACK


def endpoint_levels(profile, model: str) -> tuple[str, ...]:
    """Levels a text endpoint's model takes; () when it has no control."""
    from services.text_llm import PROFILE_KIND_OPENAI, PROFILE_KIND_OPENROUTER
    from services.text_model_catalog import model_spec

    if profile is None:
        return ()
    spec = None
    if model:
        try:
            spec = model_spec(profile, model)
        except ValueError:
            spec = None
    if profile.kind == PROFILE_KIND_OPENAI:
        supported = spec is None or spec.reasoning
    else:
        # OpenRouter takes a reasoning object for any model; a server the
        # user runs says per model which control it accepts.
        supported = profile.kind == PROFILE_KIND_OPENROUTER or (
            spec is not None and spec.reasoning_format in ("openai", "deepseek")
        )
    if not supported:
        return ()
    levels = set(_ENDPOINT_BASE)
    for level, wire in ((spec.thinking_levels if spec else None) or {}).items():
        if wire is None:
            levels.discard(level)
        else:
            levels.add(level)
    return _ordered(levels)


def levels_for(provider: str, model: str, settings=None) -> tuple[str, ...]:
    """Levels the cleanup runner ``provider`` (an agent or endpoint) offers."""
    if provider in TranscriptCleanupProvider.INSTALLED:
        return agent_levels(provider, model)
    from services.text_llm import get_profile

    return endpoint_levels(get_profile(provider, settings), model)


def nearest(level: str, offered: tuple[str, ...]) -> str | None:
    """``level`` when offered, else the closest offered one (the lower on a tie)."""
    if not offered:
        return None
    if level in offered:
        return level
    index = ORDER.index(level) if level in ORDER else 0
    return min(offered, key=lambda option: (abs(ORDER.index(option) - index),
                                            ORDER.index(option)))


def effective_level(level: str, offered: tuple[str, ...]) -> str:
    """What to send for a saved level: the nearest offered, or "off" if none."""
    return nearest(level, offered) or "off"
