"""Dictionary learning: a pasted word the user corrects twice becomes a Learned term."""
from concurrent.futures import Future

import pytest

from services import dictionary, focus_context
from services.dictation_pipeline import DictationJob, JobMode, after_paste
from services.focus_context import AppIdentity, FocusSnapshot, TextContext
from services.settings import SettingsKey, settings_manager

PASTED = "Let's meet with Sonia about the launch."
NOTES = AppIdentity("notes.exe", "Notes", pid=42)


class FakeService(focus_context.NullCaptureService):
    def __init__(self, field):
        self.field = field
        self.rereads = []

    def reread(self, identity, callback):
        self.rereads.append(identity)
        callback(None if self.field is None else TextContext(
            before=self.field, after="", caret_known=True, source="fake",
        ))


@pytest.fixture
def learning(monkeypatch):
    """Runs the delayed steps at once and records the learned callback."""
    later = []
    monkeypatch.setattr(dictionary, "_run_later", lambda delay, fn, *args: later.append(delay) or fn(*args))
    monkeypatch.setattr(dictionary, "_sightings", {})
    learned = []
    dictionary.set_learned_callback(learned.append)
    settings_manager.update_settings({SettingsKey.APP_CONTEXT_READ_TEXT: True})
    yield learned, later
    dictionary.set_learned_callback(None)
    focus_context.set_service(None)


def job(identity=NOTES, *, caret_known=True, mode=JobMode.DICTATION, text=True):
    future = Future()
    context = TextContext(before="Dear team, ", caret_known=caret_known) if text else None
    future.set_result(FocusSnapshot(identity=identity, text=context))
    return DictationJob(mode=mode, focus=future)


def saved_terms():
    return dictionary.load_dictionary(settings_manager.load_all_settings())


class TestCorrectionDiff:
    @pytest.mark.parametrize("field,expected", [
        ("Dear team, Let's meet with Ksenia about the launch.", "Ksenia"),
        ("let's meet with KSENIA about the launch. See you then", "KSENIA"),
        ("Let's meet with Ksenia about the launch", "Ksenia"),
        ("Let's meet with Sonia about the Kickoff.", "Kickoff"),
    ])
    def test_single_word_substitution(self, field, expected):
        assert dictionary.learned_correction(PASTED, field) == expected

    @pytest.mark.parametrize("field", [
        "Let's meet with Sonia about the launch.",          # unchanged
        "Let's meet with sonia about the launch.",          # case only
        "Let's meet with Ksenia Petrova about the launch.",  # one word became two
        "Let's meet with Ksenia about the big launch.",      # two edits
        "Let's meet with Ksenia and the launch.",            # two edits
        "Let's meet with Monday about the launch.",          # a common word
        "Let's meet with Al about the launch.",              # too short
        "Let's meet with S0n1a_ about the launch.",          # not a word
        "Let's meet with 2024 about the launch.",            # digits only
        "Completely different text in the field.",
        "",
    ])
    def test_everything_else_is_ignored(self, field):
        assert dictionary.learned_correction(PASTED, field) is None

    def test_needs_two_unchanged_words_to_anchor(self):
        assert dictionary.learned_correction("Call Sonia", "Call Ksenia") is None
        assert dictionary.learned_correction("Call Sonia now", "Call Ksenia now") == "Ksenia"

    def test_apostrophe_or_hyphen_only_is_not_a_correction(self):
        assert dictionary.learned_correction("we dont ship today", "we don't ship today") is None
        assert dictionary.learned_correction("the eshop is open", "the e-shop is open") is None


class TestLearning:
    def test_second_sighting_adds_a_learned_word_and_notifies(self, learning):
        learned, later = learning
        service = FakeService("Dear team, Let's meet with Ksenia about the launch.")
        focus_context.set_service(service)

        dictionary.schedule_learning(job(), PASTED)
        assert saved_terms() == [] and learned == []
        dictionary.schedule_learning(job(), PASTED)

        assert service.rereads == [NOTES, NOTES]
        assert dictionary.LEARN_DELAY_S in later
        (term,) = saved_terms()
        assert (term.term, term.learned, term.new, term.heard) == ("Ksenia", True, True, ())
        assert learned == ["Ksenia"]

    def test_through_the_pipeline_after_a_paste(self, learning):
        learned, _later = learning
        focus_context.set_service(FakeService("Let's meet with Ksenia about the launch."))
        after_paste(job(), PASTED)
        after_paste(job(), PASTED)
        assert learned == ["Ksenia"]
        after_paste(job(mode=JobMode.COMMAND), PASTED)
        assert len(saved_terms()) == 1

    def test_a_known_word_is_not_added_again(self, learning):
        learned, _later = learning
        settings_manager.mutate_settings(lambda settings: dictionary.add_term(settings, "ksenia"))
        focus_context.set_service(FakeService("Let's meet with Ksenia about the launch."))
        for _ in range(2):
            dictionary.schedule_learning(job(), PASTED)
        assert learned == [] and len(saved_terms()) == 1

    @pytest.mark.parametrize("settings", [
        {SettingsKey.DICTIONARY_LEARN_ENABLED: False},
        {SettingsKey.APP_CONTEXT_READ_TEXT: False},
    ])
    def test_off_unless_learning_and_text_reading_are_on(self, learning, settings):
        learned, _later = learning
        service = FakeService("Let's meet with Ksenia about the launch.")
        focus_context.set_service(service)
        settings_manager.update_settings(settings)
        for _ in range(2):
            dictionary.schedule_learning(job(), PASTED)
        assert service.rereads == [] and learned == [] and saved_terms() == []

    @pytest.mark.parametrize("make_job", [
        lambda: None,
        lambda: job(identity=None),
        lambda: job(caret_known=False),
        lambda: job(text=False),
        lambda: job(identity=AppIdentity("openwhisper", "OpenWhisper", is_self=True)),
        lambda: DictationJob(),
    ])
    def test_needs_the_caret_text_of_another_app(self, learning, make_job):
        _learned, later = learning
        service = FakeService("Let's meet with Ksenia about the launch.")
        focus_context.set_service(service)
        dictionary.schedule_learning(make_job(), PASTED)
        assert later == [] and service.rereads == []

    def test_a_failed_reread_learns_nothing(self, learning):
        learned, _later = learning
        focus_context.set_service(FakeService(None))
        for _ in range(3):
            dictionary.schedule_learning(job(), PASTED)
        assert learned == []

    def test_an_unpending_job_never_waits(self, learning):
        _learned, later = learning
        dictionary.schedule_learning(DictationJob(focus=Future()), PASTED)
        assert later == []

    def test_scheduling_returns_before_the_delay(self, monkeypatch):
        started = []

        class Timer:
            def __init__(self, delay, function, args):
                started.append(delay)
                self.daemon = False

            def start(self):
                started.append("started")

        monkeypatch.setattr(dictionary.threading, "Timer", Timer)
        dictionary.schedule_learning(job(), PASTED)
        assert started == [dictionary.LEARN_DELAY_S, "started"]
