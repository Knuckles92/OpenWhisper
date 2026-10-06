"""A dictation into an app the focus capture could not identify gets no paragraph breaks either."""

from concurrent.futures import Future

import pytest

from benchmarks.cleanup_levels import run as bench
from config import config
from services.dictation_pipeline import DictationJob, compose_cleanup_prompt, prepare_text
from services.focus_context import AppIdentity, FocusSnapshot
from services.settings import SettingsKey

LONG = (
    "so the deploy finished this morning and everything looks fine on the dashboards "
    "next topic the database migration still needs a review before friday"
)
NOTEPAD = AppIdentity("notepad.exe", "Notepad", platform="windows")


def _job(identity, *, focus=True):
    if not focus:
        return DictationJob()  # what begin_job builds with "Know which app" off
    future = Future()
    future.set_result(FocusSnapshot(identity))
    return DictationJob(focus=future)


def _prompt(settings, job):
    return compose_cleanup_prompt(
        job=job, settings=settings, profile=None, rules=[],
        prepared=prepare_text(LONG, job, settings),
    )


# Installs from before cleanup levels: no saved prompt, or the old default
# saved by Reset. Both get Medium now.
UPGRADED = [
    {SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True},
    {
        SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True,
        SettingsKey.TRANSCRIPT_CLEANUP_PROMPT: config.LEGACY_DEFAULT_CLEANUP_PROMPTS[0],
    },
    {SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True, SettingsKey.TRANSCRIPT_CLEANUP_LEVEL: "high"},
]


@pytest.mark.parametrize("settings", UPGRADED)
@pytest.mark.parametrize("focus", [True, False])
def test_an_unknown_app_is_told_to_add_no_line_or_paragraph_breaks(settings, focus):
    prompt = _prompt(settings, _job(None, focus=focus))
    note = prompt.rsplit("\n\n", 1)[1]

    assert "unknown" in note
    assert "never add line breaks" in note
    assert "one paragraph" in note
    assert "list inline" in note


@pytest.mark.parametrize("settings", UPGRADED)
def test_a_known_app_keeps_the_presets_paragraphs(settings):
    prompt = _prompt(settings, _job(NOTEPAD))

    assert config.TRANSCRIPT_CLEANUP_PARAGRAPHS in prompt
    assert "never add line breaks" not in prompt


def test_the_golden_set_checks_a_long_dictation_into_an_unknown_app_stays_one_line():
    cases = [
        case for case in bench.load_cases()
        if case.get("destination") == "unknown" and case.get("max_lines") == 1
        and len(case["input"].split()) >= 30
    ]

    assert cases
    for case in cases:
        assert bench.compose_prompt(case).endswith(f"\n\n{config.TRANSCRIPT_CLEANUP_UNKNOWN_APP_LINES}")
