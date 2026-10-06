"""The languages a user dictates in, and the one the next dictation uses.

Switching never reloads the engine and never writes local_asr_language; the
active language is read when a dictation's final pass starts.
"""

from __future__ import annotations

import threading
from typing import Iterable, Mapping, Optional

from services.local_asr import languages as local_languages
from services.settings import (
    LEGACY_API_MODELS,
    MeetingLanguage,
    SettingsKey,
    setting_value,
)

AUTO = MeetingLanguage.AUTO
_WHISPER_CODES = tuple(code for code in MeetingLanguage.ALL if code != AUTO)
_LABELS = {**local_languages.LANGUAGE_LABELS, **dict(MeetingLanguage.CHOICES)}
_REMOTE = "remote"

# What the paired host said its engine accepts, kept by the UI thread whenever
# the Remote engine reports in; read from the transcription worker too.
_remote_lock = threading.Lock()
_remote_family: str = ""
_remote_languages: Optional[tuple[str, ...]] = None
_remote_selected: str = ""


def note_remote_runtime(runtime: Optional[Mapping], engine: Optional[Mapping] = None) -> None:
    """Remember the paired host's languages; None when it is not connected."""
    global _remote_family, _remote_languages, _remote_selected
    family = ""
    codes: Optional[tuple[str, ...]] = None
    selected = ""
    if isinstance(runtime, Mapping):
        family = str(runtime.get("family") or "")
        raw = runtime.get("languages")
        if isinstance(raw, (list, tuple)):
            codes = tuple(code for code in raw if isinstance(code, str))
        chosen = runtime.get("selected")
        if isinstance(chosen, Mapping) and isinstance(chosen.get("language"), str):
            selected = chosen["language"]
    if not family and isinstance(engine, Mapping):
        family = str(engine.get("family") or "")
    with _remote_lock:
        _remote_family = family
        _remote_languages = codes if family else None
        _remote_selected = selected


def _remote_state() -> tuple[str, Optional[tuple[str, ...]], str]:
    with _remote_lock:
        return _remote_family, _remote_languages, _remote_selected


def engine(settings: Mapping) -> str:
    """The recording engine the settings select, such as "parakeet" or "api"."""
    from config import config

    value = settings.get(SettingsKey.SELECTED_MODEL)
    if isinstance(value, str) and value in LEGACY_API_MODELS:
        return "api"
    if isinstance(value, str) and value in (config.MODEL_VALUE_MAP or {}).values():
        return value
    return setting_value(SettingsKey.SELECTED_MODEL, {})


def _whisper_is_english_only(model: str) -> bool:
    if model.endswith(".en") or model.startswith("distil-"):
        return True
    try:
        from services.model_catalog import MODEL_CATALOG
    except Exception:
        return False
    details = MODEL_CATALOG.get(model)
    return details is not None and details.language_support == "English only"


def accepted_languages(settings: Mapping) -> tuple[str, ...]:
    """Every concrete language the current engine accepts, in display order."""
    backend = engine(settings)
    if backend == "local_whisper":
        model = setting_value(SettingsKey.WHISPER_MODEL, settings)
        if isinstance(model, str) and _whisper_is_english_only(model):
            return ("en",)
        return _WHISPER_CODES
    if backend == "api":
        return _WHISPER_CODES
    if backend == _REMOTE:
        family, codes, _selected = _remote_state()
        if family and family != "local_whisper" and codes is not None:
            return tuple(code for code in codes if code != AUTO)
        return _WHISPER_CODES
    return tuple(code for code in local_languages.language_choices(backend) if code != AUTO)


def single_language_reason(settings: Mapping) -> str:
    """Why the current engine takes only one language, or "" when it takes more."""
    if len(accepted_languages(settings)) > 1:
        return ""
    backend = engine(settings)
    if backend == "moonshine":
        return "Moonshine understands English only."
    if backend == "parakeet_mlx":
        return "Parakeet MLX detects the language by itself."
    if backend == "local_whisper":
        return "This Whisper model understands English only. Pick one without “.en” to dictate in other languages."
    if backend == _REMOTE:
        return "The other computer's engine uses one language."
    return "This engine uses one language."


def chosen_languages(settings: Mapping) -> list[str]:
    """``dictation_languages`` as saved: known codes, in order, without repeats."""
    raw = settings.get(SettingsKey.DICTATION_LANGUAGES)
    if not isinstance(raw, (list, tuple)):
        return []
    chosen: list[str] = []
    for code in raw:
        if isinstance(code, str) and code in _LABELS and code != AUTO and code not in chosen:
            chosen.append(code)
    return chosen


def language_choices(settings: Mapping) -> list[str]:
    """Codes from dictation_languages that the current engine accepts."""
    accepted = set(accepted_languages(settings))
    return [code for code in chosen_languages(settings) if code in accepted]


def job_language(settings: Mapping) -> str:
    """The language for a dictation starting now; "" for the engine's own setting."""
    active = settings.get(SettingsKey.DICTATION_ACTIVE_LANGUAGE)
    return active if isinstance(active, str) and active in language_choices(settings) else ""


def engine_language(settings: Mapping) -> str:
    """The language the engine uses on its own: a code, or "auto"."""
    backend = engine(settings)
    if backend == _REMOTE:
        _family, _codes, selected = _remote_state()
        return selected or AUTO
    if backend in ("local_whisper", "api"):
        accepted = accepted_languages(settings)
        return accepted[0] if len(accepted) == 1 else AUTO
    stored = setting_value(SettingsKey.LOCAL_ASR_LANGUAGE, settings)
    return local_languages.selected_language(backend, stored if isinstance(stored, str) else None)


def current_language(settings: Mapping) -> str:
    """What the next dictation uses, for display: its language or the engine's own."""
    return job_language(settings) or engine_language(settings)


def set_active(settings: dict, code: str) -> None:
    """Make ``code`` the active language when it is one of the choices; a mutator."""
    if code in language_choices(settings):
        settings[SettingsKey.DICTATION_ACTIVE_LANGUAGE] = code


def set_chosen(settings: dict, codes: Iterable[str]) -> None:
    """Save the languages a user dictates in, keeping the active one valid; a mutator.

    The active language follows the list: it stays when still chosen, else
    becomes the first choice the engine accepts, so what the overlay shows is
    what the next dictation uses.
    """
    settings[SettingsKey.DICTATION_LANGUAGES] = chosen_languages(
        {SettingsKey.DICTATION_LANGUAGES: list(codes)}
    )
    choices = language_choices(settings)
    active = settings.get(SettingsKey.DICTATION_ACTIVE_LANGUAGE)
    if active not in choices:
        settings[SettingsKey.DICTATION_ACTIVE_LANGUAGE] = choices[0] if choices else ""


def cycle(settings: dict) -> None:
    """Make the next choice active; a ``mutate_settings`` mutator."""
    choices = language_choices(settings)
    if len(choices) < 2:
        return
    current = current_language(settings)
    if current in choices:
        following = choices[(choices.index(current) + 1) % len(choices)]
    else:
        following = choices[0]
    settings[SettingsKey.DICTATION_ACTIVE_LANGUAGE] = following


def label(code: str) -> str:
    """Display name for a language code, such as "English"."""
    return _LABELS.get(code, code)


def short_label(code: str) -> str:
    """Chip text for a language code, such as "EN"."""
    return "AUTO" if code == AUTO else code.upper()
