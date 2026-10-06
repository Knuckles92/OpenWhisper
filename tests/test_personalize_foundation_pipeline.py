"""The dictation pipeline's stages: inert by default, ordered, never raising."""

import importlib
from concurrent.futures import Future

import pytest

from services import (
    app_styles,
    dictation_language,
    dictation_pipeline,
    dictionary,
    focus_context,
    snippets,
)
from services.app_styles import AppStyle
from services.batch_upload import compose_batch_cleanup_prompt
from services.cleanup_profiles import CleanupProfile, compose_profile_prompt
from services.dictation_pipeline import (
    DictationJob,
    FinishedText,
    JobMode,
    after_paste,
    begin_job,
    compose_cleanup_prompt,
    finish_text,
    history_fields,
    paste_target_ok,
    prepare_text,
    recognition_for,
    record_stats,
    text_for_paste,
)
from services.dictionary import DictionaryTerm
from services.focus_context import (
    AppIdentity,
    ContextCaptureService,
    FocusSnapshot,
    TextContext,
)
from services.recognition_context import RecognitionContext
from services.settings import (
    SettingsKey,
    compose_transcript_cleanup_prompt,
    resolve_transcript_cleanup_prompt,
)
from services.snippets import Snippet, SnippetPlan
from services.transcript_cleanup import CleanupInfo

RULES = ["Spell Acme correctly."]
OUTLOOK = AppIdentity("outlook.exe", "Outlook", pid=10, window="0x1")
CARET = TextContext(before="Dear Sam,", caret_known=True, source="uia")


def _done(value) -> Future:
    future: Future = Future()
    future.set_result(value)
    return future


def _failed(exc) -> Future:
    future: Future = Future()
    future.set_exception(exc)
    return future


def _job(mode=JobMode.DICTATION, snapshot=None, **kwargs) -> DictationJob:
    focus = _done(snapshot) if snapshot is not None else None
    return DictationJob(mode=mode, focus=focus, **kwargs)


def _today(settings, rules=RULES, profile=None, batch_context=None):
    """The prompt _maybe_cleanup_transcript built before the pipeline."""
    prompt = (
        compose_profile_prompt(profile, rules) if profile else
        compose_transcript_cleanup_prompt(resolve_transcript_cleanup_prompt(settings), rules)
    )
    if batch_context:
        prompt = compose_batch_cleanup_prompt(prompt, batch_context)
    return prompt


class FakeService(ContextCaptureService):
    def __init__(self, snapshot=None, current=None, fail=False):
        self.snapshot = snapshot or FocusSnapshot()
        self.current = current
        self.fail = fail
        self.requests = []

    def request(self, *, include_text, include_selection=False):
        self.requests.append((include_text, include_selection))
        if self.fail:
            raise RuntimeError("capture broke")
        return _done(self.snapshot)

    def current_identity(self):
        if self.fail:
            raise RuntimeError("capture broke")
        return self.current

    def reread(self, identity, callback):
        callback(None)

    def shutdown(self):
        pass


# --- inert by default -------------------------------------------------------


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True},
        {SettingsKey.TRANSCRIPT_CLEANUP_PROMPT: "  Fix my words.  "},
        {SettingsKey.TRANSCRIPT_CLEANUP_PROMPT: "   "},
        {SettingsKey.APP_CONTEXT_READ_TEXT: True, SettingsKey.TRANSCRIPT_CLEANUP_LEVEL: "high"},
    ],
)
@pytest.mark.parametrize("rules", [[], RULES])
@pytest.mark.parametrize("batch_context", [None, "Two halves of one call."])
def test_inert_prompt_is_todays_prompt(settings, rules, batch_context):
    prepared = prepare_text("hello there", _job(), settings)

    prompt = compose_cleanup_prompt(
        job=_job(snapshot=FocusSnapshot(OUTLOOK, CARET)), settings=settings,
        profile=None, rules=rules, prepared=prepared, batch_context=batch_context,
    )

    assert prompt == _today(settings, rules, batch_context=batch_context)


def test_inert_profile_prompt_is_todays_prompt():
    profile = CleanupProfile("notes", "Notes", "Make bullet notes.")
    prepared = prepare_text("hello", None, {})

    prompt = compose_cleanup_prompt(
        job=None, settings={}, profile=profile, rules=RULES, prepared=prepared,
        batch_context="ctx",
    )

    assert prompt == _today({}, profile=profile, batch_context="ctx")


