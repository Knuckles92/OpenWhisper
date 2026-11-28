"""The desktop app's AppControl for the live API and MCP.

Contract: docs/design/automation/design.md, "services/automation/control.py".

The listener calls in from its own threads. Reading the app's state is
thread-safe and happens there; anything that starts, stops or shows
something runs on the Qt application thread through one session-lived relay
(the Qt worker relay pattern, like ``ui_qt.widgets.agent_picker._ScanRelay``):
the caller queues ``(callable, Future)`` to it and waits up to two seconds.
"""

from __future__ import annotations

import logging
import threading
import weakref
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from functools import partial
from typing import Any, Callable, Optional, TypeVar

from PyQt6.QtCore import QCoreApplication, QObject, Qt, QThread, pyqtSignal, pyqtSlot

from services.automation.control import (
    BUSY,
    NOT_FOUND,
    REFUSED,
    UNAVAILABLE,
    ControlError,
    LiveState,
    set_app_control,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: How long a caller waits for the Qt thread to take its call.
CALL_TIMEOUT_S = 2.0
#: How much longer it waits once the call is already running there, so the
#: answer is what happened rather than a guess.
RUNNING_GRACE_S = 2.0

_NOT_READY = "OpenWhisper isn't ready for this yet."
_NO_ACTION = "No enabled action has that id."


class _Relay(QObject):
    """Runs calls from the listener's threads on the Qt application thread.

    One per session, parentless, created on first use and moved to the
    application thread; it never holds the controller between calls. Its
    slots are decorated: an undecorated bound method gets a PyQt proxy that
    stays on the thread that connected it, where no event loop runs.
    """

    call = pyqtSignal(object, object)

    def __init__(self) -> None:
        super().__init__()
        self._statuses: Optional[list[str]] = None
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @pyqtSlot(object, object)
    def _run(self, fn: Callable[[], Any], future: Future) -> None:
        if not future.set_running_or_notify_cancel():
            return  # the caller gave up before the Qt thread got here
        try:
            result = fn()
        except Exception as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)

    def with_statuses(self, signal, fn: Callable[[], T]) -> tuple[T, list[str]]:
        """Run ``fn`` and return what it reported on ``signal`` meanwhile; Qt thread."""
        statuses: list[str] = []
        self._statuses = statuses
        signal.connect(self._note_status)
        try:
            return fn(), statuses
        finally:
            signal.disconnect(self._note_status)
            self._statuses = None

    @pyqtSlot(str)
    def _note_status(self, text: str) -> None:
        if self._statuses is not None:
            self._statuses.append(text)


_relay: Optional[_Relay] = None
_relay_lock = threading.Lock()


def _app_thread() -> QThread:
    app = QCoreApplication.instance()
    if app is None:
        raise ControlError(UNAVAILABLE, _NOT_READY)
    return app.thread()


def _get_relay() -> _Relay:
    global _relay
    with _relay_lock:
        if _relay is None:
            relay = _Relay()
            relay.moveToThread(_app_thread())
            _relay = relay
        return _relay


def call_on_qt(fn: Callable[[], T]) -> T:
    """``fn()`` on the Qt application thread, from any thread.

    Raises:
        ControlError: ``unavailable`` when the Qt thread doesn't take the
            call within ``CALL_TIMEOUT_S`` (it then never runs), or doesn't
            finish one it already started; otherwise whatever ``fn`` raised.
    """
    if QThread.currentThread() is _app_thread():
        return fn()
    relay = _get_relay()
    future: Future = Future()
    relay.call.emit(fn, future)
    try:
        return future.result(timeout=CALL_TIMEOUT_S)
    except FutureTimeout:
        pass
    if future.cancel():
        raise ControlError(UNAVAILABLE, "OpenWhisper didn't respond in time. Try again.")
    try:
        return future.result(timeout=RUNNING_GRACE_S)
    except FutureTimeout:
        raise ControlError(
            UNAVAILABLE, "OpenWhisper is still working on that. Check its state in a moment."
        ) from None


# -- the work, on the Qt thread ------------------------------------------------

