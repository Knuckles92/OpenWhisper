"""Stats count the words dictated, not the text a snippet expanded to."""

import importlib

import pytest

from services.dictation_pipeline import DictationJob
from services.settings import SettingsKey
from services.snippets import Snippet
from tests.test_personalize_foundation_runtime import h  # noqa: F401

INTRO_TEXT = " ".join(f"word{i}" for i in range(120))
INTRO = Snippet("intro", "my intro", INTRO_TEXT)


def _stats():
    return importlib.import_module("services.dictation_stats")


def _spoken_words():
    from services.database import db
    from services.models import DictationStat

    with db.get_session() as session:
        return [row.spoken_words for row in session.query(DictationStat).filter(
            DictationStat.entry_kind != "marker")]


@pytest.fixture
def dictation(h, monkeypatch):  # noqa: F811 (pytest fixture)
    h.settings.values[SettingsKey.DICTATION_SNIPPETS] = [INTRO.to_dict()]
    h.controller._pending_audio_path = "recording.wav"
    cleaner = h.runtime._transcript_cleanup
    monkeypatch.setattr(cleaner, "configure", lambda *args: None)
    monkeypatch.setattr(cleaner, "is_available", lambda: True)
    monkeypatch.setattr(cleaner, "cleanup",
                        lambda text, system_prompt=None: text[:1].upper() + text[1:] + ".")
    cleaner.provider, cleaner.model = "openai", "gpt"

    def dictate(raw, seconds, *, cleanup):
        h.settings.values[SettingsKey.TRANSCRIPT_CLEANUP_ENABLED] = cleanup
        h.controller._pending_audio_duration = seconds
        assert h.runtime._claim_job(DictationJob())
        h.runtime.on_transcription_complete(*h.runtime._maybe_cleanup_transcript(raw))
        return h.history.entries[-1]

    return dictate


@pytest.mark.parametrize("cleanup", [True, False])
def test_a_whole_snippet_counts_its_trigger(dictation, cleanup):
    entry = dictation("My intro.", 1.2, cleanup=cleanup)

    assert entry["text"] == INTRO_TEXT
    assert _spoken_words() == [2]
    assert _stats().load_summary().average_wpm == 100.0


@pytest.mark.parametrize("cleanup", [True, False])
def test_an_inline_snippet_counts_the_words_around_its_trigger(dictation, cleanup):
    entry = dictation("here is my intro thanks", 2.0, cleanup=cleanup)

    assert INTRO_TEXT in entry["text"]
    assert _spoken_words() == [5]
    assert _stats().load_summary().average_wpm == 150.0


def test_raw_text_for_undo_still_holds_the_expansion(dictation):
    entry = dictation("here is my intro thanks", 2.0, cleanup=True)

    assert entry["raw_text"] == f"here is {INTRO_TEXT} thanks"


def test_a_dictation_without_snippets_counts_as_before(dictation):
    dictation("hello there friend", 1.5, cleanup=True)

    assert _spoken_words() == [3]


def test_spoken_text_wins_over_the_saved_text():
    stat = _stats()._stat({
        "entry_kind": "dictation", "text": INTRO_TEXT, "spoken_text": "my intro",
        "audio_duration": 1.2,
    })

    assert (stat.spoken_words, stat.final_words) == (2, 120)