@pytest.mark.parametrize("job", [None, _job(), _job(JobMode.COMMAND)])
def test_inert_text_is_unchanged(job):
    prepared = prepare_text("hello there", job, {})

    assert (prepared.original, prepared.text, prepared.cleanup_input) == ("hello there",) * 3
    assert not prepared.skip_cleanup
    assert finish_text("Hello there.", prepared) == FinishedText(
        "Hello there.", "", True, "hello there"
    )
    assert finish_text("hello there", prepared) == FinishedText(
        "hello there", "", True, "hello there"
    )
    assert text_for_paste("Hello there.", job) == "Hello there."


# --- block order ------------------------------------------------------------


@pytest.fixture
def blocks(monkeypatch):
    """Every prompt block returns its name, so the order shows in the prompt."""
    styles = []
    monkeypatch.setattr(app_styles, "style_for", lambda snapshot, settings: (
        styles.append(snapshot) or AppStyle("email", "casual", "Outlook")))
    monkeypatch.setattr(app_styles, "prompt_block", lambda style: f"STYLE {style.tone}")
    monkeypatch.setattr(dictionary, "load_dictionary", lambda settings: [DictionaryTerm("1", "Acme")])
    monkeypatch.setattr(dictionary, "prompt_block", lambda terms: "DICTIONARY")
    monkeypatch.setattr(snippets, "prompt_guard", lambda plan: "GUARD")
    monkeypatch.setattr(focus_context, "prompt_block", lambda snapshot: (
        f"CONTEXT {snapshot.text.before}"))
    return styles


def test_blocks_follow_the_base_prompt_in_order(blocks):
    settings = {SettingsKey.APP_CONTEXT_READ_TEXT: True}
    job = _job(snapshot=FocusSnapshot(OUTLOOK, CARET))
    prepared = prepare_text("hi", job, settings)

    prompt = compose_cleanup_prompt(
        job=job, settings=settings, profile=None, rules=RULES, prepared=prepared,
        batch_context="BATCH",
    )

    base = compose_transcript_cleanup_prompt(resolve_transcript_cleanup_prompt(settings), RULES)
    expected = "\n\n".join([base, "STYLE casual", "DICTIONARY", "GUARD", "CONTEXT Dear Sam,"])
    assert prompt == compose_batch_cleanup_prompt(expected, "BATCH")
    assert blocks == [job.snapshot()]


def test_profile_and_switches_leave_their_blocks_out(blocks):
    profile = CleanupProfile("notes", "Notes", "Make bullet notes.")
    job = _job(snapshot=FocusSnapshot(OUTLOOK, CARET))
    prepared = prepare_text("hi", job, {})

    with_profile = compose_cleanup_prompt(
        job=job, settings={SettingsKey.APP_CONTEXT_READ_TEXT: True}, profile=profile,
        rules=RULES, prepared=prepared,
    )
    styles_off = compose_cleanup_prompt(
        job=job, settings={SettingsKey.APP_STYLES_ENABLED: False}, profile=None,
        rules=[], prepared=prepared,
    )

    assert with_profile == "\n\n".join([
        compose_profile_prompt(profile, RULES), "DICTIONARY", "GUARD", "CONTEXT Dear Sam,",
    ])
    assert styles_off == "\n\n".join([resolve_transcript_cleanup_prompt({}), "DICTIONARY", "GUARD"])
    assert blocks == []


def test_a_failing_block_is_left_out(blocks, monkeypatch):
    monkeypatch.setattr(dictionary, "prompt_block", lambda terms: 1 / 0)
    prepared = prepare_text("hi", None, {})

    prompt = compose_cleanup_prompt(
        job=None, settings={}, profile=None, rules=[], prepared=prepared,
    )

    assert prompt == "\n\n".join([resolve_transcript_cleanup_prompt({}), "STYLE casual", "GUARD"])


# --- dictionary and snippets ------------------------------------------------


