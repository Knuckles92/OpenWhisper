"""The personal dictionary: storage, hints, replacements, prompt and spelling rules."""
import pytest

from config import config
from meeting.corrections import correct_text
from services import dictionary
from services.dictionary import DictionaryError, DictionaryTerm
from services.settings import SettingsKey

KEY = SettingsKey.DICTATION_DICTIONARY


def entry(term, **fields):
    return {"id": fields.pop("id", term.lower()), "term": term, **fields}


def terms_of(settings):
    return [term.term for term in dictionary.load_dictionary(settings)]


class TestLoading:
    def test_tolerates_malformed_values(self):
        assert dictionary.load_dictionary({}) == []
        assert dictionary.load_dictionary({KEY: "Kubernetes"}) == []
        settings = {KEY: [
            "loose string", None, 7, {"term": ""}, {"term": "   "}, {"term": 5},
            {"term": "x" * 65}, entry("Ksenia", starred="yes", learned=1, heard="Sonia"),
        ]}
        assert dictionary.load_dictionary(settings) == [
            DictionaryTerm("ksenia", "Ksenia", heard=("Sonia",)),
        ]

    def test_dedupes_ignoring_case_and_keeps_the_first(self):
        settings = {KEY: [entry("OpenWhisper", starred=True), entry("openwhisper", id="b"),
                          entry("Kubernetes")]}
        assert terms_of(settings) == ["OpenWhisper", "Kubernetes"]
        assert dictionary.load_dictionary(settings)[0].starred

    def test_collapses_whitespace_and_gives_stable_ids(self):
        settings = {KEY: [{"term": "  Alex   Rivera "}, {"term": "Ksenia", "id": ""}]}
        first = dictionary.load_dictionary(settings)
        assert [term.term for term in first] == ["Alex Rivera", "Ksenia"]
        assert first == dictionary.load_dictionary(settings)
        assert len({term.id for term in first}) == 2

    def test_duplicate_ids_are_made_unique(self):
        settings = {KEY: [entry("One", id="same"), entry("Two", id="same")]}
        ids = [term.id for term in dictionary.load_dictionary(settings)]
        assert ids[0] == "same" and ids[1] != "same"

    def test_caps_terms_and_heard_variants(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_DICTIONARY_TERMS", 3)
        settings = {KEY: [entry(f"Word{i}") for i in range(5)]}
        assert terms_of(settings) == ["Word0", "Word1", "Word2"]
        heard = ["a1", "a2", "A1", "Word0", "a3", "a4", "a5", "a6", "x" * 65]
        loaded = dictionary.load_dictionary({KEY: [entry("Word0", heard=heard)]})
        assert loaded[0].heard == ("a1", "a2", "a3", "a4", "a5")

    def test_learned_terms_never_keep_heard_variants_and_only_they_are_new(self):
        settings = {KEY: [entry("Ksenia", learned=True, new=True, heard=["sonia"]),
                          entry("Kubernetes", new=True)]}
        learned, typed = dictionary.load_dictionary(settings)
        assert learned.learned and learned.new and learned.heard == ()
        assert not typed.new


class TestRecognitionPhrases:
    def test_starred_first_then_saved_order(self):
        settings = {KEY: [entry("Alpha"), entry("Bravo", starred=True), entry("Charlie"),
                          entry("Delta", starred=True, learned=True)]}
        assert dictionary.recognition_phrases(settings) == ("Bravo", "Delta", "Alpha", "Charlie")

    def test_budget_keeps_the_starred_terms(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_RECOGNITION_PHRASES", 2)
        settings = {KEY: [entry("Alpha"), entry("Bravo"), entry("Charlie", starred=True)]}
        assert dictionary.recognition_phrases(settings) == ("Charlie", "Alpha")

    def test_off_when_steering_is_off(self):
        settings = {KEY: [entry("Alpha")], SettingsKey.DICTIONARY_STEER_RECOGNITION: False}
        assert dictionary.recognition_phrases(settings) == ()
        assert dictionary.recognition_phrases({}) == ()


class TestReplacements:
    def test_whole_words_any_case(self):
        terms = dictionary.load_dictionary({KEY: [entry("Ksenia", heard=["sonia", "senya"])]})
        assert dictionary.apply_replacements("Sonia and SENYA met Sonias", terms) == (
            "Ksenia and Ksenia met Sonias"
        )

    def test_longest_variant_wins_and_nothing_chains(self):
        terms = dictionary.load_dictionary({KEY: [
            entry("Kubernetes", heard=["cooper", "cooper netties"]),
            entry("Netty", heard=["Kubernetes"]),
        ]})
        assert dictionary.apply_replacements("ship cooper netties and cooper", terms) == (
            "ship Kubernetes and Kubernetes"
        )

    def test_learned_terms_never_replace(self):
        terms = [DictionaryTerm("a", "Ksenia", heard=("sonia",), learned=True)]
        assert dictionary.apply_replacements("Sonia called", terms) == "Sonia called"
        assert dictionary.apply_replacements("", terms) == ""

    def test_starred_term_wins_a_shared_variant(self):
        terms = dictionary.load_dictionary({KEY: [entry("Jon", heard=["john"]),
                                                  entry("Jonn", heard=["john"], starred=True)]})
        assert dictionary.apply_replacements("john", terms) == "Jonn"

    def test_meeting_corrections_keep_their_api(self):
        assert correct_text("the jon file", {"jon": "John"}) == "the John file"
        assert correct_text("", {"jon": "John"}) == ""


class TestPromptBlock:
    def test_lists_starred_first(self):
        terms = dictionary.load_dictionary({KEY: [entry("Alpha"), entry("Bravo", starred=True)]})
        assert dictionary.prompt_block(terms) == (
            "Vocabulary — when one of these is what was said, including near-miss "
            "mishearings, spell it exactly: Bravo, Alpha."
        )
        assert dictionary.prompt_block([]) == ""

    def test_is_budgeted(self, monkeypatch):
        monkeypatch.setattr(dictionary, "PROMPT_MAX_TERMS", 2)
        terms = dictionary.load_dictionary({KEY: [entry(f"Word{i}") for i in range(5)]})
        assert dictionary.prompt_block(terms).endswith("Word0, Word1.")
        monkeypatch.setattr(dictionary, "PROMPT_MAX_TERMS", 200)
        monkeypatch.setattr(dictionary, "PROMPT_MAX_CHARS", 14)
        assert dictionary.prompt_block(terms).endswith("Word0, Word1.")


class TestMutators:
    def test_add_puts_new_words_on_top(self):
        settings = {}
        dictionary.add_term(settings, "Kubernetes", heard=["cooper netties"])
        added = dictionary.add_term(settings, "  Ksenia ", starred=True)
        assert terms_of(settings) == ["Ksenia", "Kubernetes"]
        assert added.starred and added.id
        saved = settings[KEY][1]
        assert saved == {"id": saved["id"], "term": "Kubernetes", "starred": False,
                         "heard": ["cooper netties"], "learned": False, "new": False}

    @pytest.mark.parametrize("term,heard,message", [
        ("", (), "Type a word"),
        ("x" * 65, (), "under 64"),
        ("Word", ("y" * 65,), "under 64"),
        ("Word", ("a", "b", "c", "d", "e", "f"), "Up to 5"),
    ])
    def test_add_rejects_bad_input(self, term, heard, message):
        with pytest.raises(DictionaryError, match=message):
            dictionary.add_term({}, term, heard=heard)

    def test_re_adding_merges_and_confirms_a_learned_word(self):
        settings = {KEY: [entry("ksenia", learned=True, new=True)]}
        merged = dictionary.add_term(settings, "Ksenia", heard=["sonia"])
        assert merged.term == "Ksenia" and merged.heard == ("sonia",)
        assert not merged.learned and not merged.new
        with pytest.raises(DictionaryError, match="already"):
            dictionary.add_term(settings, "KSENIA".title(), heard=["Sonia"])
        assert len(settings[KEY]) == 1

    def test_full_dictionary_refuses(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_DICTIONARY_TERMS", 1)
        settings = {}
        dictionary.add_term(settings, "One")
        with pytest.raises(DictionaryError, match="full"):
            dictionary.add_term(settings, "Two")

    def test_update_star_delete(self):
        settings = {}
        word = dictionary.add_term(settings, "Kubernetes")
        other = dictionary.add_term(settings, "Ksenia")
        with pytest.raises(DictionaryError, match="already"):
            dictionary.update_term(settings, word.id, term="ksenia", heard=())
        updated = dictionary.update_term(settings, word.id, term="K8s", heard=["kates"])
        assert (updated.id, updated.term, updated.heard) == (word.id, "K8s", ("kates",))
        assert dictionary.set_starred(settings, other.id, True).starred
        assert dictionary.delete_term(settings, word.id).term == "K8s"
        assert dictionary.delete_term(settings, word.id) is None
        assert terms_of(settings) == ["Ksenia"]
        with pytest.raises(DictionaryError, match="no longer"):
            dictionary.set_starred(settings, "missing", True)

    def test_editing_or_starring_a_learned_word_reviews_it(self):
        settings = {KEY: [entry("Ksenia", learned=True, new=True), entry("Olu", learned=True, new=True)]}
        starred = dictionary.set_starred(settings, "ksenia", True)
        assert starred.learned and not starred.new
        edited = dictionary.update_term(settings, "olu", term="Olúwasẹ́un", heard=["olu"])
        assert not edited.learned and not edited.new

    def test_undo_learned_only_removes_learned_words(self):
        settings = {KEY: [entry("Ksenia", learned=True, new=True), entry("Typed")]}
        assert dictionary.undo_learned(settings, "typed") is None
        assert dictionary.undo_learned(settings, "ksenia").term == "Ksenia"
        assert terms_of(settings) == ["Typed"]

    def test_clear_new(self):
        settings = {KEY: [entry("A", learned=True, new=True), entry("B", learned=True, new=True),
                          entry("C")]}
        assert dictionary.clear_new(settings) == 2
        assert dictionary.clear_new(settings) == 0
        assert not any(term.new for term in dictionary.load_dictionary(settings))
        assert all(term.learned for term in dictionary.load_dictionary(settings)[:2])

    def test_add_learned(self):
        settings = {}
        learned = dictionary.add_learned(settings, "Ksenia")
        assert learned.learned and learned.new and learned.heard == ()
        assert dictionary.add_learned(settings, "ksenia") is None
        assert dictionary.add_learned({SettingsKey.DICTIONARY_LEARN_ENABLED: False}, "Olu") is None

    def test_summary(self):
        assert dictionary.summary({}) == "No words yet"
        assert dictionary.summary({KEY: [entry("A")]}) == "1 word"
        assert dictionary.summary({KEY: [entry("A"), entry("B", learned=True, new=True)]}) == (
            "2 words · 1 new"
        )

    def test_parse_heard(self):
        assert dictionary.parse_heard(" sonia, senya ;  ksen ya,, ") == ("sonia", "senya", "ksen ya")
        assert dictionary.parse_heard("") == ()

    def test_works_through_the_settings_manager(self):
        from services.settings import settings_manager

        settings_manager.mutate_settings(lambda settings: dictionary.add_term(settings, "Ksenia"))
        assert terms_of(settings_manager.load_all_settings()) == ["Ksenia"]


class TestSpellingRules:
    @pytest.mark.parametrize("rule,expected", [
        ('Always spell my name "Alex Rivera"', ("Alex Rivera", ())),
        ("Always spell the name “Ksenia” exactly as written.", ("Ksenia", ())),
        ('Spell "jon" as "John".', ("John", ("jon",))),
        ('Spell it "Ksenia", not "Sonia"', ("Ksenia", ("Sonia",))),
    ])
    def test_plain_spelling_rules(self, rule, expected):
        assert dictionary.spelling_rule(rule) == expected

    @pytest.mark.parametrize("rule", [
        "Use British spelling.",
        'Write "Kubernetes" in full.',
        'Spell "k8s" as "Kubernetes" except in code blocks.',
        'Spell "a", "b" and "c" carefully.',
        'Always spell "' + "x" * 70 + '"',
    ])
    def test_other_rules_stay_rules(self, rule):
        assert dictionary.spelling_rule(rule) is None

    def test_moving_rules(self):
        settings = {
            SettingsKey.TRANSCRIPT_CLEANUP_RULES: [
                "Use British spelling.", 'Spell "jon" as "John".', 'Always spell my name "Ksenia"',
            ],
            KEY: [entry("Ksenia")],
        }
        assert dictionary.spelling_rules(settings) == ['Spell "jon" as "John".', 'Always spell my name "Ksenia"']
        assert dictionary.move_spelling_rules(settings) == 2
        assert settings[SettingsKey.TRANSCRIPT_CLEANUP_RULES] == ["Use British spelling."]
        assert terms_of(settings) == ["John", "Ksenia"]
        assert dictionary.load_dictionary(settings)[0].heard == ("jon",)
        assert dictionary.move_spelling_rules(settings) == 0
