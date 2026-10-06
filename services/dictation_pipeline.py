"""One dictation from the recording start to its history entry.

TranscriptionRuntime calls these stages in order; the feature modules behind
them (focus context, styles, dictionary, snippets, languages, stats) fill in
what each stage does. Every stage returns its input unchanged when it has
nothing to do, so with nothing turned on a dictation behaves exactly as it
did before them, and a failing feature costs only its own step: no stage
here raises into the runtime.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Final, Mapping, Optional

from config import config
from services import (
    app_styles,
    cleanup_prompts,
    dictation_language,
    dictionary,
    focus_context,
    snippets,
)
from services.batch_upload import compose_batch_cleanup_prompt
from services.cleanup_profiles import compose_profile_prompt
from services.focus_context import FocusSnapshot
from services.recognition_context import RecognitionContext
from services.settings import (
    compose_transcript_cleanup_prompt,
    resolve_app_context_enabled,
    resolve_app_context_read_text,
    resolve_app_styles_enabled,
    resolve_dictionary_steer_recognition,
    resolve_snippets_enabled,
    resolve_transcript_cleanup_prompt,
)
from services.snippets import SnippetPlan

logger = logging.getLogger(__name__)


class JobMode:
    DICTATION: Final[str] = "dictation"
    COMMAND: Final[str] = "command"
    TRANSFORM: Final[str] = "transform"


#: entry_kind for transcripts that were not dictated live: uploads, batches
#: and re-transcribed recordings.
FILE_ENTRY_KIND: Final[str] = "file"


def _resolved(future: Optional[Future], timeout: float):
    """``future``'s result, or None when absent, failed or not ready in time."""
    if future is None:
        return None
    try:
        if timeout <= 0:
            return future.result(timeout=0) if future.done() else None
        return future.result(timeout=timeout)
    except Exception:
        return None


def _as_future(value) -> Optional[Future]:
    if value is None or isinstance(value, Future):
        return value
    if isinstance(value, str):
        future: Future = Future()
        future.set_result(value)
        return future
    return None


@dataclass(frozen=True)
class DictationJob:
    """What one recording or rewrite knows about where its text will go.

    Shared read-only between the Qt thread and workers. Qt-thread callers
    only ever use ``timeout=0``.
    """
    mode: str = JobMode.DICTATION
    #: Future[FocusSnapshot] from the capture service, or None when off.
    focus: Optional[Future] = None
    recognition: Optional[RecognitionContext] = None
    #: Future[str]: the selected text a command or transform rewrites, or a
    #: non-str marker when it was deliberately left unread (an excluded app,
    #: a password field, the shortcut's keys still down).
    selection: Optional[Future] = None
    #: Future[FocusSnapshot] of where a command or transform started, for
    #: the paste check only, also with app awareness off; history, styles
    #: and prompts read ``focus`` alone.
    target: Optional[Future] = None

    def snapshot(self, timeout: float = 0.0) -> Optional[FocusSnapshot]:
        """The focus capture, or None when it failed or isn't ready; never raises.

        ``timeout`` 0 never waits.
        """
        value = _resolved(self.focus, timeout)
        return value if isinstance(value, FocusSnapshot) else None

    def selection_text(self, timeout: float = 0.0) -> str:
        """The selected text, or "" when there is none or it isn't ready."""
        value = _resolved(self.selection, timeout)
        return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class PreparedText:
    #: The speech engine's transcript.
    original: str
    #: After dictionary replacements, before snippets.
    text: str
    #: What AI cleanup is given: ``text`` with snippet placeholders.
    cleanup_input: str
    plan: SnippetPlan
    #: The whole utterance was a snippet trigger, so no AI runs.
    skip_cleanup: bool


@dataclass(frozen=True)
class FinishedText:
    text: str
    #: Rich-text alternative to paste with ``text``, or "".
    html: str = ""
    #: False when cleanup lost a snippet placeholder and ``text`` is the
    #: uncleaned text instead.
    ok: bool = True
    #: The text before AI cleanup, with snippets expanded.
    raw_text: str = ""


