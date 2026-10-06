"""Named, on-demand output formats, independent of standard dictation."""

from dataclasses import asdict, dataclass

from config import config
from services.hotkey_manager import format_hotkey, parse_hotkey
from services.settings import (
    SettingsKey,
    compose_transcript_cleanup_prompt,
    setting_value,
)


@dataclass(frozen=True)
class CleanupProfile:
    id: str
    name: str
    instructions: str
    hotkey: str = ""
    use_learned_rules: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


STARTER_PROFILES = (
    CleanupProfile(
        "support-ticket",
        "Support ticket",
        "Format the dictation as a clear support ticket with a short Title, "
        "Summary, Steps to reproduce, Expected behavior, Actual behavior, and "
        "Environment. Use numbered steps when provided. Omit sections whose "
        "information was not spoken. Keep error messages and technical details exact.",
    ),
    CleanupProfile(
        "email",
        "Email",
        "Turn the dictation into a concise, professional email with a Subject "
        "line and a readable body. Preserve the speaker's intent and requests. "
        "Use short paragraphs. Include a recipient, greeting, or signature only "
        "when supplied; do not invent names or commitments.",
    ),
)


def load_cleanup_profiles(settings: dict) -> list[CleanupProfile]:
    """Read valid profiles; starters appear only before a library is saved."""
    raw = settings.get(SettingsKey.TRANSCRIPT_CLEANUP_PROFILES)
    if raw is None:
        return list(STARTER_PROFILES)
    if not isinstance(raw, list):
        return []
    profiles = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        values = [item.get(key) for key in ("id", "name", "instructions")]
        if any(not isinstance(value, str) or not value.strip() for value in values):
            continue
        profile_id, name, instructions = (value.strip() for value in values)
        if profile_id in seen:
            continue
        seen.add(profile_id)
        hotkey = item.get("hotkey", "")
        profiles.append(
            CleanupProfile(
                profile_id,
                name,
                instructions,
                normalize_hotkey(hotkey) if isinstance(hotkey, str) else "",
                item.get("use_learned_rules", True) is not False,
            )
        )
    return profiles


def find_cleanup_profile(settings: dict, profile_id: str) -> CleanupProfile | None:
    return next(
        (p for p in load_cleanup_profiles(settings) if p.id == profile_id), None
    )


def cleanup_may_run(settings: dict) -> bool:
    """Whether a recording can reach AI cleanup with these settings.

    Standard dictation cleans up when cleanup is on; a profile always does,
    and is reached from its shortcut or the Quick Record tab's choice.
    Command Mode and transforms always use the cleanup model, so their
    shortcuts count too.
    """
    if setting_value(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, settings):
        return True
    if settings.get(SettingsKey.QUICK_RECORD_PROFILE):
        return True
    if any(profile.hotkey for profile in load_cleanup_profiles(settings)):
        return True
    hotkeys = settings.get(SettingsKey.HOTKEYS)
    if isinstance(hotkeys, dict) and hotkeys.get("command_mode"):
        return True
    from services.text_transforms import load_transforms

    return any(transform.hotkey for transform in load_transforms(settings))


def normalize_hotkey(hotkey: str) -> str:
    return format_hotkey(*parse_hotkey(hotkey.replace("kp_", "kp ")))


def profile_hotkey_conflict(
    hotkey: str,
    settings: dict,
    *,
    exclude_id: str = "",
    standard_hotkeys: dict | None = None,
) -> str:
    """Return the action, profile or transform already using this shortcut."""
    # hotkey_conflicts imports this module.
    from services.hotkey_conflicts import PROFILE, hotkey_conflict

    return hotkey_conflict(
        hotkey,
        settings,
        exclude=(PROFILE, exclude_id),
        standard_hotkeys=standard_hotkeys,
    )


def validate_profile(profile: CleanupProfile, settings: dict) -> None:
    if not profile.name.strip():
        raise ValueError("Give the profile a name.")
    if len(profile.name.strip()) > 80:
        raise ValueError("Use a name with 80 characters or fewer.")
    if not profile.instructions.strip():
        raise ValueError("Add instructions for the output you want.")
    if any(
        p.id != profile.id and p.name.casefold() == profile.name.strip().casefold()
        for p in load_cleanup_profiles(settings)
    ):
        raise ValueError("A profile with that name already exists.")
    conflict = profile_hotkey_conflict(profile.hotkey, settings, exclude_id=profile.id)
    if conflict:
        raise ValueError(
            f"That shortcut is already used by {conflict}. Choose another."
        )


def save_cleanup_profile(settings: dict, profile: CleanupProfile) -> None:
    """Mutate inside SettingsManager.mutate_settings to preserve concurrent edits."""
    validate_profile(profile, settings)
    profiles = load_cleanup_profiles(settings)
    index = next(
        (i for i, p in enumerate(profiles) if p.id == profile.id), len(profiles)
    )
    profiles[index : index + 1] = [profile]
    settings[SettingsKey.TRANSCRIPT_CLEANUP_PROFILES] = [p.to_dict() for p in profiles]


def delete_cleanup_profile(settings: dict, profile_id: str) -> None:
    settings[SettingsKey.TRANSCRIPT_CLEANUP_PROFILES] = [
        p.to_dict() for p in load_cleanup_profiles(settings) if p.id != profile_id
    ]
    if settings.get(SettingsKey.QUICK_RECORD_PROFILE) == profile_id:
        settings[SettingsKey.QUICK_RECORD_PROFILE] = ""


def compose_profile_prompt(profile: CleanupProfile, rules: list[str]) -> str:
    prompt = (
        "Transform this speech-to-text dictation into the requested output. "
        "Fix punctuation, remove fillers, and preserve meaning, names, and facts. "
        f"{config.TRANSCRIPT_CLEANUP_SPOKEN_CORRECTIONS} "
        "Do not invent missing details. Return only the finished text, without "
        "commentary or surrounding code fences.\n\n"
        f"Output instructions:\n{profile.instructions}"
    )
    if profile.use_learned_rules and rules:
        prompt = compose_transcript_cleanup_prompt(prompt, rules)
        prompt += (
            "\nIf a learned formatting rule conflicts with the output instructions, "
            "follow the output instructions. Keep learned spellings and terminology."
        )
    return prompt
