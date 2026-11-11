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
    return SettingsKey.GUIDED_SETUP_STATE.read(manager.load_all_settings()) == PENDING


def is_completed(manager=None) -> bool:
    manager = settings_manager if manager is None else manager
    return SettingsKey.GUIDED_SETUP_STATE.read(manager.load_all_settings()) == COMPLETED


def save_state(state: str, manager=None) -> None:
    if state not in STATES:
        raise ValueError(f"Unknown guided setup state: {state}")
    manager = settings_manager if manager is None else manager
    manager.save_setting(SettingsKey.GUIDED_SETUP_STATE, state)


def speech_model_name(settings: dict) -> str | None:
    """The local model dictation runs, or None for an API server or a remote host."""
    from services.local_asr.catalog import BACKENDS, selected_model
    engine = SettingsKey.SELECTED_MODEL.read(settings)
    if engine == "local_whisper":
        model = SettingsKey.WHISPER_MODEL.read(settings)
        if model == "auto":
            # "auto" names no repository; ask for the model it resolves to.
            from transcriber.local_backend import LocalWhisperBackend

            model = LocalWhisperBackend(model_name="auto", load=False).resolve_auto_model()
        return model
    if engine in BACKENDS:
        return selected_model(engine, settings)
    return None


def speech_model_downloaded(settings: dict) -> bool | None:
    """Whether dictation's local model and runtime are on this computer.

    None when dictation does not run a local model. Reads the disk only;
    never downloads or probes a server.
    """
    from services.hf_access import is_model_cached
    from services.local_asr.catalog import missing_runtime

    model = speech_model_name(settings)
    if model is None:
        return None
    if missing_runtime(model, settings):
        return False
    try:
        return is_model_cached(model)
    except Exception:
        logger.debug("Could not check the speech model cache", exc_info=True)
        return None