def begin_job(mode: str, settings: Mapping, *, selection=None) -> DictationJob:
    """Start a job's background captures as its recording starts; never raises.

    Called right after the recorder opens and before the preview starts, so
    the focus capture runs while the user speaks.

    Args:
        mode: A JobMode value.
        settings: Settings loaded for this recording.
        selection: The text to rewrite, as a Future[str] or a str, for
            command and transform jobs.
    """
    settings = settings or {}
    focus = target = None
    try:
        if resolve_app_context_enabled(settings):
            focus = focus_context.get_service().request(
                include_text=resolve_app_context_read_text(settings),
                include_selection=mode == JobMode.COMMAND,
            )
        elif mode != JobMode.DICTATION:
            # A rewrite pastes even with auto-paste off, so it still learns
            # where it started (see DictationJob.target).
            target = focus_context.get_service().request(include_text=False)
    except Exception:
        logger.debug("Focus capture could not start", exc_info=True)
    return DictationJob(
        mode=mode,
        focus=focus,
        recognition=recognition_for(None, settings),
        selection=_as_future(selection),
        target=target,
    )


def recognition_for(job: Optional[DictationJob], settings: Mapping) -> RecognitionContext:
    """The language and vocabulary for a final pass starting now; never raises."""
    settings = settings or {}
    try:
        phrases = (
            tuple(dictionary.recognition_phrases(settings))
            if resolve_dictionary_steer_recognition(settings) else ()
        )
        return RecognitionContext(
            language=dictation_language.job_language(settings) or "",
            phrases=phrases,
        )
    except Exception:
        logger.debug("Recognition context unavailable", exc_info=True)
        return RecognitionContext()


def prepare_text(raw: str, job: Optional[DictationJob], settings: Mapping) -> PreparedText:
    """Apply the dictionary to every transcript and plan live dictation's snippets."""
    settings = settings or {}
    original = raw or ""
    text = original
    try:
        text = dictionary.apply_replacements(text, dictionary.load_dictionary(settings))
    except Exception:
        logger.debug("Dictionary replacements failed", exc_info=True)
        text = original
    plan = SnippetPlan(text)
    if (
        job is not None
        and job.mode == JobMode.DICTATION
        and resolve_snippets_enabled(settings)
    ):
        try:
            plan = snippets.plan_expansion(text, snippets.load_snippets(settings))
        except Exception:
            logger.debug("Snippet planning failed", exc_info=True)
            plan = SnippetPlan(text)
    return PreparedText(
        original=original,
        text=text,
        cleanup_input=plan.text,
        plan=plan,
        skip_cleanup=plan.whole is not None,
    )


def _block(name: str, build) -> str:
    try:
        return build() or ""
    except Exception:
        logger.debug("Cleanup prompt block %s failed", name, exc_info=True)
        return ""


