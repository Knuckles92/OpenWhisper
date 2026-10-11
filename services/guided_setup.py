"""Conservative, durable eligibility for optional dictation setup.

Initialize before the main window or database can create user-data files.
Only a launch that started without settings and without prior user data is
invited. A persisted pending invitation survives other first-launch writes.
"""

from __future__ import annotations

import logging
from pathlib import Path

from services.settings import SettingsKey, settings_manager

logger = logging.getLogger(__name__)
PENDING = "pending"
STARTED = "started"
SKIPPED = "skipped"
COMPLETED = "completed"
EXISTING = "existing"
STATES = (PENDING, STARTED, SKIPPED, COMPLETED, EXISTING)


def _has_prior_data(root: Path) -> bool:
    # Settings may have been deleted or moved. Do not interrupt users who
    # already have history, recordings, meetings, or an autosaved scratchpad.
    for name in ("openwhisper.db", "transcription_history.json", "scratchpad.txt"):
        if (root / name).exists():
            return True
    for name in ("recordings", "meetings"):
        folder = root / name
        if folder.exists() and next(folder.iterdir(), None) is not None:
            return True
    return False


def initialize(manager=None) -> None:
    """Seed only onboarding state, retaining every other preference."""
    manager = settings_manager if manager is None else manager
    try:
        current = manager.load_all_settings()
        if SettingsKey.GUIDED_SETUP_STATE in current:
            return
        state = EXISTING
        if manager.is_new_install() and not _has_prior_data(
            Path(manager.settings_file).parent
        ):
            state = PENDING

        def seed(settings):
            # Recheck under the store's transaction lock.
            settings.setdefault(SettingsKey.GUIDED_SETUP_STATE, state)

        manager.mutate_settings(seed)
    except Exception:
        # Corrupt/unreadable settings or ambiguous data must not look new.
        logger.exception("Could not initialize guided setup eligibility")


def should_invite(manager=None) -> bool:
    manager = settings_manager if manager is None else manager
    return manager.get(SettingsKey.GUIDED_SETUP_STATE) == PENDING


def save_state(state: str, manager=None) -> None:
    if state not in STATES:
        raise ValueError(f"Unknown guided setup state: {state}")
    manager = settings_manager if manager is None else manager
    manager.save_setting(SettingsKey.GUIDED_SETUP_STATE, state)
