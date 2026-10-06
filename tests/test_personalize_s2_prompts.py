"""Cleanup levels: the presets, custom prompts, and the prompt a dictation gets."""

from concurrent.futures import Future

import pytest

from config import config
from services import cleanup_prompts, synthetic_keys
from services.cleanup_profiles import CleanupProfile, compose_profile_prompt
from services.cleanup_prompts import CleanupLevel
from services.dictation_pipeline import (
    DictationJob,
    JobMode,
    compose_cleanup_prompt,
    prepare_text,
)
from services.focus_context import AppIdentity, FocusSnapshot
from services.settings import (
    SETTING_DEFAULTS,
    SettingsKey,
    resolve_transcript_cleanup_prompt,
)

PRESETS = config.TRANSCRIPT_CLEANUP_LEVEL_PROMPTS
CORRECTIONS = config.TRANSCRIPT_CLEANUP_SPOKEN_CORRECTIONS
LISTS = config.TRANSCRIPT_CLEANUP_SPOKEN_LISTS
PARAGRAPHS = config.TRANSCRIPT_CLEANUP_PARAGRAPHS
TIGHTEN = config.TRANSCRIPT_CLEANUP_TIGHTEN
LIGHT_ONLY = config.TRANSCRIPT_CLEANUP_LIGHT_ONLY
ON = {SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True}


def _on(**values):
    return {**ON, **{getattr(SettingsKey, key): value for key, value in values.items()}}


# --- presets ------------------------------------------------------------------


@pytest.mark.parametrize("level", CleanupLevel.STORED)
def test_every_level_keeps_the_guard_and_the_language(level):
    prompt = PRESETS[level]

    assert prompt.startswith(config.TRANSCRIPT_CLEANUP_INTRO)
    assert config.TRANSCRIPT_CLEANUP_BASICS in prompt
    assert config.TRANSCRIPT_CLEANUP_LANGUAGE in prompt
    assert "never translate" in prompt
    assert prompt.endswith(config.TRANSCRIPT_CLEANUP_GUARD)
    for phrase in ("Do not invent content", "preserve meaning, tone, and proper nouns",
                   "no preamble or quotes"):
        assert phrase in prompt


@pytest.mark.parametrize(
    ("level", "included", "left_out"),
    [
        ("light", [LIGHT_ONLY], [CORRECTIONS, LISTS, PARAGRAPHS, TIGHTEN]),
        ("medium", [CORRECTIONS, LISTS, PARAGRAPHS], [LIGHT_ONLY, TIGHTEN]),
        ("high", [CORRECTIONS, LISTS, PARAGRAPHS, TIGHTEN], [LIGHT_ONLY]),
    ],
)
def test_each_level_adds_only_its_own_sentences(level, included, left_out):
    prompt = PRESETS[level]

    for sentence in included:
        assert sentence in prompt
    for sentence in left_out:
        assert sentence not in prompt


def test_the_corrections_sentence_has_a_positive_and_a_negative_example():
    for cue in ("actually", "no wait", "sorry", "scratch that", "I mean", "make that"):
        assert f'"{cue}"' in CORRECTIONS
    assert '"let\'s meet at 2, actually 3" becomes "Let\'s meet at 3."' in CORRECTIONS
    assert '"I actually like it."' in CORRECTIONS
    for cue in ("first", "one... two", "bullet", "number one"):
        assert cue in LISTS
    assert "one item per line" in LISTS and "keep it as prose" in LISTS


def test_the_presets_are_read_only_and_the_old_default_is_kept_for_comparison():
    with pytest.raises(TypeError):
        PRESETS["medium"] = "Anything."
    assert config.LEGACY_DEFAULT_CLEANUP_PROMPTS == (config.TRANSCRIPT_CLEANUP_PROMPT,)
    assert "speech-to-text" in config.TRANSCRIPT_CLEANUP_PROMPT
    assert config.TRANSCRIPT_CLEANUP_PROMPT not in PRESETS.values()


# --- levels -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "level"),
    [
        ({}, "none"),
        ({SettingsKey.TRANSCRIPT_CLEANUP_LEVEL: "high"}, "none"),
        ({SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: "yes"}, "none"),
        (ON, "medium"),
        (_on(TRANSCRIPT_CLEANUP_LEVEL="light"), "light"),
        (_on(TRANSCRIPT_CLEANUP_LEVEL="high"), "high"),
        (_on(TRANSCRIPT_CLEANUP_LEVEL="none"), "medium"),
        (_on(TRANSCRIPT_CLEANUP_LEVEL="HIGH"), "medium"),
        (_on(TRANSCRIPT_CLEANUP_LEVEL=3), "medium"),
    ],
)
def test_resolve_level_follows_the_switch_and_validates_the_level(settings, level):
    assert cleanup_prompts.resolve_level(settings) == level


