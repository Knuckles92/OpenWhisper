"""Command Mode and transforms: rewrite the selection, or write at the cursor.

Command Mode records a spoken instruction like a dictation and hands the
transcript here instead of to cleanup; a transform rewrites the selection
with a saved instruction and records nothing. Both read the selection from
UI Automation when the focus capture knows it, and otherwise copy it once
the shortcut's keys are up, never in a terminal, an app the user excluded
or a password field. Both run on the AI cleanup client inside the
transcription job slot, so a Cancel ends them and their result is pasted
and saved like a dictation's. Selections, instructions and results are
never logged.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from concurrent.futures import Future
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from config import config
from services import dictation_pipeline, focus_context, synthetic_keys, text_rewrite
from services.dictation_pipeline import DictationJob, JobMode
from services.settings import (
    RecordingTriggerMode,
    resolve_app_context_enabled,
    resolve_app_context_read_text,
    resolve_command_mode_insert_without_selection,
    resolve_recording_trigger_mode,
    settings_manager,
)
from services.text_transforms import find_transform
from services.transcript_cleanup import CleanupInfo
from ui_qt.overlay_state import OverlayState

if TYPE_CHECKING:
    from services.application_controller import ApplicationController

logger = logging.getLogger(__name__)

NO_PROVIDER_MESSAGE = "Set up AI cleanup to use Command Mode"
KEYS_HELD_MESSAGE = "Release the shortcut keys, then try again"
PASSWORD_MESSAGE = "OpenWhisper doesn't read password fields"
CLIPBOARD_HIDDEN_MESSAGE = "OpenWhisper can't read selected text on this desktop"
#: How long the worker waits for the selection after the transcript is in.
#: It covers the modifier wait and the copy's own timeout, both started at
#: the stop, which the post-roll and the final decode usually outlast.
SELECTION_WAIT_S = (
    synthetic_keys.MODIFIER_WAIT_S
    + config.COMMAND_SELECTION_TIMEOUT_MS / 1000
    + config.CONTEXT_CAPTURE_DEADLINE_S
)


class CommandRefused(RuntimeError):
    """Nothing to paste, for a reason the user acts on.

    Not a failed transcription: the runtime shows the message as it is and
    keeps no instruction audio.
    """


@dataclass(frozen=True)
class UnreadSelection:
    """A selection deliberately left unread, and the message that says why.

    Resolves a job's selection Future in place of the text, so
    ``DictationJob.selection_text`` gives "" and nothing writes over a
    selection that is still there.
    """

    message: str


def _resolve(future: Optional[Future], value) -> None:
    if future is not None and not future.done():
        try:
            future.set_result(value)
        except Exception:
            pass  # resolved by another thread in between


def _snapshot(future: Optional[Future], timeout: float):
    if future is None:
        return None
    try:
        value = future.result(timeout=timeout)
    except Exception:
        return None
    return value if isinstance(value, focus_context.FocusSnapshot) else None


def _refusal(snapshot) -> Optional[UnreadSelection]:
    """Why the capture refused to read this app's selection, or None."""
    text = snapshot.text if snapshot is not None else None
    if text is None or not text.blocked:
        return None
    if text.source != focus_context.EXCLUDED_SOURCE:
        return UnreadSelection(PASSWORD_MESSAGE)
    kind = focus_context.catalog.classify(snapshot.identity)
    name = kind.name if kind is not None and kind.name else "this app"
    return UnreadSelection(f"OpenWhisper doesn't read text in {name}")


def _known_selection(snapshot) -> Optional[str]:
    """The selection UI Automation read, or None when it is unknown."""
    text = snapshot.text if snapshot is not None else None
    if text is None or not text.selection_known:
        return None
    return text.selected or ""


def _looks_like_copied_line(text: str) -> bool:
    """Whether ``text`` is what a copy-line editor copies with nothing selected.

    VS Code, Visual Studio, JetBrains IDEs, Sublime Text and Zed copy the
    caret's whole line with its line break. A real selection rarely is
    exactly one line ending in one, so only that shape counts as "nothing
    selected" and selections in those editors still work without UI
    Automation.
    """
    if not text.endswith("\n"):
        return False
    return "\n" not in text[:-1].rstrip("\r")


class _QtCall(QObject):
    """Runs callables on the Qt thread, where this object lives."""

    requested = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.requested.connect(self._run)

    def _run(self, call) -> None:
        try:
            call()
        except Exception:
            logger.exception("A Command Mode step failed")


@dataclass(frozen=True)
class _Recording:
    """A Command Mode recording this runtime started."""

    selection: Future
    job: Optional[DictationJob]
    press_at: float
    trigger_mode: str


