"""Snippets through the dictation pipeline and the runtime's cleanup stage."""

from types import SimpleNamespace

import pytest

from services.dictation_pipeline import (
    DictationJob,
    FinishedText,
    JobMode,
    compose_cleanup_prompt,
    finish_text,
    prepare_text,
)
from services.runtime import transcription
from services.runtime.transcription import TranscriptionRuntime
from services.settings import SettingsKey
from services.snippets import Snippet
from tests.fakes.settings import InMemorySettings

CAL = Snippet("cal", "my calendar link", "https://cal.example.com/dana?ref=email")
SIGN_OFF = Snippet("sig", "my sign off", "**Dana Lee**\n- Product designer", formatted=True)


def _settings(**extra):
    return {
        SettingsKey.DICTATION_SNIPPETS: [CAL.to_dict(), SIGN_OFF.to_dict()],
        SettingsKey.APP_CONTEXT_ENABLED: False,
        **extra,
    }


def _capitalize_and_punctuate(text: str) -> str:
    return text[:1].upper() + text[1:].rstrip(".") + "."


def test_a_whole_snippet_skips_cleanup_and_pastes_its_exact_text():
    prepared = prepare_text("My calendar link.", DictationJob(), _settings())

    assert prepared.skip_cleanup
    assert finish_text(prepared.cleanup_input, prepared) == FinishedText(
        CAL.text, "", True, CAL.text
    )


def test_an_embedded_snippet_survives_cleanup():
    prepared = prepare_text("here's my calendar link for friday", DictationJob(), _settings())

    assert not prepared.skip_cleanup
    assert prepared.cleanup_input == "here's [[S1]] for friday"
    cleaned = _capitalize_and_punctuate(prepared.cleanup_input)
    assert finish_text(cleaned, prepared) == FinishedText(
        f"Here's {CAL.text} for friday.", "", True, f"here's {CAL.text} for friday",
    )


def test_a_lost_placeholder_falls_back_to_the_uncleaned_text_with_expansions():
    prepared = prepare_text("thanks again my sign off", DictationJob(), _settings())

    finished = finish_text("Thanks again.", prepared)

    assert finished == FinishedText(
        "thanks again Dana Lee\n- Product designer",
        "thanks again <b>Dana Lee</b><ul><li>Product designer</li></ul>",
        False,
        "thanks again Dana Lee\n- Product designer",
    )


@pytest.mark.parametrize("job,settings", [
    (DictationJob(), _settings(**{SettingsKey.SNIPPETS_ENABLED: False})),
    (DictationJob(mode=JobMode.COMMAND), _settings()),
    (DictationJob(mode=JobMode.TRANSFORM), _settings()),
    (None, _settings()),
])
def test_snippets_only_run_for_live_dictation_with_snippets_on(job, settings):
    prepared = prepare_text("my calendar link", job, settings)

    assert not prepared.skip_cleanup
    assert prepared.cleanup_input == "my calendar link"
    assert finish_text("My calendar link.", prepared).text == "My calendar link."


def test_the_cleanup_prompt_carries_the_placeholder_guard():
    settings = _settings()
    prepared = prepare_text("send my calendar link", DictationJob(), settings)

    prompt = compose_cleanup_prompt(
        job=DictationJob(), settings=settings, profile=None, rules=[], prepared=prepared,
    )

    assert prompt.endswith(
        "The transcript contains the placeholder [[S1]], which stands for text "
        "inserted later; keep it exactly once, unchanged and in its place."
    )


# --- the runtime's cleanup stage ----------------------------------------------


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


@pytest.fixture
def runtime(monkeypatch):
    settings = InMemorySettings(_settings(**{SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True}))
    monkeypatch.setattr(transcription, "settings_manager", settings)
    controller = SimpleNamespace(overlay_state_update=_Signal(), status_update=_Signal())
    runtime = TranscriptionRuntime(controller)
    cleaner = runtime._transcript_cleanup
    calls = []

    def cleanup(text, system_prompt=None):
        calls.append((text, system_prompt))
        return runtime.cleaner_result(text)

    runtime.cleaner_result = _capitalize_and_punctuate
    runtime.cleanup_calls = calls
    monkeypatch.setattr(cleaner, "configure", lambda *args: None)
    monkeypatch.setattr(cleaner, "is_available", lambda: True)
    monkeypatch.setattr(cleaner, "cleanup", cleanup)
    cleaner.provider, cleaner.model = "openai", "gpt"
    assert runtime._claim_job(DictationJob())
    return runtime


def test_the_runtime_never_sends_a_whole_snippet_to_cleanup(runtime):
    assert runtime._maybe_cleanup_transcript("My sign off.") == (
        "Dana Lee\n- Product designer", None, None,
    )
    assert runtime.cleanup_calls == []
    assert runtime._delivery_html == "<b>Dana Lee</b><ul><li>Product designer</li></ul>"


def test_the_runtime_cleans_around_a_protected_snippet(runtime):
    text, raw_text, info = runtime._maybe_cleanup_transcript("book with my calendar link")

    (sent, prompt), = runtime.cleanup_calls
    assert sent == "book with [[S1]]"
    assert "[[S1]]" in prompt
    assert CAL.text not in sent + prompt
    assert (text, raw_text) == (f"Book with {CAL.text}.", f"book with {CAL.text}")
    assert info is not None
    assert runtime._delivery_html == ""


def test_the_runtime_reports_a_cleanup_that_dropped_a_snippet(runtime):
    runtime.cleaner_result = lambda text: "Book with it."

    assert runtime._maybe_cleanup_transcript("book with my calendar link") == (
        f"book with {CAL.text}", None, None,
    )
    assert runtime._last_cleanup_failure == "snippet placeholder lost"