@pytest.mark.parametrize(
    ("settings", "label"),
    [
        ({}, "Off"),
        ({SettingsKey.TRANSCRIPT_CLEANUP_PROMPT: "Write like a pirate."}, "Off"),
        (ON, "Medium"),
        (_on(TRANSCRIPT_CLEANUP_LEVEL="light"), "Light"),
        (_on(TRANSCRIPT_CLEANUP_LEVEL="high"), "High"),
        (_on(TRANSCRIPT_CLEANUP_PROMPT="Write like a pirate."), "Custom"),
        (_on(TRANSCRIPT_CLEANUP_PROMPT=config.TRANSCRIPT_CLEANUP_PROMPT), "Medium"),
    ],
)
def test_level_label(settings, label):
    assert cleanup_prompts.level_label(settings) == label


# --- custom prompts -----------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "",
        "  \n ",
        42,
        config.TRANSCRIPT_CLEANUP_PROMPT,
        f"\n  {config.TRANSCRIPT_CLEANUP_PROMPT}  ",
        # Re-wrapped by an editor: still the old default.
        config.TRANSCRIPT_CLEANUP_PROMPT.replace(". ", ".\n"),
        *PRESETS.values(),
        f"  {PRESETS['high']}\n",
    ],
)
def test_built_in_prompts_are_not_custom(stored):
    settings = {} if stored is None else {SettingsKey.TRANSCRIPT_CLEANUP_PROMPT: stored}

    assert cleanup_prompts.custom_prompt(settings) == ""
    for level in CleanupLevel.STORED:
        assert cleanup_prompts.base_prompt(settings, level) == PRESETS[level]


def test_a_custom_prompt_replaces_every_preset():
    settings = _on(TRANSCRIPT_CLEANUP_PROMPT="  Make this a Slack message.  ")

    assert cleanup_prompts.custom_prompt(settings) == "Make this a Slack message."
    for level in (*CleanupLevel.STORED, CleanupLevel.NONE):
        assert cleanup_prompts.base_prompt(settings, level) == "Make this a Slack message."
    assert resolve_transcript_cleanup_prompt(settings) == "Make this a Slack message."


def test_with_cleanup_off_the_base_prompt_is_the_saved_levels_preset():
    settings = {SettingsKey.TRANSCRIPT_CLEANUP_LEVEL: "light"}

    assert cleanup_prompts.base_prompt(settings, CleanupLevel.NONE) == PRESETS["light"]
    assert cleanup_prompts.base_prompt({}, CleanupLevel.NONE) == PRESETS["medium"]
    assert cleanup_prompts.base_prompt({}, "bogus") == PRESETS["medium"]
    assert resolve_transcript_cleanup_prompt(settings) == PRESETS["light"]


def test_the_saved_prompt_default_is_empty_so_no_install_starts_custom():
    assert SETTING_DEFAULTS[SettingsKey.TRANSCRIPT_CLEANUP_PROMPT] == ""
    assert resolve_transcript_cleanup_prompt({}) == PRESETS["medium"]


# --- what a dictation is sent -------------------------------------------------


NOTEPAD = AppIdentity("notepad.exe", "Notepad", pid=7, window="0x7")
TERMINAL = AppIdentity("WindowsTerminal.exe", "Windows Terminal", pid=8, window="0x8")


def _job(identity=NOTEPAD, mode=JobMode.DICTATION):
    focus = Future()
    focus.set_result(FocusSnapshot(identity))
    return DictationJob(mode=mode, focus=focus)


def _prompt(settings, rules=(), profile=None, job=None):
    job = _job() if job is None else job
    prepared = prepare_text("so um first milk second eggs", job, settings)
    return compose_cleanup_prompt(
        job=job, settings=settings, profile=profile, rules=list(rules), prepared=prepared,
    )