class CommandRuntime:
    """Owns Command Mode recordings and transform runs for the controller."""

    def __init__(self, controller: "ApplicationController"):
        self.controller = controller
        self._lock = threading.Lock()
        self._qt = _QtCall()
        self._closed = False
        # From its start until the shortcut stops or cancels it; only the
        # hotkey dispatcher thread replaces it.
        self._recording: Optional[_Recording] = None
        # Where each recording's selection is read from, and the reads already
        # started. A recording stopped some other way (the record shortcut,
        # the Stop button) has its selection read by the worker instead,
        # still exactly once.
        self._focus_for: "weakref.WeakKeyDictionary[Future, Optional[Future]]" = (
            weakref.WeakKeyDictionary()
        )
        self._reading: "weakref.WeakSet[Future]" = weakref.WeakSet()

    def key_pressed(self, at: float) -> None:
        """The command_mode shortcut went down; ``at`` is the hook's monotonic time.

        Called on the hotkey dispatcher thread, in event order.
        """
        if self._closed:
            return
        recording = self._recording
        if recording is not None and self._running(recording):
            if recording.trigger_mode == RecordingTriggerMode.TOGGLE:
                self._stop(recording)
            return
        self._recording = None
        self._start(at)

    def key_released(self, at: float) -> None:
        """The command_mode shortcut came up; dispatcher thread."""
        recording = self._recording
        if recording is None or recording.trigger_mode != RecordingTriggerMode.PUSH_HOLD:
            return
        self._recording = None
        if not self._running(recording):
            return
        if (at - recording.press_at) * 1000 < config.RECORD_MIN_HOLD_MS:
            self.controller.cancel()
        else:
            self._stop(recording)

    def _running(self, recording: _Recording) -> bool:
        runtime = self.controller.transcription_runtime
        # A Cancel or a failed capture ends the recording without a key event.
        return bool(
            self.controller.recorder.is_recording
            and getattr(runtime, "_job", None) is recording.job
        )

    def _start(self, at: float) -> None:
        settings = settings_manager.load_all_settings()
        if not text_rewrite.provider_ready(settings):
            self.controller.status_update.emit(NO_PROVIDER_MESSAGE)
            return
        runtime = self.controller.transcription_runtime
        # A stop's post-roll also keeps the recorder running; the start
        # below reports that job as busy itself.
        if self.controller.recorder.is_recording and not getattr(runtime, "has_active_job", False):
            self.controller.status_update.emit(
                "Finish the current recording before using Command Mode"
            )
            return
        probe = None
        if not resolve_app_context_enabled(settings):
            # Before the start, so the capture still sees the app the user
            # is in rather than OpenWhisper's overlay.
            probe = self._request_focus()
        selection: Future = Future()
        if not runtime.start_recording(mode=JobMode.COMMAND, selection=selection):
            return
        job = getattr(runtime, "_job", None)
        with self._lock:
            self._focus_for[selection] = (
                job.focus if job is not None and job.focus is not None else probe
            )
        self._recording = _Recording(
            selection=selection,
            job=job,
            press_at=at,
            trigger_mode=resolve_recording_trigger_mode(settings),
        )

    def _stop(self, recording: _Recording) -> None:
        self._recording = None
        self.controller.stop_recording()
        self._read_selection_once(recording.selection)

    def _read_selection_once(self, selection: Future) -> None:
        """Start reading a command recording's selection unless that has begun."""
        with self._lock:
            if selection.done() or selection in self._reading:
                return
            self._reading.add(selection)
            focus = self._focus_for.get(selection)
        threading.Thread(
            target=self._read_selection,
            args=(focus, lambda text: _resolve(selection, text)),
            name="command-selection",
            daemon=True,
        ).start()

    @staticmethod
    def _request_focus() -> Optional[Future]:
        try:
            return focus_context.get_service().request(
                include_text=False, include_selection=True
            )
        except Exception:
            logger.debug("Focus capture could not start", exc_info=True)
            return None

    def _read_selection(self, focus: Optional[Future], done: Callable[[object], None]) -> None:
        """Find the selected text off the Qt thread, then ``done(text)``.

        UI Automation's answer wins, even an empty one. Otherwise the text is
        copied once the shortcut's modifiers are up, except in a terminal,
        where a copy shortcut would interrupt the running program. ``done``
        gets an UnreadSelection instead when the app or field must not be
        read, the modifiers stayed down, or OpenWhisper's clipboard can't
        see other apps' copies; nothing is copied then.
        """
        try:
            snapshot = _snapshot(focus, config.CONTEXT_CAPTURE_DEADLINE_S)
            refusal = _refusal(snapshot)
            if refusal is not None:
                done(refusal)
                return
            known = _known_selection(snapshot)
            if known is not None:
                done(known)
                return
            identity = snapshot.identity if snapshot is not None else None
            if synthetic_keys.is_terminal(identity):
                done("")
                return
            copy_line = synthetic_keys.copies_line_without_selection(identity)
            if not synthetic_keys.wait_for_modifiers_released():
                # The app would get Ctrl+C plus the held keys: Ctrl+Alt+C
                # types a character on AltGr layouts, Ctrl+Shift+C opens
                # DevTools in browsers.
                logger.info("Shortcut keys still held; not copying the selection")
                done(UnreadSelection(KEYS_HELD_MESSAGE))
                return

            def copied(text: Optional[str]) -> None:
                if text is None:
                    done(UnreadSelection(CLIPBOARD_HIDDEN_MESSAGE))
                    return
                done("" if copy_line and _looks_like_copied_line(text) else text)

            self._qt.requested.emit(
                lambda: self.controller.ui_controller.capture_selection(copied)
            )
        except Exception:
            logger.exception("Couldn't read the selection")
            done("")

    def run_transform(self, transform_id: str) -> None:
        """Rewrite the current selection with a saved transform; Qt thread."""
        if self._closed:
            return
        settings = settings_manager.load_all_settings()
        transform = find_transform(settings, transform_id)
        if transform is None:
            self.controller.status_update.emit("That transform no longer exists")
            return
        if not text_rewrite.provider_ready(settings):
            self.controller.status_update.emit("Set up AI cleanup to use transforms")
            return
        focus = self._request_focus()
        selection: Future = Future()
        job = DictationJob(
            mode=JobMode.TRANSFORM,
            # Without app context the capture only finds the selection and
            # where to paste; the job's focus, and so history, does not
            # learn the app.
            focus=focus if resolve_app_context_enabled(settings) else None,
            selection=selection,
            target=focus,
        )
        runtime = self.controller.transcription_runtime
        if not runtime.begin_rewrite_job(job, source_name=f"Transform · {transform.name}"):
            return
        self.controller.status_update.emit(f"Rewriting · {transform.name}...")

        def selected(text) -> None:
            self._qt.requested.emit(lambda: self._transform_selected(job, transform, text))

        threading.Thread(
            target=self._read_selection,
            args=(focus, selected),
            name="transform-selection",
            daemon=True,
        ).start()

    def _transform_selected(self, job: DictationJob, transform, text) -> None:
        runtime = self.controller.transcription_runtime
        _resolve(job.selection, text)
        if isinstance(text, UnreadSelection):
            runtime.abandon_rewrite_job(text.message)
            return
        if not text.strip():
            runtime.abandon_rewrite_job("Select text to transform")
            return
        runtime.submit_rewrite(lambda: self._rewrite(text, transform.instruction, job=job))

    def _rewrite(self, selection: str, instruction: str, *, job=None):
        """Run one rewrite on the shared cleanup client; executor worker."""
        settings = settings_manager.load_all_settings()
        cleaner = self.controller.transcription_runtime._transcript_cleanup
        # The job slot is held, so configuring the shared client cannot race
        # a dictation's cleanup.
        text_rewrite.configure_cleaner(cleaner, settings)
        if not cleaner.is_available():
            raise CommandRefused(NO_PROVIDER_MESSAGE)
        context_block = ""
        if job is not None and resolve_app_context_read_text(settings):
            try:
                context_block = focus_context.prompt_block(
                    job.snapshot(timeout=config.CONTEXT_CAPTURE_DEADLINE_S)
                )
            except Exception:
                logger.debug("Caret context unavailable", exc_info=True)
        started = time.monotonic()
        result, error = text_rewrite.rewrite_with(
            cleaner,
            selection,
            instruction,
            dictionary_block=text_rewrite.dictionary_block(settings),
            context_block=context_block,
        )
        if error:
            raise (CommandRefused if error in text_rewrite.REFUSALS else RuntimeError)(error)
        info = CleanupInfo(
            provider=cleaner.provider,
            model=cleaner.model,
            elapsed_s=time.monotonic() - started,
            level="",
        )
        return result, (selection if selection.strip() else None), info

    def complete_recording(
        self, raw: str, job,
    ) -> tuple[str, Optional[str], Optional[CleanupInfo]]:
        """Turn a command recording's transcript into the text to paste.

        Runs on the executor worker. Returns ``(text, raw_text, info)`` like
        the dictation path, with the original selection as ``raw_text``.

        Raises:
            CommandRefused: When there is nothing to do, with the message to show.
            RuntimeError: With a user-facing message when the AI model fails.
        """
        settings = settings_manager.load_all_settings()
        instruction = dictation_pipeline.prepare_text(raw, job, settings).text.strip()
        if not instruction:
            raise CommandRefused(text_rewrite.NO_INSTRUCTION_MESSAGE)
        selection = ""
        if job is not None and job.selection is not None:
            self._read_selection_once(job.selection)
            selection = job.selection_text(timeout=SELECTION_WAIT_S)
            if not job.selection.done():
                raise CommandRefused("Couldn't read the selected text. Try again.")
            unread = job.selection.result()
            if isinstance(unread, UnreadSelection):
                # Writing at the cursor would replace a selection still there.
                raise CommandRefused(unread.message)
        writing = not selection.strip()
        if writing and not resolve_command_mode_insert_without_selection(settings):
            raise CommandRefused("Select text first")
        self.controller.overlay_state_update.emit(OverlayState.REWRITING)
        self.controller.status_update.emit("Writing..." if writing else "Rewriting...")
        return self._rewrite(selection, instruction, job=job)

    def cleanup(self) -> None:
        self._closed = True
        self._recording = None
