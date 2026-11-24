"""A word the user puts back after AI cleanup changed it is learned at once and protected (issue #37)."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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


@pytest.mark.parametrize("before_cleanup, pasted, field, expected", [
    (SPOKEN, PASTED, REVERTED, True),
    ("lets meet with ksenia, about the launch", PASTED, "Let's meet with KSENIA! about the launch.", True),
    (
        "lets meet with o'brien about the launch",
        "Let's meet with Brian about the launch.",
        "Let’s meet with O’Brien about the launch.",
        True,
    ),
    (SPOKEN, PASTED, "Dear team, Let's meet with Ksenia about the launch. See you then", True),
    (
        "Invite Sarah and ask Ksenia to sign",
        "Invite Sarah and ask Sarah to sign",
        "Invite Sarah and ask Ksenia to sign",
        True,  # valid reversal even though replaced word also appears elsewhere
    ),
    (
        "Invite Sarah and ask Ksenia to sign",
        "Invite Sarah and ask Sarah to sign.",
        "Hi Sarah, Invite Sarah and ask Ksenia to sign. Thanks!",
        True,  # valid reversal with context outside pasted span and replaced word elsewhere
    ),
    (
        "Invite Mira and ask Sonia to sign",
        "Invite Mira and ask Sarah to sign",
        "Invite Mira and ask Mira to sign",
        False,  # restored word appeared elsewhere, not at the corrected position
    ),
    (
        SPOKEN,
        "Let's meet with Ksenia about the launch.",
        "Let's meet with Sonia about the launch.",
        False,  # the user changed a spoken word
    ),
    ("lets meet with Sonia about the launch", PASTED, REVERTED, False),  # cleanup kept it
    (
        "Invite Mira to sign",
        "Invite Mira and ask Sarah to sign",
        "Invite Mira and ask Mira to sign",
        False,  # cleanup inserted the edited word
    ),
    (
        "Mira asked Sonia to sign the form",
        "Ask Sarah to sign the form",
        "Ask Mira to sign the form",
        False,  # ambiguous multi-word cleanup rewrite covering the edited position
    ),
    (
        "Ask Mira Sonia to sign",
        "Ask Sarah to sign",
        "Ask Mira to sign",
        False,  # ambiguous two-to-one collapse
    ),
    (
        "Ask Mira Sonia to sign",
        "Ask Elena Sarah to sign",
        "Ask Elena Mira to sign",
        False,  # ambiguous two-to-two replacement
    ),
    (
        "Ask Mira to sign and ask Sonia to sign",
        "Ask Sarah to sign",
        "Ask Mira to sign",
        False,  # ambiguous alignment between repeated clauses
    ),
    (
        "Ksenia and something completely different",
        "Sonia and totally unrelated text",
        "Ksenia and totally unrelated text",
        False,  # too few anchors between pre-cleanup and pasted text
    ),
    (None, PASTED, REVERTED, False),  # cleanup changed nothing
    ("", PASTED, REVERTED, False),
])
def test_reverted_cleanup(before_cleanup, pasted, field, expected):
    assert dictionary.reverted_cleanup(before_cleanup, pasted, field) is expected


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

    def test_word_elsewhere_in_transcript_does_not_immediately_protect(self, learning):  # noqa: F811
        learned, _later = learning
        before = "Invite Mira and ask Sonia to sign"
        pasted = "Invite Mira and ask Sarah to sign"
        field = "Invite Mira and ask Mira to sign"
        focus_context.set_service(FakeService(field))

        dictionary.schedule_learning(job(), pasted, before)
        assert saved_terms() == [] and learned == []

        dictionary.schedule_learning(job(), pasted, before)
        (term,) = saved_terms()
        assert (term.term, term.learned, term.new, term.protected) == ("Mira", True, True, False)
        assert learned == ["Mira"]

    def test_valid_reversal_with_replaced_word_elsewhere_protects_at_once(self, learning):  # noqa: F811
        learned, _later = learning
        before = "Invite Sarah and ask Ksenia to sign"
        pasted = "Invite Sarah and ask Sarah to sign."
        field = "Dear team, Invite Sarah and ask Ksenia to sign. Thanks!"
        focus_context.set_service(FakeService(field))

        dictionary.schedule_learning(job(), pasted, before)

        (term,) = saved_terms()
        assert (term.term, term.learned, term.new, term.protected) == ("Ksenia", True, True, True)
        assert learned == ["Ksenia"]

    def test_ambiguous_cleanup_rewrite_falls_back_to_ordinary_learning(self, learning):  # noqa: F811
        learned, _later = learning
        before = "Mira asked Sonia to sign the form"
        pasted = "Ask Sarah to sign the form"
        field = "Ask Mira to sign the form"
        focus_context.set_service(FakeService(field))

        dictionary.schedule_learning(job(), pasted, before)
        assert saved_terms() == [] and learned == []

        dictionary.schedule_learning(job(), pasted, before)
        (term,) = saved_terms()
        assert term.term == "Mira" and term.learned and not term.protected
        assert learned == ["Mira"]


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
    from ui_qt.widgets.dictionary_library import DictionaryRow

    row = DictionaryRow(dictionary.DictionaryTerm("k", "Ksenia", learned=True, protected=True))
    plain = DictionaryRow(dictionary.DictionaryTerm("o", "Olu"))

    assert not row.kept_badge.isHidden()
    assert plain.kept_badge.isHidden()