def _enabled_action(action_id: str):
    """The enabled action ``action_id`` for a program; raises when it may not have it.

    Every AppControl caller is a program (the live API or an MCP agent), so
    the words are a program's: an action that wouldn't take them (an agent or
    Claude Code channel action without "send words to your coding agents",
    ``program_words_allowed``) is refused now rather than after recording.
    """
    from services.actions.model import find_action
    from services.actions.runtime import program_words_allowed
    from services.automation import permissions
    from services.settings import settings_manager

    saved = settings_manager.load_all_settings()
    action = find_action(saved, action_id)
    if action is None or not action.enabled:
        raise ControlError(NOT_FOUND, _NO_ACTION)
    if not program_words_allowed(action, saved):
        raise permissions.PermissionDenied(permissions.AGENT_PROMPTS)
    return action


def _start(
    controller,
    action_id: str,
    source: str,
    *,
    profile_id: str = "",
    route: bool = False,
) -> str:
    runtime = controller.transcription_runtime
    if controller.recorder.is_recording:
        raise ControlError(BUSY, "OpenWhisper is already recording.")
    if runtime.has_active_job:
        raise ControlError(BUSY, "OpenWhisper is busy with another recording.")
    if controller.is_meeting_active():
        raise ControlError(BUSY, "Meeting Mode is active; end the meeting first.")
    if sum(bool(flag) for flag in (action_id, profile_id, route)) > 1:
        raise ControlError(REFUSED, "Choose at most one of action_id, profile_id, or route.")
    if action_id:
        _enabled_action(action_id)
        start = partial(controller.start_action_recording, action_id, source=source)
    elif profile_id:
        from services.cleanup_profiles import find_cleanup_profile
        from services.settings import settings_manager

        if find_cleanup_profile(settings_manager.load_all_settings(), profile_id) is None:
            raise ControlError(NOT_FOUND, f"No cleanup profile has id {profile_id!r}.")
        start = partial(controller.start_profile_recording, profile_id, source=source)
    elif route:
        from services.actions import routing
        from services.settings import settings_manager

        status = routing.status(settings_manager.load_all_settings())
        if not status.ready:
            raise ControlError(REFUSED, status.message)
        start = partial(runtime.start_recording, route="api", source=source)
    else:
        # What the record shortcut starts, with the caller as its source;
        # it refuses during backup or while the engine isn't ready.
        start = partial(runtime.start_recording, source=source)
    started, statuses = _get_relay().with_statuses(controller.status_update, start)
    if started is False:
        raise ControlError(REFUSED, statuses[-1] if statuses else "OpenWhisper couldn't start recording.")
    job = runtime.current_job
    return job.id if job is not None else ""


def _stop(controller) -> None:
    if not controller.recorder.is_recording:
        raise ControlError(REFUSED, "Nothing is recording.")
    controller.stop_recording()


def _cancel(controller) -> None:
    # Recordings and jobs still working that no Cancel has reached yet: the
    # actions the user started are theirs to stop, including those a
    # delivered job (only being saved) handed its words to.
    runtime = controller.transcription_runtime
    if not runtime.cancelable or not runtime.cancel():
        raise ControlError(REFUSED, "Nothing is recording or transcribing.")


def _notify(controller, text: str, title: str) -> None:
    text = " ".join(text.split())
    if not text:
        # The MCP tool only requires one character, which may be a space.
        raise ControlError(REFUSED, "There's nothing to show.")
    title = " ".join(title.split())
    message = f"{title}: {text}" if title else text
    ui = controller.ui_controller
    ui.set_status(message)
    ui.show_notice(message)


