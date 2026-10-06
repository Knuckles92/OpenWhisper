"""Saved transforms: tolerant loading, validation, shortcuts and the starters."""

import pytest

from config import config
from services import text_transforms
from services.hotkey_conflicts import hotkey_conflict
from services.settings import SettingsKey
from services.text_transforms import (
    STARTER_TRANSFORMS,
    Transform,
    delete_transform,
    find_transform,
    load_transforms,
    save_transform,
)
from services.cleanup_profiles import save_cleanup_profile, CleanupProfile


def _row(transform_id, name, instruction="Do it.", hotkey=""):
    return {"id": transform_id, "name": name, "instruction": instruction, "hotkey": hotkey}


def test_starters_show_until_a_library_is_saved():
    assert load_transforms({}) == list(STARTER_TRANSFORMS)
    assert [t.name for t in STARTER_TRANSFORMS] == [
        "Polish", "Make concise", "Fix grammar", "Prompt engineer",
    ]
    assert load_transforms({SettingsKey.TEXT_TRANSFORMS: []}) == []

    settings = {}
    delete_transform(settings, "polish")
    assert [t.id for t in load_transforms(settings)] == [
        "make-concise", "fix-grammar", "prompt-engineer",
    ]


def test_loading_skips_malformed_rows_and_normalises_shortcuts():
    settings = {SettingsKey.TEXT_TRANSFORMS: [
        "nonsense",
        {"id": "x", "name": "", "instruction": "Do it."},
        {"id": "y", "name": "No instruction", "instruction": "  "},
        {"id": 3, "name": "Bad id", "instruction": "Do it."},
        _row(" tidy ", " Tidy ", " Tidy it up. ", "Alt+Ctrl+T"),
        _row("tidy", "Duplicate id"),
        _row("loose", "Loose", hotkey=42),
    ]}
    assert load_transforms(settings) == [
        Transform("tidy", "Tidy", "Tidy it up.", "ctrl+alt+t"),
        Transform("loose", "Loose", "Do it.", ""),
    ]
    assert load_transforms({SettingsKey.TEXT_TRANSFORMS: "oops"}) == []
    assert find_transform(settings, "tidy").name == "Tidy"
    assert find_transform(settings, "missing") is None


def test_loading_stops_at_the_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_TEXT_TRANSFORMS", 3)
    rows = [_row(str(i), f"T{i}") for i in range(5)]
    assert [t.id for t in load_transforms({SettingsKey.TEXT_TRANSFORMS: rows})] == ["0", "1", "2"]


def test_save_adds_replaces_and_keeps_order():
    settings = {}
    save_transform(settings, Transform("mine", " Mine ", " Shorter. ", "ctrl+alt+j"))
    assert [t.id for t in load_transforms(settings)][-1] == "mine"
    assert settings[SettingsKey.TEXT_TRANSFORMS][-1] == _row("mine", "Mine", "Shorter.", "ctrl+alt+j")

    save_transform(settings, Transform("polish", "Polish more", "Polish harder."))
    names = [t.name for t in load_transforms(settings)]
    assert names == ["Polish more", "Make concise", "Fix grammar", "Prompt engineer", "Mine"]


@pytest.mark.parametrize("transform, message", [
    (Transform("a", "  ", "Do it."), "Give the transform a name."),
    (Transform("a", "x" * 81, "Do it."), "80 characters or fewer"),
    (Transform("a", "New", "   "), "Add an instruction"),
    (Transform("a", "polish", "Do it."), "already exists"),
    (Transform("", "New", "Do it."), "no id"),
])
def test_invalid_transforms_are_refused_without_changes(transform, message):
    settings = {}
    with pytest.raises(ValueError, match=message):
        save_transform(settings, transform)
    assert settings == {}


def test_the_cap_refuses_only_new_transforms(monkeypatch):
    monkeypatch.setattr(config, "MAX_TEXT_TRANSFORMS", 4)
    settings = {}
    with pytest.raises(ValueError, match="up to 4 transforms"):
        save_transform(settings, Transform("fifth", "Fifth", "Do it."))
    save_transform(settings, Transform("polish", "Polish", "Edited."))
    assert find_transform(settings, "polish").instruction == "Edited."


def test_shortcuts_conflict_with_actions_profiles_and_other_transforms():
    settings = {SettingsKey.HOTKEYS: {"record_toggle": "ctrl+alt+r", "command_mode": "ctrl+alt+k"}}
    save_cleanup_profile(settings, CleanupProfile("ticket", "Ticket", "Format.", "ctrl+alt+t"))
    save_transform(settings, Transform("tidy", "Tidy", "Tidy.", "ctrl+alt+y"))

    for hotkey, owner in (
        ("Alt+Ctrl+R", "record toggle"),
        ("ctrl+alt+k", "command mode"),
        ("ctrl+alt+t", "Ticket"),
        ("alt+ctrl+y", "Tidy"),
    ):
        with pytest.raises(ValueError, match=f"already used by {owner}"):
            save_transform(settings, Transform("new", "New", "Do it.", hotkey))

    # Its own shortcut never conflicts with itself.
    save_transform(settings, Transform("tidy", "Tidy", "Tidier.", "ctrl+alt+y"))
    assert hotkey_conflict("ctrl+alt+y", settings) == "Tidy"
    assert text_transforms.transform_hotkey_conflict(
        "ctrl+alt+y", settings, exclude_id="tidy") == ""


def test_delete_removes_only_that_transform():
    settings = {SettingsKey.TEXT_TRANSFORMS: [_row("a", "A"), _row("b", "B")]}
    delete_transform(settings, "a")
    delete_transform(settings, "missing")
    assert settings[SettingsKey.TEXT_TRANSFORMS] == [_row("b", "B")]


def test_mutators_work_through_the_settings_manager(tmp_path):
    from services.settings import SettingsManager

    store = SettingsManager(str(tmp_path / "settings.json"))
    store.mutate_settings(lambda s: save_transform(s, Transform("mine", "Mine", "Do it.")))
    with pytest.raises(ValueError):
        store.mutate_settings(lambda s: save_transform(s, Transform("other", "mine", "Clash.")))
    saved = load_transforms(store.load_all_settings())
    assert [t.id for t in saved] == [t.id for t in STARTER_TRANSFORMS] + ["mine"]