def compose_cleanup_prompt(
    *,
    job: Optional[DictationJob],
    settings: Mapping,
    profile,
    rules: list[str],
    prepared: PreparedText,
    batch_context: Optional[str] = None,
) -> str:
    """The system prompt for one cleanup call.

    Order: the base prompt (a profile's, or the level preset or custom
    prompt with the learned rules), the app's style and the live list
    layout (never with a profile), the dictionary, the snippet guard, the
    text around the caret, and last
    a batch's description with its injection guard. Each block is separated
    by a blank line and left out when empty.
    """
    settings = settings or {}
    if profile is not None:
        prompt = compose_profile_prompt(profile, rules)
    else:
        try:
            base = cleanup_prompts.base_prompt(
                settings, cleanup_prompts.resolve_level(settings)
            )
        except Exception:
            logger.debug("Cleanup preset unavailable", exc_info=True)
            base = resolve_transcript_cleanup_prompt(settings)
        prompt = compose_transcript_cleanup_prompt(base, rules)

    use_style = profile is None and resolve_app_styles_enabled(settings)
    use_caret_text = resolve_app_context_read_text(settings)
    snapshot = None
    if job is not None and (use_style or use_caret_text):
        # A worker thread: the capture resolved long before cleanup starts,
        # and the deadline only bounds a service that broke its promise.
        snapshot = job.snapshot(timeout=config.CONTEXT_CAPTURE_DEADLINE_S)

    blocks = []
    style = None
    if use_style:
        try:
            style = app_styles.style_for(snapshot, settings)
        except Exception:
            logger.debug("App style unavailable", exc_info=True)
        blocks.append(_block("style", lambda: app_styles.prompt_block(style)))
    # The style block already tells a terminal to stay on one line.
    terminal_covered = style is not None and style.surface == app_styles.Surface.TERMINAL
    if (profile is None and job is not None and job.mode == JobMode.DICTATION
            and not terminal_covered):
        blocks.append(_block("lists", lambda: cleanup_prompts.inline_lists_block(
            settings, job.snapshot(timeout=config.CONTEXT_CAPTURE_DEADLINE_S))))
    blocks.append(_block("dictionary", lambda: dictionary.prompt_block(
        dictionary.load_dictionary(settings))))
    blocks.append(_block("snippets", lambda: snippets.prompt_guard(prepared.plan)))
    if use_caret_text:
        blocks.append(_block("context", lambda: focus_context.prompt_block(snapshot)))
    for block in blocks:
        if block:
            prompt = f"{prompt}\n\n{block}"
    if batch_context:
        prompt = compose_batch_cleanup_prompt(prompt, batch_context)
    return prompt


def cleanup_level(settings: Mapping, profile) -> str:
    """What CleanupInfo.level and history record for a cleanup that ran.

    A preset name, "custom" for a saved custom prompt, "profile" for a
    cleanup profile, or "" when unknown.
    """
    if profile is not None:
        return "profile"
    try:
        if cleanup_prompts.custom_prompt(settings or {}):
            return "custom"
        level = cleanup_prompts.resolve_level(settings or {})
    except Exception:
        logger.debug("Cleanup level unavailable", exc_info=True)
        return ""
    return "" if level == cleanup_prompts.CleanupLevel.NONE else level


def _expand(text: str, plan: SnippetPlan) -> Optional[tuple[str, str, bool]]:
    try:
        plain, html, ok = snippets.expand(text, plan)
        return plain, html or "", bool(ok)
    except Exception:
        logger.debug("Snippet expansion failed", exc_info=True)
        return None


def finish_text(cleaned: str, prepared: PreparedText) -> FinishedText:
    """Expand snippets in the cleaned text, or fall back to the uncleaned text.

    ``cleaned`` is cleanup's output, or ``prepared.cleanup_input`` when no
    cleanup ran.
    """
    raw = _expand(prepared.cleanup_input, prepared.plan)
    if raw is None or not raw[2]:
        # The plan cannot expand even its own text: paste the words as heard.
        raw = (prepared.text, "", True)
    raw_plain, raw_html, _ok = raw
    if cleaned == prepared.cleanup_input:
        return FinishedText(raw_plain, raw_html, True, raw_plain)
    result = _expand(cleaned, prepared.plan)
    if result is None or not result[2]:
        return FinishedText(raw_plain, raw_html, False, raw_plain)
    plain, html, _ok = result
    return FinishedText(plain, html, True, raw_plain)


def _caret_context(job: Optional[DictationJob]):
    if job is None or job.mode != JobMode.DICTATION:
        return None
    snapshot = job.snapshot(timeout=0)
    context = snapshot.text if snapshot is not None else None
    return context if context is not None and context.caret_known else None


def text_for_paste(text: str, job: Optional[DictationJob]) -> str:
    """``text`` joined onto what is already before the caret, for dictation.

    Qt thread: never waits for the capture.
    """
    context = _caret_context(job)
    if context is None:
        return text
    try:
        return focus_context.join_with_context(text, context)
    except Exception:
        logger.debug("Joining with the text before the caret failed", exc_info=True)
        return text


