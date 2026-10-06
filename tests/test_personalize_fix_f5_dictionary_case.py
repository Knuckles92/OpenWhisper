"""Dictionary and term replacements match letters whose case forms differ in length."""

import pytest

from meeting.corrections import correct_text, term_rules_from_items
from services import dictation_pipeline, dictionary
from services.settings import SettingsKey
from services.vocabulary import replace_terms


def _terms(*heard):
    return dictionary.load_dictionary({SettingsKey.DICTATION_DICTIONARY: [
        {"id": "t", "term": "Izmir HQ", "starred": False, "heard": list(heard)},
    ]})


@pytest.mark.parametrize("heard", ["izmir", "İzmir", "IZMIR"])
@pytest.mark.parametrize("spoken", ["İzmir office", "izmir office", "IZMİR office"])
def test_a_dotted_capital_i_matches_its_variant(heard, spoken):
    assert dictionary.apply_replacements(spoken, _terms(heard)) == "Izmir HQ office"


def test_the_dictation_pipeline_replaces_a_dotted_capital_i():
    settings = {SettingsKey.DICTATION_DICTIONARY: [
        {"id": "t", "term": "Istanbul Ofis", "heard": ["İstanbul"]},
    ]}
    assert dictation_pipeline.prepare_text("İstanbul ofis", None, settings).text == (
        "Istanbul Ofis ofis")


@pytest.mark.parametrize("text,rules,replaced", [
    ("İzmir office", {"izmir": "Izmir HQ"}, "Izmir HQ office"),
    ("ſoft launch", {"soft": "Soft"}, "Soft launch"),
    ("die Straße hier", {"strasse": "Strasse"}, "die Strasse hier"),
    # Text after a letter that folds to two keeps its place.
    ("Straße und koln.", {"koln": "Köln"}, "Straße und Köln."),
    ("İİ İzmir", {"izmir": "X"}, "İİ X"),
    # A lowercased dotted capital (i + combining dot) is still the word.
    ("i̇zmir", {"izmir": "Izmir"}, "Izmir"),
    ("Izmirli", {"izmir": "X"}, "Izmirli"),
    ("x", {"": "never"}, "x"),
])
def test_replace_terms_folds_case_like_unicode(text, rules, replaced):
    assert replace_terms(text, rules) == replaced


def test_meeting_rules_with_lowered_keys_still_match():
    rules = term_rules_from_items([{
        "author_type": "user",
        "data": {"kind": "term_correction", "selected_text": "İzmir", "replacement": "Izmir"},
    }])
    assert correct_text("we met in İzmir", rules) == "we met in Izmir"