def test_dictionary_applies_to_every_job_and_snippets_only_to_dictation(monkeypatch):
    planned = []
    monkeypatch.setattr(dictionary, "apply_replacements", lambda text, terms: text.replace("acme", "Acme"))
    monkeypatch.setattr(snippets, "load_snippets", lambda settings: [Snippet("s", "my address", "1 Main St")])

    def plan(text, items):
        planned.append(text)
        return SnippetPlan("[[S1]]", placeholders=(("[[S1]]", items[0]),))

    monkeypatch.setattr(snippets, "plan_expansion", plan)

    for job in (None, _job(JobMode.COMMAND), _job(JobMode.TRANSFORM)):
        prepared = prepare_text("acme rocks", job, {})
        assert (prepared.text, prepared.cleanup_input) == ("Acme rocks", "Acme rocks")
    assert planned == []

    off = prepare_text("acme rocks", _job(), {SettingsKey.SNIPPETS_ENABLED: False})
    assert off.cleanup_input == "Acme rocks"
    on = prepare_text("acme rocks", _job(), {})
    assert (on.text, on.cleanup_input) == ("Acme rocks", "[[S1]]")
    assert planned == ["Acme rocks"]


def test_a_whole_snippet_skips_cleanup_and_expands(monkeypatch):
    snippet = Snippet("s", "my address", "1 Main St")
    monkeypatch.setattr(snippets, "load_snippets", lambda settings: [snippet])
    monkeypatch.setattr(snippets, "plan_expansion", lambda text, items: SnippetPlan(text, whole=snippet))
    monkeypatch.setattr(snippets, "expand", lambda text, plan: (
        (plan.whole.text, "", True) if plan.whole else (text, "", True)))

    prepared = prepare_text("my address", _job(), {})

    assert prepared.skip_cleanup
    assert finish_text(prepared.cleanup_input, prepared) == FinishedText(
        "1 Main St", "", True, "1 Main St"
    )


def test_a_lost_placeholder_falls_back_to_the_uncleaned_expansion(monkeypatch):
    snippet = Snippet("s", "sig", "Best, Dana", formatted=True)
    plan = SnippetPlan("thanks [[S1]]", placeholders=(("[[S1]]", snippet),))
    monkeypatch.setattr(snippets, "expand", lambda text, p: (
        (text.replace("[[S1]]", "Best, Dana"), "<p>Best, Dana</p>", True)
        if "[[S1]]" in text else (text, "", False)))
    prepared = dictation_pipeline.PreparedText(
        "thanks sig", "thanks sig", "thanks [[S1]]", plan, False
    )

    kept = finish_text("Thanks, [[S1]]", prepared)
    lost = finish_text("Thanks.", prepared)

    assert kept == FinishedText("Thanks, Best, Dana", "<p>Best, Dana</p>", True, "thanks Best, Dana")
    assert lost == FinishedText("thanks Best, Dana", "<p>Best, Dana</p>", False, "thanks Best, Dana")


# --- job start and recognition ----------------------------------------------


def test_begin_job_asks_for_focus_only_when_app_context_is_on():
    service = FakeService(FocusSnapshot(OUTLOOK))
    focus_context.set_service(service)

    dictation = begin_job(JobMode.DICTATION, {SettingsKey.APP_CONTEXT_READ_TEXT: True})
    command = begin_job(JobMode.COMMAND, {}, selection="the selected words")
    off = begin_job(JobMode.DICTATION, {SettingsKey.APP_CONTEXT_ENABLED: False})

    assert service.requests == [(True, False), (False, True)]
    assert dictation.snapshot() == FocusSnapshot(OUTLOOK)
    assert command.selection_text() == "the selected words"
    assert off.focus is None and off.snapshot() is None
    assert dictation.recognition == RecognitionContext()


def test_begin_job_survives_a_broken_capture_service():
    focus_context.set_service(FakeService(fail=True))

    job = begin_job(JobMode.DICTATION, {})

    assert job.focus is None and job.mode == JobMode.DICTATION


def test_recognition_carries_language_and_phrases_when_steering_is_on(monkeypatch):
    monkeypatch.setattr(dictation_language, "job_language", lambda settings: "de")
    monkeypatch.setattr(dictionary, "recognition_phrases", lambda settings: ("Acme", "Kubernetes"))

    assert recognition_for(None, {}) == RecognitionContext("de", ("Acme", "Kubernetes"))
    assert recognition_for(_job(), {SettingsKey.DICTIONARY_STEER_RECOGNITION: False}) == (
        RecognitionContext("de", ())
    )
    monkeypatch.setattr(dictionary, "recognition_phrases", lambda settings: 1 / 0)
    assert recognition_for(None, {}) == RecognitionContext()
    assert not RecognitionContext()


# --- futures never raise ----------------------------------------------------