def html_for_paste(html: str, text: str, job: Optional[DictationJob]) -> str:
    """``html``, the rich-text twin of ``text``, given ``text_for_paste``'s join.

    ``text`` is the transcript before its join. Rich-text apps paste the
    HTML, so both flavours must agree. Qt thread: never waits.
    """
    context = _caret_context(job) if html else None
    if context is None:
        return html
    try:
        return focus_context.join_html_with_context(html, text, context)
    except Exception:
        logger.debug("Joining rich text with the text before the caret failed", exc_info=True)
        return html


class PasteTarget:
    SAME: Final[str] = "same"
    CHANGED: Final[str] = "changed"
    #: Either side could not be told: nothing proves the app is the same.
    UNKNOWN: Final[str] = "unknown"


#: How long the paste check waits on Linux, where the focused app comes
#: from hyprctl or an X11 round trip; it runs on the Qt thread.
PASTE_CHECK_TIMEOUT_S: Final[float] = 0.5


def paste_target(job: Optional[DictationJob]) -> str:
    """Whether the app that had focus at the job's start still has it.

    A PasteTarget value. Qt thread: never waits for the start's capture,
    and waits at most PASTE_CHECK_TIMEOUT_S for the current app.
    """
    future = None
    if job is not None:
        future = job.target if job.target is not None else job.focus
    value = _resolved(future, 0)
    expected = value.identity if isinstance(value, FocusSnapshot) else None
    if expected is None:
        return PasteTarget.UNKNOWN
    try:
        current = focus_context.get_service().identity_now(PASTE_CHECK_TIMEOUT_S)
    except Exception:
        logger.debug("Current focus unavailable", exc_info=True)
        return PasteTarget.UNKNOWN
    if current is None:
        return PasteTarget.UNKNOWN
    same = (expected.app_id, expected.pid, expected.window) == (
        current.app_id, current.pid, current.window
    )
    return PasteTarget.SAME if same else PasteTarget.CHANGED


def paste_target_ok(job: Optional[DictationJob]) -> bool:
    """Whether a command or transform may paste: its app provably still has focus.

    A rewrite is pasted even with auto-paste off, so an unknown app counts
    as changed rather than pasting it blind.
    """
    return paste_target(job) == PasteTarget.SAME


def after_paste(job: Optional[DictationJob], pasted_text: str) -> None:
    """Follow-up work once a dictation is pasted; Qt thread, never blocks."""
    if job is None or job.mode != JobMode.DICTATION:
        return
    try:
        dictionary.schedule_learning(job, pasted_text)
    except Exception:
        logger.debug("Dictionary learning could not be scheduled", exc_info=True)


def history_fields(
    job: Optional[DictationJob],
    info,
    *,
    live: bool,
    settings: Optional[Mapping] = None,
) -> dict:
    """The context columns for a history entry, with unknown ones left out.

    Args:
        job: The delivered job, or None (uploads, and live dictations in
            tests).
        info: The CleanupInfo of a cleanup that ran, or None.
        live: The transcript was dictated live rather than read from a file;
            decides entry_kind when there is no command or transform job.
        settings: For the app's category, which user overrides can change.
    """
    if job is not None and job.mode != JobMode.DICTATION:
        kind = job.mode
    else:
        kind = JobMode.DICTATION if live else FILE_ENTRY_KIND
    fields = {"entry_kind": kind}
    snapshot = job.snapshot(timeout=0) if job is not None else None
    identity = snapshot.identity if snapshot is not None else None
    if identity is not None:
        fields["app_id"] = identity.app_id
        fields["app_name"] = identity.name
        try:
            # Not style_for: stats keep the category with styles turned off.
            fields["app_category"] = app_styles.category_for(snapshot, settings or {})
        except Exception:
            logger.debug("App category unavailable", exc_info=True)
    fields["cleanup_level"] = getattr(info, "level", "") if info is not None else ""
    recognition = job.recognition if job is not None else None
    fields["language"] = recognition.language if recognition is not None else ""
    return {key: value for key, value in fields.items() if value not in (None, "")}


def record_stats(entry_fields: dict, row) -> None:
    """Add the saved entry to the local stats; history-save worker, never raises."""
    try:
        from services.dictation_stats import record

        record(entry_fields, row)
    except Exception:
        logger.debug("Could not record dictation stats", exc_info=True)