@pytest.mark.parametrize("level", CleanupLevel.STORED)
def test_a_dictation_gets_its_levels_preset_and_the_rules(level):
    prompt = _prompt(_on(TRANSCRIPT_CLEANUP_LEVEL=level), rules=["Spell Acme correctly."])

    assert prompt.startswith(PRESETS[level])
    assert "Additional user-taught rules (always apply):\n1. Spell Acme correctly." in prompt
    assert config.TRANSCRIPT_CLEANUP_PROMPT not in prompt


def test_existing_users_with_the_old_default_saved_get_medium():
    prompt = _prompt(_on(TRANSCRIPT_CLEANUP_PROMPT=config.TRANSCRIPT_CLEANUP_PROMPT))

    assert prompt == PRESETS["medium"]


def test_a_custom_prompt_gets_no_corrections_or_lists():
    prompt = _prompt(_on(TRANSCRIPT_CLEANUP_PROMPT="Bullet points only.",
                         TRANSCRIPT_CLEANUP_LEVEL="high"))

    assert prompt == "Bullet points only."


def test_the_profile_preamble_handles_corrections_but_not_lists():
    profile = CleanupProfile("notes", "Notes", "Make bullet notes.", use_learned_rules=False)

    prompt = compose_profile_prompt(profile, ["Spell Acme correctly."])

    assert CORRECTIONS in prompt
    assert LISTS not in prompt
    assert "Do not invent missing details." in prompt
    assert prompt.endswith("Output instructions:\nMake bullet notes.")
    assert _prompt(ON, profile=profile) == prompt
    assert _prompt(ON, profile=profile, job=_job(identity=None)) == prompt


# --- lists where line breaks may not belong -----------------------------------


@pytest.fixture
def terminals(monkeypatch):
    monkeypatch.setattr(synthetic_keys, "is_terminal", lambda identity: identity == TERMINAL)


INLINE = config.TRANSCRIPT_CLEANUP_INLINE_LISTS
TERMINAL_LINES = config.TRANSCRIPT_CLEANUP_TERMINAL_LINES


@pytest.mark.parametrize("level", ["medium", "high"])
@pytest.mark.parametrize(
    ("identity", "note"), [(NOTEPAD, ""), (None, INLINE), (TERMINAL, TERMINAL_LINES)],
)
def test_lists_stay_inline_in_terminals_and_unknown_apps(terminals, level, identity, note):
    # With styles off the list note is the only thing keeping a terminal on
    # one line; with them on, the style block says it instead (below).
    settings = _on(TRANSCRIPT_CLEANUP_LEVEL=level, APP_STYLES_ENABLED=False)
    prompt = _prompt(settings, job=_job(identity))

    expected = PRESETS[level] + (f"\n\n{note}" if note else "")
    assert prompt == expected


def test_a_terminal_is_told_to_stay_on_one_line_only_once(terminals):
    prompt = _prompt(_on(TRANSCRIPT_CLEANUP_LEVEL="medium"), job=_job(TERMINAL))

    assert TERMINAL_LINES not in prompt
    assert prompt.count("terminal") == 1


def test_a_dictation_without_focus_capture_counts_as_an_unknown_app(terminals):
    assert _prompt(ON, job=DictationJob()) == f"{PRESETS['medium']}\n\n{INLINE}"


@pytest.mark.parametrize(
    "settings",
    [
        _on(TRANSCRIPT_CLEANUP_LEVEL="light"),
        _on(TRANSCRIPT_CLEANUP_PROMPT="Bullet points only."),
    ],
)
@pytest.mark.parametrize("identity", [None, TERMINAL])
def test_no_note_when_the_prompt_makes_no_lists(terminals, settings, identity):
    prompt = _prompt(settings, job=_job(identity))

    assert INLINE not in prompt and TERMINAL_LINES not in prompt


def test_files_and_rewrites_keep_their_lists(terminals):
    prepared = prepare_text("first milk second eggs", None, ON)

    upload = compose_cleanup_prompt(job=None, settings=ON, profile=None, rules=[], prepared=prepared)
    command = _prompt(ON, job=_job(None, mode=JobMode.COMMAND))

    assert upload == command == PRESETS["medium"]


def test_the_note_follows_the_rules_and_precedes_the_batch_context(terminals):
    prepared = prepare_text("first milk", _job(None), ON)

    prompt = compose_cleanup_prompt(
        job=_job(None), settings=ON, profile=None, rules=["Spell Acme correctly."],
        prepared=prepared, batch_context="Two halves.",
    )

    rules_at = prompt.index("Additional user-taught rules")
    assert rules_at < prompt.index(INLINE) < prompt.index("Two halves.")
