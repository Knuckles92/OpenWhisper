"""A word the user puts back after AI cleanup changed it is learned at once and protected (issue #37)."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from services import dictionary, focus_context  # noqa: E402
from services.settings import SettingsKey, settings_manager  # noqa: E402
from tests.test_personalize_s4_learning import (  # noqa: E402
    FakeService,
    job,
    learning,  # noqa: F401  (fixture)
    saved_terms,
)

#: What was said, the AI cleanup's version that was pasted, and the field
#: after the user changed "Sonia" back.
SPOKEN = "lets meet with Ksenia about the launch"
PASTED = "Let's meet with Sonia about the launch."
REVERTED = "Let's meet with Ksenia about the launch."


@pytest.mark.parametrize("before_cleanup, replaced, restored, expected", [
    (SPOKEN, "Sonia", "Ksenia", True),
    ("lets meet with ksenia about the launch", "Sonia", "KSENIA", True),
    (SPOKEN, "Ksenia", "Sonia", False),  # the user changed a spoken word
    ("lets meet with Sonia about the launch", "Sonia", "Ksenia", False),  # cleanup kept it
    (None, "Sonia", "Ksenia", False),  # cleanup changed nothing
    ("", "Sonia", "Ksenia", False),
])
def test_reverted_cleanup(before_cleanup, replaced, restored, expected):
    assert dictionary.reverted_cleanup(before_cleanup, replaced, restored) is expected


class TestLearning:
    def test_one_revert_learns_a_protected_word(self, learning):  # noqa: F811 (pytest fixture)
        learned, later = learning
        focus_context.set_service(FakeService(REVERTED))

        dictionary.schedule_learning(job(), PASTED, SPOKEN)

        (term,) = saved_terms()
        assert (term.term, term.learned, term.new, term.protected) == ("Ksenia", True, True, True)
        assert learned == ["Ksenia"]
        assert later == [dictionary.LEARN_DELAY_S, 0]

    def test_an_ordinary_correction_still_needs_two_sightings(self, learning):  # noqa: F811 (pytest fixture)
        learned, _later = learning
        focus_context.set_service(FakeService(REVERTED))

        dictionary.schedule_learning(job(), PASTED, "lets meet with Sonia about the launch")
        assert saved_terms() == []
        dictionary.schedule_learning(job(), PASTED)

        (term,) = saved_terms()
        assert term.protected is False
        assert learned == ["Ksenia"]

    def test_a_known_word_becomes_protected_once(self, learning):  # noqa: F811 (pytest fixture)
        learned, _later = learning
        settings_manager.mutate_settings(lambda settings: dictionary.add_term(settings, "Ksenia"))
        focus_context.set_service(FakeService(REVERTED))

        dictionary.schedule_learning(job(), PASTED, SPOKEN)
        dictionary.schedule_learning(job(), PASTED, SPOKEN)

        (term,) = saved_terms()
        assert term.protected and not term.learned  # still the user's own word
        assert learned == ["Ksenia"]

    def test_off_when_learning_is_off(self, learning):  # noqa: F811 (pytest fixture)
        learned, _later = learning
        settings_manager.update_settings({SettingsKey.DICTIONARY_LEARN_ENABLED: False})
        focus_context.set_service(FakeService(REVERTED))

        dictionary.schedule_learning(job(), PASTED, SPOKEN)

        assert learned == [] and saved_terms() == []


def test_protected_words_round_trip_and_reach_the_cleanup_prompt():
    settings = {}
    dictionary.add_term(settings, "Kubernetes")
    kept = dictionary.add_learned(settings, "Ksenia", protected=True)
    assert kept.protected
    assert settings[SettingsKey.DICTATION_DICTIONARY][0]["protected"] is True
    assert "protected" not in settings[SettingsKey.DICTATION_DICTIONARY][1]

    terms = dictionary.load_dictionary(settings)
    assert [(term.term, term.protected) for term in terms] == [
        ("Ksenia", True), ("Kubernetes", False),
    ]
    block = dictionary.prompt_block(terms)
    assert "spell it exactly: Ksenia, Kubernetes." in block
    assert block.endswith("never replace or respell them: Ksenia.")
    assert dictionary.find_term(settings, "ksenia") == kept


def test_no_protected_words_leaves_the_prompt_unchanged():
    settings = {}
    dictionary.add_term(settings, "Kubernetes")
    block = dictionary.prompt_block(dictionary.load_dictionary(settings))
    assert "never replace" not in block


def test_a_protected_word_shows_its_badge():
    QApplication.instance() or QApplication([])
    from ui_qt.widgets.dictionary_library import DictionaryRow

    row = DictionaryRow(dictionary.DictionaryTerm("k", "Ksenia", learned=True, protected=True))
    plain = DictionaryRow(dictionary.DictionaryTerm("o", "Olu"))

    assert not row.kept_badge.isHidden()
    assert plain.kept_badge.isHidden()
