"""Stable destination keys for the unified Settings window.

Callers deep-link into one destination by key, so these never depend on rail
order. ``LEGACY_ALIASES`` keeps the destination names the separate Model
Manager and Downloads windows used working for code that still passes them.
"""
from typing import Final

OVERVIEW: Final[str] = "overview"

# Dictation
VOICE_MODEL: Final[str] = "voice_model"
RECORDING: Final[str] = "recording"
CLEANUP: Final[str] = "cleanup"
CLEANUP_RULES: Final[str] = "cleanup_rules"
CLEANUP_PROFILES: Final[str] = "cleanup_profiles"

# Meeting Mode
MEETING_VOICE: Final[str] = "meeting_voice"
MEETING_INTELLIGENCE: Final[str] = "meeting_intelligence"
MEETING_FAST: Final[str] = "meeting_fast"
MEETING_AFTER: Final[str] = "meeting_after"
MEETING_DASHBOARD: Final[str] = "meeting_dashboard"

# Models & storage
DOWNLOADS: Final[str] = "downloads"
RUNTIME: Final[str] = "runtime"

# App
GENERAL: Final[str] = "general"
HOTKEYS: Final[str] = "hotkeys"
API_KEYS: Final[str] = "api_keys"
ADVANCED: Final[str] = "advanced"

#: Names the retired Model Manager and Downloads windows accepted.
LEGACY_ALIASES: Final[dict] = {
    "ondemand": VOICE_MODEL,
    "text": CLEANUP,
    "meeting": MEETING_VOICE,
    "library": DOWNLOADS,
    "voice": DOWNLOADS,
    "engine_downloads": DOWNLOADS,
}


def resolve_destination(name: str) -> str:
    """Map a destination key or legacy alias to a destination key."""
    return LEGACY_ALIASES.get(name, name)