class QtAppControl:
    """``AppControl`` for the running desktop app; any thread.

    Holds the controller weakly, so a registration left behind never keeps
    the app's objects alive; calls after the controller is gone are
    ``unavailable``.
    """

    def __init__(self, controller) -> None:
        self._controller = weakref.ref(controller)

    def _require(self):
        controller = self._controller()
        if controller is None:
            raise ControlError(UNAVAILABLE, _NOT_READY)
        return controller

    def state(self) -> LiveState:
        from _version import __version__
        from services.automation import app_control_ext as ext

        controller = self._require()
        runtime = controller.transcription_runtime
        recording = bool(controller.recorder.is_recording)
        # Until its history is saved: a start is busy until then.
        busy = bool(runtime.has_active_job)
        # As it is now: the action routing chose, once it did.
        job = runtime.current_job if (recording or busy) else None
        return LiveState(
            recording=recording,
            busy=busy,
            meeting=bool(controller.is_meeting_active()),
            mode=str(getattr(job, "mode", "") or ""),
            action_id=str(getattr(job, "action_id", "") or ""),
            job_id=str(getattr(job, "id", "") or ""),
            version=__version__,
            meeting_state=ext.meeting_status(controller),
            walkthrough_state=ext.walkthrough_status(controller),
        )

    def list_actions(self) -> list[dict]:
        from services.actions.model import load_actions
        from services.settings import settings_manager

        return [
            action.public_dict()
            for action in load_actions(settings_manager.load_all_settings())
            if action.enabled
        ]

    def run_action(self, action_id: str, text: str, *, source: str) -> str:
        controller = self._require()
        return call_on_qt(partial(
            controller.transcription_runtime.run_action_text, action_id, text, source=source
        ))

    def start_recording(
        self,
        *,
        action_id: str = "",
        profile_id: str = "",
        route: bool = False,
        source: str,
    ) -> str:
        return call_on_qt(
            partial(
                _start,
                self._require(),
                action_id,
                source,
                profile_id=profile_id,
                route=route,
            )
        )

    def stop_recording(self) -> None:
        call_on_qt(partial(_stop, self._require()))

    def cancel(self) -> None:
        call_on_qt(partial(_cancel, self._require()))

    def notify(self, text: str, *, title: str = "") -> None:
        call_on_qt(partial(_notify, self._require(), text, title))

    # ---- Profiles, transforms & file transcription ----

    def list_profiles(self) -> list[dict]:
        from services.automation import app_control_ext as ext

        return ext.list_profiles()

    def list_transforms(self) -> list[dict]:
        from services.automation import app_control_ext as ext

        return ext.list_transforms()

    def run_transform(self, transform_id: str, text: str, *, source: str = "mcp") -> str:
        from services.automation import app_control_ext as ext

        return ext.run_transform(self._require(), transform_id, text)

    def transcribe_audio_file(
        self, path: str, *, language: Optional[str] = None, source: str = "mcp"
    ) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(
            partial(ext.transcribe_audio_file, self._require(), path, language=language)
        )

    # ---- Meeting Mode ----

    def meeting_status(self) -> dict:
        from services.automation import app_control_ext as ext

        return ext.meeting_status(self._require())

    def start_meeting(
        self, *, title: str = "", cloud: Optional[bool] = None, source: str = "mcp"
    ) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.start_meeting, self._require(), title=title, cloud=cloud))

    def pause_meeting(self) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.pause_meeting, self._require()))

    def resume_meeting(self) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.resume_meeting, self._require()))

    def end_meeting(self) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.end_meeting, self._require()))

    def background_meeting(self) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.background_meeting, self._require()))

    def retry_meeting_finalization(
        self, *, step: str = "failed", meeting_id: Optional[str] = None
    ) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(
            partial(ext.retry_meeting_finalization, self._require(), step=step, meeting_id=meeting_id)
        )

    def manage_recovered_meeting(self, meeting_id: str, action: str) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.manage_recovered_meeting, self._require(), meeting_id, action))

    def open_meeting_view(self, *, meeting_id: str = "", view: str = "dashboard") -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.open_meeting_view, self._require(), meeting_id=meeting_id, view=view))

    def get_meeting_dashboard_state(self, *, meeting_id: Optional[str] = None) -> dict:
        from services.automation import app_control_ext as ext

        return ext.get_meeting_dashboard_state(self._require(), meeting_id=meeting_id)

    def update_live_meeting(
        self,
        *,
        title: Optional[str] = None,
        intent: Optional[str] = None,
        topic: Optional[str] = None,
        cloud_enabled: Optional[bool] = None,
    ) -> dict:
        from services.automation import app_control_ext as ext

        return ext.update_live_meeting(
            self._require(),
            title=title,
            intent=intent,
            topic=topic,
            cloud_enabled=cloud_enabled,
        )

    def manage_meeting_card_item(
        self,
        *,
        action: str,
        card: Optional[str] = None,
        item_id: Optional[str] = None,
        text: Optional[str] = None,
        data: Optional[dict] = None,
        evidence: Optional[list[str]] = None,
    ) -> dict:
        from services.automation import app_control_ext as ext

        return ext.manage_meeting_card_item(
            self._require(),
            action=action,
            card=card,
            item_id=item_id,
            text=text,
            data=data,
            evidence=evidence,
        )

    def manage_meeting_question(
        self,
        *,
        action: str,
        text: Optional[str] = None,
        question_id: Optional[str] = None,
        answer_text: Optional[str] = None,
        evidence: Optional[list[str]] = None,
    ) -> dict:
        from services.automation import app_control_ext as ext

        return ext.manage_meeting_question(
            self._require(),
            action=action,
            text=text,
            question_id=question_id,
            answer_text=answer_text,
            evidence=evidence,
        )

    def update_meeting_transcript_or_speaker(
        self,
        *,
        action: str,
        participant_id: Optional[str] = None,
        display_name: Optional[str] = None,
        segment_id: Optional[str] = None,
        text: Optional[str] = None,
    ) -> dict:
        from services.automation import app_control_ext as ext

        return ext.update_meeting_transcript_or_speaker(
            self._require(),
            action=action,
            participant_id=participant_id,
            display_name=display_name,
            segment_id=segment_id,
            text=text,
        )

    def request_meeting_note_adjustment(
        self, *, text: str, wait: bool = True, timeout_s: float = 60.0
    ) -> dict:
        from services.automation import app_control_ext as ext

        return ext.request_meeting_note_adjustment(
            self._require(), text=text, wait=wait, timeout_s=timeout_s
        )

    def request_meeting_custom_report(
        self, *, request_text: str, meeting_id: Optional[str] = None
    ) -> dict:
        from services.automation import app_control_ext as ext

        return ext.request_meeting_custom_report(
            self._require(), request_text=request_text, meeting_id=meeting_id
        )

    def undo_live_meeting_action(self, *, event_seq: int) -> dict:
        from services.automation import app_control_ext as ext

        return ext.undo_live_meeting_action(self._require(), event_seq=event_seq)

    # ---- Walkthroughs & screenshots ----

    def walkthrough_status(self) -> dict:
        from services.automation import app_control_ext as ext

        return ext.walkthrough_status(self._require())

    def start_walkthrough(
        self, *, snap_mode: str = "", auto_mode: Optional[bool] = None, source: str = "mcp"
    ) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(
            partial(ext.start_walkthrough, self._require(), snap_mode=snap_mode, auto_mode=auto_mode, source=source)
        )

    def finish_walkthrough(self) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.finish_walkthrough, self._require()))

    def cancel_walkthrough(self) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.cancel_walkthrough, self._require()))

    def set_walkthrough_paused(self, paused: bool) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.set_walkthrough_paused, self._require(), paused))

    def snap_walkthrough(self, *, snap_mode: str = "", source: str = "mcp") -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.snap_walkthrough, self._require(), snap_mode=snap_mode, source=source))

    def choose_walkthrough_snap_mode(self, mode: str) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.choose_walkthrough_snap_mode, self._require(), mode))

    def edit_walkthrough_shots(
        self, action: str, *, number: Optional[int] = None, step: Optional[int] = None
    ) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.edit_walkthrough_shots, self._require(), action, number=number, step=step))

    def take_screenshot(self, *, source: str = "mcp") -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.take_screenshot, self._require(), source=source))

    def list_walkthroughs(self, *, q: str = "", limit: int = 20) -> dict:
        from services.automation import app_control_ext as ext

        return ext.list_walkthroughs(q=q, limit=limit)

    def get_walkthrough(
        self, ref: str, *, spoken: bool = False, include_images: bool = False
    ) -> dict:
        from services.automation import app_control_ext as ext

        return ext.get_walkthrough(ref, spoken=spoken, include_images=include_images)

    def run_walkthrough_writeup(self, ref: str, *, style: str = "") -> dict:
        from services.automation import app_control_ext as ext

        return ext.run_walkthrough_writeup(self._require(), ref, style=style)

    # ---- Personalization ----

    def get_personalization_summary(self) -> dict:
        from services.automation import app_control_ext as ext

        return ext.get_personalization_summary()

    def list_dictionary(self) -> dict:
        from services.automation import app_control_ext as ext

        return ext.list_dictionary()

    def add_dictionary_term(
        self, term: str, *, heard: tuple[str, ...] = (), starred: bool = False
    ) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(
            partial(ext.add_dictionary_term, self._require(), term, heard=heard, starred=starred)
        )

    def bulk_add_dictionary_terms(self, items: list[dict], *, dry_run: bool = False) -> dict:
        from services.automation import app_control_ext as ext

        if dry_run:
            return ext.bulk_add_dictionary_terms(self._require(), items, dry_run=True)
        return call_on_qt(
            partial(ext.bulk_add_dictionary_terms, self._require(), items, dry_run=False)
        )

    def remove_dictionary_term(self, term_or_id: str) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.remove_dictionary_term, self._require(), term_or_id))

    def list_snippets(self) -> dict:
        from services.automation import app_control_ext as ext

        return ext.list_snippets()

    def upsert_snippet(
        self, trigger: str, text: str, *, snippet_id: str = "", formatted: bool = False
    ) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(
            partial(ext.upsert_snippet, self._require(), trigger, text, snippet_id=snippet_id, formatted=formatted)
        )

    def bulk_upsert_snippets(
        self, items: list[dict], *, dry_run: bool = False, skip_existing: bool = True
    ) -> dict:
        from services.automation import app_control_ext as ext

        if dry_run:
            return ext.bulk_upsert_snippets(
                self._require(), items, dry_run=True, skip_existing=skip_existing
            )
        return call_on_qt(
            partial(
                ext.bulk_upsert_snippets,
                self._require(),
                items,
                dry_run=False,
                skip_existing=skip_existing,
            )
        )

    def delete_snippet(self, snippet_id: str) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.delete_snippet, self._require(), snippet_id))

    def get_app_styles(self) -> dict:
        from services.automation import app_control_ext as ext

        return ext.get_app_styles()

    def set_app_style_tone(self, category: str, tone: str) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.set_app_style_tone, self._require(), category, tone))

    def set_app_style_override(self, match: str, category: str) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.set_app_style_override, self._require(), match, category))

    def remove_app_style_override(self, match: str) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.remove_app_style_override, self._require(), match))

    def list_walkthrough_styles(self) -> dict:
        from services.automation import app_control_ext as ext

        return ext.list_walkthrough_styles()

    def upsert_walkthrough_style(self, name: str, instructions: str) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.upsert_walkthrough_style, self._require(), name, instructions))

    def delete_walkthrough_style(self, style_id: str) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.delete_walkthrough_style, self._require(), style_id))

    def list_languages(self) -> dict:
        from services.automation import app_control_ext as ext

        return ext.list_languages()

    def set_active_language(self, code: str) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.set_active_language, self._require(), code))

    def set_quick_record_profile(self, profile_id: str) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.set_quick_record_profile, self._require(), profile_id))

    # ---- Learned cleanup rules ----

    def list_cleanup_rules(self) -> dict:
        from services.automation import app_control_ext as ext

        return ext.list_cleanup_rules()

    def add_cleanup_rule(self, rule: str, *, dry_run: bool = False) -> dict:
        from services.automation import app_control_ext as ext

        if dry_run:
            return ext.add_cleanup_rule(self._require(), rule, dry_run=True)
        return call_on_qt(partial(ext.add_cleanup_rule, self._require(), rule, dry_run=False))

    def update_cleanup_rule(self, index: int, rule: str) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.update_cleanup_rule, self._require(), index, rule))

    def remove_cleanup_rule(self, *, index: Optional[int] = None, rule: str = "") -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(
            partial(ext.remove_cleanup_rule, self._require(), index=index, rule=rule)
        )

    # ---- Engines, models, audio devices, remote & window ----

    def engine_status(self) -> dict:
        from services.automation import app_control_ext as ext

        return ext.engine_status(self._require())

    def list_models(self) -> dict:
        from services.automation import app_control_ext as ext

        return ext.list_models(self._require())

    def select_model(self, engine: str, *, model: str = "") -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.select_model, self._require(), engine, model=model))

    def download_model(self, model_name: str) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.download_model, self._require(), model_name))

    def list_audio_devices(self) -> dict:
        from services.automation import app_control_ext as ext

        return ext.list_audio_devices()

    def select_audio_device(self, name: str, *, hostapi: str = "") -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.select_audio_device, self._require(), name, hostapi=hostapi))

    def remote_status(self) -> dict:
        from services.automation import app_control_ext as ext

        return ext.remote_status(self._require())

    def reconnect_remote(self) -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.reconnect_remote, self._require()))

    def control_window(self, action: str, *, text: str = "", tab: str = "") -> dict:
        from services.automation import app_control_ext as ext

        return call_on_qt(partial(ext.control_window, self._require(), action, text=text, tab=tab))


def install(controller) -> None:
    """Register this app's control with ``control.set_app_control``; Qt thread."""
    set_app_control(QtAppControl(controller))
