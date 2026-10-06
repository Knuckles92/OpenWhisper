"""Personalization settings: keys, defaults, resolvers and new-install seeding."""

import json
import os

import pytest

from config import config
from services import app_styles, cleanup_prompts, focus_context
from services.app_styles import NEW_INSTALL_TONES, AppCategory, Tone
from services.cleanup_prompts import CleanupLevel
from services.settings import (
    SETTING_DEFAULTS,
    SettingsKey,
    SettingsManager,
    resolve_app_context_enabled,
    resolve_app_context_read_text,
    resolve_recording_hands_free_latch,
    resolve_transcript_cleanup_level,
    seed_new_install_settings,
)

DESIGN_DEFAULTS = {
    "app_context_enabled": True,
    "app_context_read_text": False,
    "app_context_excluded_apps": [],
    "app_styles_enabled": True,
    "app_style_overrides": [],
    "transcript_cleanup_level": "medium",
    "command_mode_insert_without_selection": True,
    "dictation_dictionary": [],
    "dictionary_learn_enabled": True,
    "dictionary_steer_recognition": True,
    "dictation_snippets": [],
    "snippets_enabled": True,
    "recording_hands_free_latch": True,
    "audio_input_priority": [],
    "dictation_languages": [],
    "dictation_active_language": "",
    "scratchpad_always_on_top": True,
    "flow_features_intro_seen": False,
}


def test_every_design_key_has_its_default():
    keys = {value for name, value in vars(SettingsKey).items() if name.isupper()}
    for key, default in DESIGN_DEFAULTS.items():
        assert key in keys
        assert SETTING_DEFAULTS[key] == default
    # Absence means something for these two, so they have no default.
    for key in ("app_style_tones", "text_transforms"):
        assert key in keys and key not in SETTING_DEFAULTS


def test_resolvers_validate_stored_values():
    assert resolve_transcript_cleanup_level({}) == "medium"
    assert resolve_transcript_cleanup_level({"transcript_cleanup_level": "high"}) == "high"
    assert resolve_transcript_cleanup_level({"transcript_cleanup_level": "none"}) == "medium"
    assert resolve_app_context_enabled({}) is True
    assert resolve_app_context_read_text({"app_context_read_text": "yes"}) is False
    assert resolve_recording_hands_free_latch({"recording_hands_free_latch": False}) is False


def test_limits_and_scratchpad_path():
    assert (config.MAX_DICTIONARY_TERMS, config.MAX_RECOGNITION_PHRASES) == (500, 50)
    assert (config.MAX_SNIPPETS, config.MAX_TEXT_TRANSFORMS) == (200, 50)
    assert (config.CONTEXT_BEFORE_CHARS, config.CONTEXT_AFTER_CHARS) == (1000, 200)
    assert config.CONTEXT_CAPTURE_DEADLINE_S == 0.4
    assert config.COMMAND_SELECTION_TIMEOUT_MS == 700
    assert config.RECORD_LATCH_WINDOW_MS == 400
    assert os.path.basename(config.SCRATCHPAD_FILE) == "scratchpad.txt"


def test_tests_never_touch_the_real_focus_or_data_root(tmp_path):
    assert isinstance(focus_context.get_service(), focus_context.NullCaptureService)
    assert os.path.dirname(config.SCRATCHPAD_FILE) == str(tmp_path)


# --- styles and levels ------------------------------------------------------


def test_tones_are_all_formal_until_saved_and_validated():
    assert app_styles.resolve_tones({}) == dict.fromkeys(AppCategory.ALL, Tone.FORMAL)
    assert app_styles.resolve_tones({"app_style_tones": NEW_INSTALL_TONES}) == NEW_INSTALL_TONES
    assert app_styles.resolve_tones({"app_style_tones": {"work": "shouty", "email": "casual"}}) == {
        "email": "casual", "work": "formal", "personal": "formal", "other": "formal",
    }
    assert NEW_INSTALL_TONES == {
        "email": "formal", "work": "casual", "personal": "casual", "other": "formal",
    }


def test_level_is_none_while_cleanup_is_off():
    assert cleanup_prompts.resolve_level({}) == CleanupLevel.NONE
    assert cleanup_prompts.resolve_level({"transcript_cleanup_enabled": True}) == "medium"
    assert cleanup_prompts.resolve_level({
        "transcript_cleanup_enabled": True, "transcript_cleanup_level": "light",
    }) == "light"


@pytest.mark.parametrize(
    ("stored", "custom"),
    [
        (None, ""),
        ("   ", ""),
        (config.TRANSCRIPT_CLEANUP_PROMPT, ""),
        (f"  {config.TRANSCRIPT_CLEANUP_PROMPT}\n", ""),
        ("  Keep my British spelling. ", "Keep my British spelling."),
    ],
)
def test_only_a_real_custom_prompt_counts(stored, custom):
    settings = {} if stored is None else {"transcript_cleanup_prompt": stored}
    assert cleanup_prompts.custom_prompt(settings) == custom
    expected = custom or config.TRANSCRIPT_CLEANUP_LEVEL_PROMPTS["medium"]
    assert cleanup_prompts.base_prompt(settings, "medium") == expected


# --- new-install seeding ----------------------------------------------------


def test_a_brand_new_install_is_seeded_once(tmp_path):
    manager = SettingsManager(str(tmp_path / "openwhisper_settings.json"))
    assert manager.is_new_install()

    assert seed_new_install_settings(manager)
    assert not seed_new_install_settings(manager)

    with open(manager.settings_file, encoding="utf-8") as handle:
        assert json.load(handle)["app_style_tones"] == NEW_INSTALL_TONES
    # A tone the user changes afterwards is never overwritten.
    manager.save_setting("app_style_tones", {"work": "very_casual"})
    assert not seed_new_install_settings(manager)
    assert manager.get("app_style_tones") == {"work": "very_casual"}


def test_an_existing_install_keeps_todays_output(tmp_path):
    path = tmp_path / "openwhisper_settings.json"
    path.write_text(json.dumps({"ui_theme": "dark"}), encoding="utf-8")
    manager = SettingsManager(str(path))

    assert not manager.is_new_install()
    assert not seed_new_install_settings(manager)
    assert "app_style_tones" not in manager.load_all_settings()


def test_a_manager_pointed_elsewhere_is_never_new(tmp_path):
    manager = SettingsManager(str(tmp_path / "first.json"))
    manager.settings_file = str(tmp_path / "second.json")

    assert not manager.is_new_install()
    assert not seed_new_install_settings(manager)
    assert not os.path.exists(manager.settings_file)


def test_seeding_tolerates_stand_in_managers():
    class Bare:
        pass

    assert not seed_new_install_settings(Bare())
