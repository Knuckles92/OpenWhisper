"""Term replacement folds case for the whole text at once, matching the per-character fold."""

import random
import sys

import pytest

from meeting.corrections import correct_text, term_rules_from_items
from services import dictation_pipeline, dictionary, vocabulary
from services.settings import SettingsKey
from services.vocabulary import replace_terms

_DOT = "\u0307"


def _reference_fold_with_origin(text):
    # Folds one character at a time: plainly right, too slow for Meeting Mode.
    def fold(piece):
        return piece.casefold().replace("i" + _DOT, "i")

    pieces, origin, previous = [], [], ""
    for index, char in enumerate(text):
        piece = "" if char == _DOT and previous == "i" else fold(char)
        pieces.append(piece)
        origin.extend([index] * len(piece))
        previous = piece[-1:] or previous
    return "".join(pieces), origin


def test_no_character_folds_to_nothing_and_only_the_dotted_capital_i_folds_to_a_dot():
    # The whole-text fold relies on both facts about the running Python's Unicode tables.
    empty, dotted = [], []
    for code in range(sys.maxunicode + 1):
        folded = chr(code).casefold()
        if not folded:
            empty.append(code)
        if _DOT in folded and code != ord(_DOT):
            dotted.append(code)
    assert empty == []
    assert dotted == [0x130]


_ALPHABET = list("abcxyz iIİıßẞﬁﬃΣσςÉéŉǰ’—.,Kk\u212a") + [_DOT, _DOT, "İ"]


@pytest.mark.parametrize("text", [
    "",
    "plain ascii, with digits 123!",
    "".join(chr(code) for code in range(128)),
    "café — we’ll ship",
    "die Straße hier",
    "İİ İzmir",
    "i" + _DOT + "zmir",
    "I" + _DOT + _DOT + "x",
    "ﬁ" + _DOT + " a" + _DOT + _DOT,
    _DOT + "i",
    "ΑΣ ΣΑΣ σς",
])
def test_the_fold_matches_the_per_character_fold(text):
    folded, origin = vocabulary._fold_with_origin(text)
    expected, expected_origin = _reference_fold_with_origin(text)
    assert folded == expected
    assert list(origin) == expected_origin


def test_the_fold_matches_the_per_character_fold_on_random_text():
    rng = random.Random(7)
    for _ in range(20000):
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 14)))
        folded, origin = vocabulary._fold_with_origin(text)
        assert (folded, list(origin)) == _reference_fold_with_origin(text), repr(text)


def _python_calls(text, rules):
    calls = 0

    def count(frame, event, arg):
        nonlocal calls
        if event in ("call", "c_call"):
            calls += 1

    sys.setprofile(count)
    try:
        result = replace_terms(text, rules)
    finally:
        sys.setprofile(None)
    return calls, result


@pytest.mark.parametrize("filler", [
    "the quick brown fox ",
    "we’ll ship the café — ",
    "die Straße ist groß ",
    "İyi günler İZMİR ",
])
def test_long_text_costs_no_more_python_calls_than_short_text(filler):
    # Meeting Mode corrects every segment of every transcript read, so the
    # fold must not do Python-level work per character.
    rules = {"kubernetes": "Kubernetes", "jira": "Jira"}
    short = filler + "ask jira about kubernetes."
    long = filler * 200 + "ask jira about kubernetes."
    replace_terms(short, rules)  # compiles and caches the pattern
    short_calls, short_result = _python_calls(short, rules)
    long_calls, long_result = _python_calls(long, rules)
    assert short_result.endswith("ask Jira about Kubernetes.")
    assert long_result.endswith("ask Jira about Kubernetes.")
    assert long_calls == short_calls


# Dictionary and term replacements match letters whose case forms differ in length.


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
    ("we’ll ship kubernetes — café", {"kubernetes": "Kubernetes"}, "we’ll ship Kubernetes — café"),
    ("an i" + _DOT + _DOT + "zmir trip", {"izmir": "Izmir"}, "an Izmir trip"),
    ("x ﬁ" + _DOT + "x", {"fix": "FIX"}, "x FIX"),
    ("İZMİR", {"i" + _DOT + "zmir": "Izmir"}, "Izmir"),
])
def test_replace_terms_folds_case_like_unicode(text, rules, replaced):
    assert replace_terms(text, rules) == replaced


def test_meeting_rules_with_lowered_keys_still_match():
    rules = term_rules_from_items([{
        "author_type": "user",
        "data": {"kind": "term_correction", "selected_text": "İzmir", "replacement": "Izmir"},
    }])
    assert correct_text("we met in İzmir", rules) == "we met in Izmir"
