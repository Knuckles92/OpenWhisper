"""Runs an action once its words are ready.

Contract: docs/design/automation/design.md, "services/actions/runtime.py".

``ActionRuntime.deliver`` runs on the Qt thread and returns at once. Copying,
"Send to my scripts" and the Claude Code channel finish there. Opening a
link, a webhook, a program and an agent block, so they run on the runtime's
own small pool rather than the transcription executor: the dictation's job
slot frees as soon as its history is saved, however long an agent thinks.
Their results come back to the Qt thread through one relay that lives for
the session; a worker only ever emits a token and a result. At most
``MAX_PENDING`` of them run or wait at once; more are refused, and
``cancel`` stops them all (agents and programs are killed, late results
dropped).

A run that fails leaves the words on the clipboard, so nothing said is lost.
Dictated text, links, program paths and arguments, selections, secrets and
replies are never logged.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import threading
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, Optional

from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal, pyqtSlot

from services.actions.model import (
    Action,
    ActionKind,
    AgentMode,
    Output,
    action_secret_name,
    expand_link,
    find_action,
)
from services.automation import catalog, events
from services.settings import settings_manager

logger = logging.getLogger(__name__)

#: ``EventBus.subscribers`` label of a Claude Code session's channel bridge.
CHANNEL_LISTENER_LABEL = "claude-channel"
#: Blocking runs at once; more wait their turn.
MAX_WORKERS = 4
#: Blocking runs running or waiting; beyond this a run is refused.
MAX_PENDING = 8
#: How long a webhook action waits for its receiver.
WEBHOOK_TIMEOUT_S = 5.0
#: How much of an answer a notice shows.
NOTICE_MAX_CHARS = 140
#: The fixed frame around the selection an agent action passes as context.
CONTEXT_OPEN = '<context source="OpenWhisper">'
CONTEXT_CLOSE = "</context>"

MISSING_MESSAGE = "That action no longer exists"
DISABLED_MESSAGE = "That action is turned off"
NO_LISTENER_MESSAGE = "No Claude Code session is listening"
CLOSING_MESSAGE = "OpenWhisper is closing"
BUSY_MESSAGE = "Too many actions are still running"
CANCELED_MESSAGE = "Canceled"
COPIED_INSTEAD = " — copied instead"


@dataclass(frozen=True)
class ActionResult:
    """How a run went: ``message`` is shown as it is; ``output`` is an answer's text."""

    ok: bool
    message: str
    output: Optional[str] = None


def _described(exc: BaseException) -> str:
    """An exception's type and where it was raised, never its message.

    Messages can carry the words, a link or a reply.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return type(exc).__name__
    return f"{type(exc).__name__} at {os.path.basename(frames[-1].filename)}:{frames[-1].lineno}"


# -- the Qt-thread relay --------------------------------------------------------

class _ResultRelay(QObject):
    """Brings results from the runtime's workers to the Qt thread.

    One for the session, on the application's thread. A worker emits only
    the token its run was given and the result; whoever is still waiting
    for that token gets it, and a token dropped meanwhile (the runtime
    closed) is ignored.
    """

    finished = pyqtSignal(int, object)

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._waiting: dict[int, Callable[[Any], None]] = {}
        # A slot of this object, so a queued call follows it to its thread.
        self.finished.connect(self._deliver)

    def expect(self, token: int, callback: Callable[[Any], None]) -> None:
        with self._lock:
            self._waiting[token] = callback

    def drop(self, tokens) -> None:
        with self._lock:
            for token in tokens:
                self._waiting.pop(token, None)

    @pyqtSlot(int, object)
    def _deliver(self, token: int, result: object) -> None:
        with self._lock:
            callback = self._waiting.pop(token, None)
        if callback is None:
            return
        try:
            callback(result)
        except Exception as exc:
            logger.error("An action's outcome could not be shown: %s", _described(exc))


_relay: Optional[_ResultRelay] = None
_relay_lock = threading.Lock()
_tokens = itertools.count(1)


def _result_relay() -> _ResultRelay:
    """The session's relay, created on first use and kept on the app's thread."""
    global _relay
    with _relay_lock:
        if _relay is None:
            relay = _ResultRelay()
            app = QCoreApplication.instance()
            if app is not None and relay.thread() is not app.thread():
                relay.moveToThread(app.thread())
            _relay = relay
        return _relay


# -- blocking work (worker threads; never touches the runtime or Qt) ------------

def _open_link(uri: str) -> ActionResult:
    from services.actions.launch import LaunchError, open_uri

    try:
        open_uri(uri)
    except LaunchError as exc:
        return ActionResult(False, str(exc))
    return ActionResult(True, "")


_secret_lock = threading.Lock()


def ensure_action_secret(action_id: str) -> str:
    """A webhook action's signing secret, created on first use.

    Blocking: the credential store may ask the desktop to unlock it.

    Raises:
        services.credentials.CredentialStoreError: It could not be saved.
    """
    from services import credentials
    from services.automation import signing

    name = action_secret_name(action_id)
    with _secret_lock:
        store = credentials.store()
        secret = store.get(name)
        if not secret:
            secret = signing.new_secret()
            store.set(name, secret)
    return secret


def delete_action_secret(action_id: str) -> None:
    """Forget a deleted webhook action's secret; blocking.

    Raises:
        services.credentials.CredentialStoreError: The store refused.
    """
    from services import credentials

    with _secret_lock:
        credentials.store().delete(action_secret_name(action_id))


def _post_webhook(url: str, payload: dict, secret: str):
    """POST an action's payload; ``(DeliveryRecord, reply body or None)``."""
    from services.automation import webhooks

    return webhooks.post_signed_reply(url, payload, secret, timeout_s=WEBHOOK_TIMEOUT_S)


