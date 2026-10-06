"""One shortcut space shared by standard actions, cleanup profiles and transforms."""

import pytest

from config import config
from services import hotkey_conflicts, text_transforms
from services.cleanup_profiles import profile_hotkey_conflict
from services.hotkey_conflicts import PROFILE, STANDARD, TRANSFORM, hotkey_conflict
from services.settings import SettingsKey
from services.text_transforms import Transform

PROFILES = [
    {"id": "notes", "name": "Meeting notes", "instructions": "Bullets.", "hotkey": "ctrl+shift+f9"},
    {"id": "ticket", "name": "Ticket", "instructions": "A ticket.", "hotkey": ""},
]
TRANSFORMS = [
    Transform("polish", "Polish", "Polish it.", hotkey="ctrl+alt+p"),
    Transform("short", "Make concise", "Shorter."),
]


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setattr(text_transforms, "load_transforms", lambda _settings: list(TRANSFORMS))
    return {SettingsKey.TRANSCRIPT_CLEANUP_PROFILES: [dict(p) for p in PROFILES]}


def test_new_actions_have_no_default_shortcut():
    for action in ("command_mode", "scratchpad_toggle", "cycle_language", "paste_last_original"):
        assert config.DEFAULT_HOTKEYS[action] == ""


def test_free_and_empty_shortcuts_conflict_with_nothing(settings):
    assert hotkey_conflict("", settings) == ""
    assert hotkey_conflict("ctrl+shift+f12", settings) == ""


def test_standard_actions_conflict_by_their_saved_or_default_shortcut(settings):
    record = config.DEFAULT_HOTKEYS["record_toggle"]
    assert hotkey_conflict(record, settings) == "record toggle"

    settings[SettingsKey.HOTKEYS] = {"command_mode": "ctrl+alt+c"}
    # Modifier order does not matter.
    assert hotkey_conflict("alt+ctrl+c", settings) == "command mode"
    # Unsaved edits replace the saved shortcuts.
    assert hotkey_conflict("ctrl+alt+c", settings, standard_hotkeys={}) == ""
    assert hotkey_conflict(
        "ctrl+alt+s", settings, standard_hotkeys={"scratchpad_toggle": "ctrl+alt+s"}
    ) == "scratchpad toggle"


def test_profiles_and_transforms_conflict_by_name(settings):
    assert hotkey_conflict("ctrl+shift+f9", settings) == "Meeting notes"
    assert hotkey_conflict("ctrl+alt+p", settings) == "Polish"


@pytest.mark.parametrize(
    ("hotkey", "exclude"),
    [
        ("ctrl+shift+f9", (PROFILE, "notes")),
        ("ctrl+alt+p", (TRANSFORM, "polish")),
    ],
)
def test_the_binding_being_edited_does_not_conflict_with_itself(settings, hotkey, exclude):
    assert hotkey_conflict(hotkey, settings, exclude=exclude) == ""


def test_an_excluded_standard_action_is_free_to_keep_its_shortcut(settings):
    record = config.DEFAULT_HOTKEYS["record_toggle"]
    assert hotkey_conflict(record, settings, exclude=(STANDARD, "record_toggle")) == ""
    # Excluding one kind never hides another kind with the same id.
    assert hotkey_conflict(record, settings, exclude=(PROFILE, "record_toggle")) == "record toggle"


def test_profile_wrapper_matches_the_shared_check(settings):
    for hotkey in ("ctrl+shift+f9", "ctrl+alt+p", config.DEFAULT_HOTKEYS["cancel"], "f13"):
        assert profile_hotkey_conflict(hotkey, settings) == hotkey_conflict(hotkey, settings)
    assert profile_hotkey_conflict("ctrl+shift+f9", settings, exclude_id="notes") == ""
    assert profile_hotkey_conflict(
        "ctrl+alt+c", settings, standard_hotkeys={"command_mode": "ctrl+alt+c"}
    ) == "command mode"


def test_transforms_are_read_through_their_module(monkeypatch):
    seen = []
    monkeypatch.setattr(
        text_transforms, "load_transforms", lambda settings: seen.append(settings) or []
    )
    settings = {SettingsKey.TRANSCRIPT_CLEANUP_PROFILES: []}

    assert hotkey_conflicts.hotkey_conflict("ctrl+alt+q", settings) == ""
    assert seen == [settings]