def test_job_futures_never_raise_or_wait_at_timeout_zero():
    pending: Future = Future()
    job = DictationJob(focus=pending, selection=pending)
    assert job.snapshot() is None and job.selection_text() == ""
    assert job.snapshot(timeout=0.01) is None

    broken = DictationJob(focus=_failed(OSError("uia")), selection=_failed(OSError("copy")))
    assert broken.snapshot(timeout=1) is None and broken.selection_text(timeout=1) == ""

    wrong_type = DictationJob(focus=_done("not a snapshot"), selection=_done(42))
    assert wrong_type.snapshot() is None and wrong_type.selection_text() == ""


# --- delivery ----------------------------------------------------------------


def test_text_joins_the_caret_only_for_dictation_with_a_known_caret(monkeypatch):
    monkeypatch.setattr(focus_context, "join_with_context", lambda text, ctx: " " + text.lower())

    assert text_for_paste("And more.", _job(snapshot=FocusSnapshot(OUTLOOK, CARET))) == " and more."
    assert text_for_paste("And more.", _job(JobMode.COMMAND, FocusSnapshot(OUTLOOK, CARET))) == (
        "And more."
    )
    unknown = TextContext(before="Dear Sam,")
    assert text_for_paste("And more.", _job(snapshot=FocusSnapshot(OUTLOOK, unknown))) == "And more."

    monkeypatch.setattr(focus_context, "join_with_context", lambda text, ctx: 1 / 0)
    assert text_for_paste("And more.", _job(snapshot=FocusSnapshot(OUTLOOK, CARET))) == "And more."


def test_paste_target_is_refused_only_when_the_app_provably_changed():
    job = _job(JobMode.COMMAND, FocusSnapshot(OUTLOOK))

    focus_context.set_service(FakeService(current=OUTLOOK))
    assert paste_target_ok(job)
    focus_context.set_service(FakeService(current=AppIdentity("slack.exe", "Slack", pid=11)))
    assert not paste_target_ok(job)
    focus_context.set_service(FakeService(current=None))
    assert paste_target_ok(job)
    focus_context.set_service(FakeService(fail=True))
    assert paste_target_ok(job)
    assert paste_target_ok(None)
    assert paste_target_ok(_job(JobMode.COMMAND))


def test_after_paste_schedules_learning_for_dictation_only(monkeypatch):
    learned = []
    monkeypatch.setattr(dictionary, "schedule_learning", lambda job, text: learned.append((job.mode, text)))

    after_paste(_job(), "Hello")
    after_paste(_job(JobMode.COMMAND), "Rewritten")
    after_paste(None, "Upload")
    assert learned == [(JobMode.DICTATION, "Hello")]

    monkeypatch.setattr(dictionary, "schedule_learning", lambda job, text: 1 / 0)
    after_paste(_job(), "Hello")


# --- history and stats -------------------------------------------------------


def test_history_fields_name_the_kind_and_leave_unknowns_out(monkeypatch):
    monkeypatch.setattr(app_styles, "style_for", lambda snapshot, settings: AppStyle("email", "formal"))
    info = CleanupInfo("openai", "gpt", 0.4, level="medium")

    assert history_fields(None, None, live=True) == {"entry_kind": "dictation"}
    assert history_fields(None, None, live=False) == {"entry_kind": "file"}
    assert history_fields(_job(JobMode.TRANSFORM), None, live=False) == {"entry_kind": "transform"}

    job = _job(
        JobMode.COMMAND, FocusSnapshot(OUTLOOK, CARET),
        recognition=RecognitionContext(language="fr"),
    )
    assert history_fields(job, info, live=True) == {
        "entry_kind": "command",
        "app_id": "outlook.exe",
        "app_name": "Outlook",
        "app_category": "email",
        "cleanup_level": "medium",
        "language": "fr",
    }


def test_history_fields_never_hold_window_titles_or_text():
    titled = AppIdentity("chrome.exe", "Chrome", title_hint="Gmail")
    fields = history_fields(_job(snapshot=FocusSnapshot(titled, CARET)), None, live=True)

    assert "Gmail" not in fields.values() and "Dear Sam," not in fields.values()


def test_record_stats_swallows_failures(monkeypatch):
    # The module record_stats imports lazily: a package attribute can be a
    # stale copy after another test's sys.modules patch is undone.
    dictation_stats = importlib.import_module("services.dictation_stats")
    calls = []
    monkeypatch.setattr(dictation_stats, "record", lambda fields, row: calls.append(fields) or 1 / 0)

    record_stats({"entry_kind": "dictation"}, object())

    assert calls == [{"entry_kind": "dictation"}]