def reply_text(body) -> Optional[str]:
    """The ``"text"`` string of a JSON reply, or None."""
    if not body:
        return None
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return None
    text = data.get("text") if isinstance(data, dict) else None
    return text if isinstance(text, str) else None


def _webhook(url: str, payload: dict, action_id: str) -> ActionResult:
    from services.credentials import CredentialStoreError

    try:
        secret = ensure_action_secret(action_id)
    except (CredentialStoreError, ValueError) as exc:
        return ActionResult(False, f"couldn't keep its signing secret ({exc})")
    record, body = _post_webhook(url, payload, secret)
    if not record.ok:
        if record.error:
            return ActionResult(False, record.error)
        if record.status:
            return ActionResult(False, f"the address answered {record.status}")
        return ActionResult(False, "the address didn't answer")
    return ActionResult(True, "", reply_text(body))


def _program_env(extra: dict) -> dict:
    from services.automation import programs

    return programs.child_env(extra)


def _program(argv: list, stdin: bytes, timeout_s: float, env: dict,
             cancel: threading.Event) -> ActionResult:
    from services.automation import programs

    code, out, error = programs.run_once(argv, stdin, timeout_s=timeout_s, env=env,
                                         cancel=cancel)
    if error:
        return ActionResult(False, error)
    if code != 0:
        return ActionResult(False, f"the program stopped with code {code}"
                            if code is not None else "the program didn't finish")
    return ActionResult(True, "", (out or b"").decode("utf-8", errors="replace").strip())


def prompt_with_context(text: str, snapshot) -> str:
    """``text`` followed by the selection and app the job started on, when known.

    The selection is UI Automation's read from the job's focus capture,
    never a copy; nothing is added when it is unknown, refused or empty.
    """
    context = getattr(snapshot, "text", None)
    if context is None or context.blocked or not context.selection_known:
        return text
    selected = context.selected or ""
    if not selected.strip():
        return text
    identity = getattr(snapshot, "identity", None)
    app = identity.name if identity is not None and identity.name else ""
    lines = [CONTEXT_OPEN]
    if app:
        lines.append(f"<app>{app}</app>")
    lines += ["<selected_text>", selected, "</selected_text>", CONTEXT_CLOSE]
    return text + "\n\n" + "\n".join(lines)


def _agent(params: dict, text: str, job, cancel: threading.Event) -> ActionResult:
    from config import config
    from services.actions import agents

    if params["include_selection"]:
        # The capture runs from the recording's start; a run from the API
        # may still be waiting for it, which a worker can afford.
        text = prompt_with_context(text, job.snapshot(timeout=config.CONTEXT_CAPTURE_DEADLINE_S))
    if params["mode"] == AgentMode.ASK:
        return agents.ask(params["agent"], text, model=params["model"], cancel=cancel)
    return agents.handoff(params["agent"], text, folder=params["folder"],
                          surface=params["surface"])


# -- the runtime ------------------------------------------------------------------

@dataclass
class _Run:
    """One run of an action, from its words to its outcome."""

    job: Any
    action: Action
    text: str
    raw_text: Optional[str] = None
    html: str = ""
    settings: dict = field(default_factory=dict)
    #: Settings' "Try it": the outcome goes to this callback, not the user.
    done: Optional[Callable[[ActionResult], None]] = None
    #: Stops an agent or a program early (``cancel``, or the app closing).
    cancel: threading.Event = field(default_factory=threading.Event)


@dataclass
class _Pending:
    """A run on the pool, until its result is back or it is canceled."""

    run: _Run
    future: Optional[Future] = None


def _output_of(action: Action) -> str:
    """Where an action's answer goes: an Output value."""
    answers = action.kind in (ActionKind.WEBHOOK, ActionKind.PROGRAM) or (
        action.kind == ActionKind.AGENT and action.param("mode") == AgentMode.ASK
    )
    output = action.param("output") if answers else Output.NONE
    return output if output in Output.ALL else Output.NONE


def _short(text: str, limit: int = NOTICE_MAX_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def _listening(count: int) -> str:
    if count <= 0:
        return NO_LISTENER_MESSAGE
    if count == 1:
        return "1 Claude Code session is listening"
    return f"{count} Claude Code sessions are listening"


class ActionRuntime:
    """Delivers an action job's text; one per controller."""

    def __init__(self, controller) -> None:
        self.controller = controller
        self._lock = threading.Lock()
        self._executor: Optional[ThreadPoolExecutor] = None
        #: Runs on the pool by relay token: running, or waiting for a worker.
        self._pending: dict[int, _Pending] = {}
        self._closed = False

    # -- entry points --------------------------------------------------------

    def deliver(self, job, text: str, *, raw_text: Optional[str], html: str) -> None:
        """Run ``job``'s action with ``text``; Qt thread, returns at once.

        Shows the outcome, and publishes ``action.ran`` (and
        ``channel.message`` for a channel action).
        """
        # A routed dictation took a clipboard snapshot for a paste that
        # will not happen.
        self.controller.ui_controller.discard_clipboard_prefetch()
        settings = settings_manager.load_all_settings()
        action = find_action(settings, job.action_id)
        run = _Run(job=job, action=action or Action(job.action_id or "", "", ""),
                   text=text, raw_text=raw_text, html=html or "", settings=settings)
        if action is None or not action.enabled:
            self._finish(run, ActionResult(
                False, MISSING_MESSAGE if action is None else DISABLED_MESSAGE))
            return
        self._start(run)

    def test(self, action, sample_text: str, done: Callable[[ActionResult], None]) -> None:
        """Try ``action`` with ``sample_text`` from Settings; ``done`` on the Qt thread.

        Runs it for real, but saves no history and routes no answer:
        ``done`` gets the answer as ``output``. A channel action only
        reports how many sessions are listening.
        """
        from services.dictation_pipeline import DictationJob, JobMode

        job = DictationJob(mode=JobMode.ACTION, action_id=action.id, source="ui")
        run = _Run(job=job, action=action, text=sample_text,
                   settings=settings_manager.load_all_settings(), done=done)
        if action.kind == ActionKind.CHANNEL:
            count = events.bus().subscribers(CHANNEL_LISTENER_LABEL)
            done(ActionResult(count > 0, _listening(count)))
            return
        self._start(run)

    def accepting(self) -> bool:
        """Whether another blocking run would be taken now; any thread.

        False once ``MAX_PENDING`` runs are running or waiting (a new one
        would be refused), and after ``close``.
        """
        with self._lock:
            return not self._closed and len(self._pending) < MAX_PENDING

    def cancel(self, *, tests_only: bool = False) -> int:
        """Cancel every run that is running or waiting; Qt thread.

        Agents and programs are stopped with what they started, and nothing
        a canceled run would have pasted, copied or shown appears.
        ``action.ran`` says ``Canceled``; a Settings "Try it" gets that
        result too.

        Args:
            tests_only: Only Settings' "Try it" runs (its page went away).

        Returns:
            How many runs were canceled.
        """
        with self._lock:
            tokens = [token for token, pending in self._pending.items()
                      if not tests_only or pending.run.done is not None]
            canceled = [self._pending.pop(token) for token in tokens]
        if not canceled:
            return 0
        if _relay is not None:
            _relay.drop(tokens)
        for pending in canceled:
            pending.run.cancel.set()
            if pending.future is not None:
                pending.future.cancel()  # still waiting: never starts
        for pending in canceled:
            run = pending.run
            self._publish_ran(run, False, CANCELED_MESSAGE, Output.NONE)
            if run.done is not None:
                run.done(ActionResult(False, CANCELED_MESSAGE))
        logger.info("Canceled %d action run(s)", len(canceled))
        return len(canceled)

    def close(self) -> None:
        """Stop work in progress at quit: cancel agents, drop late results."""
        with self._lock:
            self._closed = True
            pending, self._pending = self._pending, {}
            executor, self._executor = self._executor, None
        for item in pending.values():
            item.run.cancel.set()
        if pending and _relay is not None:
            _relay.drop(list(pending))
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    # -- running -------------------------------------------------------------

    def _start(self, run: _Run) -> None:
        action, kind = run.action, run.action.kind
        if kind == ActionKind.CLIPBOARD:
            copied = self._copy(run.text, run.html)
            self._finish(run, ActionResult(copied, "Copied" if copied else "Couldn't copy the words"))
        elif kind == ActionKind.SCRIPTS:
            # transcription.completed already carried the words to listeners.
            self._finish(run, ActionResult(True, "Sent to your scripts"))
        elif kind == ActionKind.CHANNEL:
            self._finish(run, self._send_to_channel(run))
        elif kind == ActionKind.OPEN_LINK:
            try:
                uri = expand_link(action.param("template") or "", run.text)
            except ValueError as exc:
                self._finish(run, ActionResult(False, str(exc)))
                return
            self._submit(run, partial(_open_link, uri))
        elif kind == ActionKind.WEBHOOK:
            self._submit(run, partial(
                _webhook, action.param("url") or "", self._webhook_payload(run), action.id))
        elif kind == ActionKind.PROGRAM:
            argv = [action.param("program") or "", *(action.param("args") or [])]
            # The user's environment minus OpenWhisper's own keys and tokens.
            env = _program_env({
                "OPENWHISPER_ACTION_ID": action.id,
                "OPENWHISPER_ACTION_NAME": action.name,
                "OPENWHISPER_JOB_ID": run.job.id,
            })
            self._submit(run, partial(
                _program, argv, run.text.encode("utf-8"), float(action.param("timeout_s")), env,
                run.cancel))
        elif kind == ActionKind.AGENT:
            params = {key: action.param(key) for key in (
                "agent", "mode", "surface", "folder", "include_selection", "model")}
            self._submit(run, partial(_agent, params, run.text, run.job, run.cancel))
        else:
            self._finish(run, ActionResult(False, "OpenWhisper can't run this kind of action"))

    def _submit(self, run: _Run, work: Callable[[], ActionResult]) -> None:
        """Run ``work`` on the pool and finish ``run`` with it on the Qt thread."""
        relay = _result_relay()
        token = next(_tokens)
        with self._lock:
            executor, refusal = None, ""
            if self._closed:
                refusal = CLOSING_MESSAGE
            elif len(self._pending) >= MAX_PENDING:
                refusal = BUSY_MESSAGE
            else:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=MAX_WORKERS, thread_name_prefix="action-run")
                executor = self._executor
                pending = self._pending[token] = _Pending(run)
                relay.expect(token, partial(self._finish_from_worker, token, run))
        if executor is None:
            self._finish(run, ActionResult(False, refusal))
            return
        if run.done is None:
            self.controller.ui_controller.set_status(f"{run.action.name} · working…")
        kind = run.action.kind
        # Bound here: the worker never reaches back into the runtime.
        emit, cancel = relay.finished.emit, run.cancel

        def task() -> None:
            if cancel.is_set():
                return  # canceled before a worker took it
            try:
                result = work()
            except Exception as exc:
                logger.warning("A %s action failed: %s", kind, _described(exc))
                result = ActionResult(False, "something went wrong")
            emit(token, result)

        try:
            pending.future = executor.submit(task)
        except RuntimeError:
            pass  # closed meanwhile; close() dropped the token

    def _finish_from_worker(self, token: int, run: _Run, result: ActionResult) -> None:
        with self._lock:
            self._pending.pop(token, None)
        if not result.ok and run.done is None:
            result = ActionResult(False, f"{run.action.name}: {result.message}")
        self._finish(run, result)

    def _finish(self, run: _Run, result: ActionResult) -> None:
        """Show the outcome, route an answer and publish ``action.ran``; Qt thread."""
        name = run.action.name
        if run.done is not None:
            message = result.message or f"Sent to {name}"
            message = message[:1].upper() + message[1:]
            self._publish_ran(run, result.ok, message, Output.NONE)
            run.done(ActionResult(result.ok, message, result.output))
            return
        if not result.ok:
            message, where = result.message.rstrip("."), Output.NONE
            # A copy action's failure is the copy itself.
            if run.action.kind != ActionKind.CLIPBOARD and self._copy(run.text, run.html):
                message, where = message + COPIED_INSTEAD, Output.CLIPBOARD
            self._say(message, ok=False)
            self._publish_ran(run, False, message, where)
            return
        ok, message, where = self._route(run, result)
        self._publish_ran(run, ok, message, where)

    def _route(self, run: _Run, result: ActionResult) -> tuple[bool, str, str]:
        """Put the answer where the action sends it; ``(ok, message, where it went)``."""
        name = run.action.name
        output = _output_of(run.action)
        answer = (result.output or "").strip()
        if output == Output.NONE or not answer:
            message = result.message or f"Sent to {name}"
            if output != Output.NONE:
                message = f"Sent to {name} — nothing came back"
            self._say(message, ok=True)
            return True, message, Output.NONE
        if output == Output.PASTE:
            return self._paste(run, answer)
        if output == Output.SCRATCHPAD:
            self.controller.ui_controller.show_in_scratchpad(answer)
            message = f"{name} answered in the Scratchpad"
            self._say(message, ok=True)
            return True, message, Output.SCRATCHPAD
        if output == Output.NOTICE:
            self._say(_short(answer), ok=True)
            return True, f"Showed {name}'s answer", Output.NOTICE
        if self._copy(answer):
            message = f"Copied {name}'s answer"
            self._say(message, ok=True)
            return True, message, Output.CLIPBOARD
        message = f"Couldn't copy {name}'s answer"
        self._say(message, ok=False)
        return False, message, Output.NONE

    def _paste(self, run: _Run, answer: str) -> tuple[bool, str, str]:
        """Paste ``answer`` only into the app the job started in, else copy it."""
        from services.dictation_pipeline import PasteTarget, paste_target

        name = run.action.name
        target = paste_target(run.job)
        if target == PasteTarget.SAME:
            if self.controller.transcription_runtime.paste_text_now(answer, job=run.job):
                message = f"Pasted {name}'s answer"
                self._say(message, ok=True)
                return True, message, Output.PASTE
            reason = "couldn't paste it"
        elif target == PasteTarget.CHANGED:
            reason = "the app changed"
        else:
            reason = "couldn't confirm the app"
        if self._copy(answer):
            message = f"Copied — {reason}"
            self._say(message, ok=True)
            return True, message, Output.CLIPBOARD
        message = f"Couldn't paste or copy {name}'s answer"
        self._say(message, ok=False)
        return False, message, Output.NONE

    # -- kinds that finish on the Qt thread ------------------------------------

    def _send_to_channel(self, run: _Run) -> ActionResult:
        if not events.bus().subscribers(CHANNEL_LISTENER_LABEL):
            return ActionResult(False, NO_LISTENER_MESSAGE)
        events.publish(catalog.CHANNEL_MESSAGE, catalog.channel_message(
            run.job, run.action, run.text, catalog.app_from_fields(self._fields(run))))
        return ActionResult(True, f"Sent to {run.action.name}")

    def _webhook_payload(self, run: _Run) -> dict:
        fields = self._fields(run)
        raw = run.raw_text
        return {
            "v": 1,
            "action": {"id": run.action.id, "name": run.action.name},
            "job_id": run.job.id,
            "text": run.text,
            "raw_text": raw if raw and raw != run.text else None,
            "language": fields.get("language") or "",
            "app": catalog.app_from_fields(fields),
        }

    @staticmethod
    def _fields(run: _Run) -> dict:
        """The job's app and language, from what its capture knows now."""
        from services.dictation_pipeline import history_fields

        return history_fields(run.job, None, live=True, settings=run.settings)

    # -- showing ---------------------------------------------------------------

    def _copy(self, text: str, html: str = "") -> bool:
        ui = self.controller.ui_controller
        return bool(ui.copy_to_clipboard(text, html=html) if html else ui.copy_to_clipboard(text))

    def _say(self, message: str, *, ok: bool) -> None:
        """The outcome in the status line and near the pointer."""
        ui = self.controller.ui_controller
        ui.set_status(message)
        ui.show_notice(message, done=ok)

    @staticmethod
    def _publish_ran(run: _Run, ok: bool, message: str, where: str) -> None:
        events.publish(catalog.ACTION_RAN, catalog.action_ran(
            run.job, run.action, ok=ok, message=message, output=where))
