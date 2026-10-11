"""Runs an action once its words are ready.

Contract: docs/design/automation/design.md, "services/actions/runtime.py".

``ActionRuntime.deliver`` runs on the Qt thread and returns at once. Copying,
"Send to my scripts" and the Claude Code channel finish there. Opening a
link, a webhook, a program and an agent block, so they run on the runtime's
own small pool rather than the transcription executor: the dictation's job
slot frees as soon as its history is saved, however long an agent thinks.
Their results come back to the Qt thread through one relay that lives for
the session; a worker only ever emits a token and a result. At most
``MAX_PENDING`` of them run or wait at once; more are refused. ``cancel``
drops the runs still waiting and stops agents and programs (killed with
what they started); a run it is too late to stop, one whose work already
returned included, finishes, reports what happened, and its answer goes
nowhere.

A run the user started that fails leaves the words on the clipboard, so
nothing said is lost. One a program started over the live API or MCP never
touches the clipboard or types into an app: an answer it would paste or copy
goes to the Scratchpad, and a failure is only reported (status, notice and
``action.ran``). Dictated text, links, program paths and arguments,
selections, secrets and replies are never logged.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
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
from services.automation import catalog, events, permissions
from services.settings import settings_manager

logger = logging.getLogger(__name__)

#: ``EventBus.subscribers`` label of a Claude Code session's channel bridge.
CHANNEL_LISTENER_LABEL = "claude-channel"
#: Labels of the listeners "Send to my scripts" reaches: live event streams
#: (services.automation.api), webhooks, programs and MCP Events.
SSE_LABEL, WEBHOOKS_LABEL, PROGRAMS_LABEL, MCP_EVENTS_LABEL = (
    "sse", "webhooks", "programs", "mcp-events")
#: Blocking runs at once; more wait their turn.
MAX_WORKERS = 4
#: Blocking runs running or waiting; beyond this a run is refused.
MAX_PENDING = 8
#: How long a webhook action waits for its receiver.
WEBHOOK_TIMEOUT_S = 5.0
#: How much of an answer a notice shows.
NOTICE_MAX_CHARS = 140
#: Kinds that hand their words to the user's coding agent, which acts on them.
AGENT_KINDS = (ActionKind.AGENT, ActionKind.CHANNEL)
#: The fixed frame around the selection an agent action passes as context.
CONTEXT_OPEN = '<context source="OpenWhisper">'
CONTEXT_CLOSE = "</context>"

MISSING_MESSAGE = "That action no longer exists"
DISABLED_MESSAGE = "That action is turned off"
NO_LISTENER_MESSAGE = "No Claude Code session is listening"
NO_SCRIPTS_MESSAGE = "Nothing is listening for your scripts"
CLOSING_MESSAGE = "OpenWhisper is closing"
BUSY_MESSAGE = "Too many actions are still running"
CANCELED_MESSAGE = "Canceled"
PROGRAM_WORDS_MESSAGE = "Words from a program need “Send words to my coding agents”"
COPIED_INSTEAD = " — copied instead"
FULL_ANSWER_COPIED = " — full answer copied"


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

def _open_link(uri: str, words: str) -> ActionResult:
    from services.actions.launch import CLAUDE_APP_TEXT_MAX, LaunchError, open_uri

    # The Claude app quietly cuts longer words short.
    if uri.split(":", 1)[0].lower() == "claude" and len(words) > CLAUDE_APP_TEXT_MAX:
        return ActionResult(False, "That's too long for the Claude app "
                                   f"({CLAUDE_APP_TEXT_MAX:,} characters at most)")
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
        lines.append(f"<app>{_inside_frame(app)}</app>")
    lines += ["<selected_text>", _inside_frame(selected), "</selected_text>", CONTEXT_CLOSE]
    return text + "\n\n" + "\n".join(lines)


#: The "<" of a tag naming one of the frame's own elements, open or close.
_FRAME_TAG = re.compile(r"<(?=\s*/?\s*(?:selected_text|context|app)\b)", re.IGNORECASE)


def _inside_frame(value: str) -> str:
    """``value`` with the frame's own tags made inert, so a selection or app
    name can't end the frame early and pose as the request after it."""
    return _FRAME_TAG.sub("&lt;", value)


def program_words_allowed(action, settings) -> bool:
    """Whether ``action`` may get words a program supplied or changed.

    The one rule for programs and the user's coding agents: an agent or
    Claude Code channel action hands its words to an agent that acts on
    them, so it takes a program's words (DictationJob.program_words) only
    with "Send words to my coding agents" (AGENT_PROMPTS). Delivery applies
    it to every job; the live API and MCP apply it before starting one, to
    answer with a permission error at once; and text filters leased over
    the API leave alone an action job it would refuse.
    """
    return action.kind not in AGENT_KINDS or permissions.granted(
        settings, permissions.AGENT_PROMPTS)


def _agent(params: dict, text: str, job, cancel: threading.Event) -> ActionResult:
    from config import config
    from services.actions import agents

    if params["include_selection"]:
        # Only a job the user started has the selection (begin_job). Its
        # capture ran while they spoke; a worker can afford to wait for one
        # still running.
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
    #: Set by the worker once the work has returned, before its result heads
    #: for the Qt thread: from then on it is too late to stop.
    finished: threading.Event = field(default_factory=threading.Event)
    #: Why the words aren't what the user asked for (a transform or cleanup
    #: profile that couldn't run); shown with the outcome.
    notice: str = ""


@dataclass
class _Pending:
    """A run on the pool, until its result is back or it is canceled."""

    run: _Run
    future: Optional[Future] = None
    #: Canceled once under way, too late to stop: it finishes, and only
    #: what happened is reported; its answer goes nowhere.
    canceled: bool = False


def _stoppable(run: _Run) -> bool:
    """Whether a run under way can still be stopped: a program, or an
    agent's quick answer (both killed with everything they started), whose
    work hasn't returned yet (its result is on the way)."""
    action = run.action
    return not run.finished.is_set() and (action.kind == ActionKind.PROGRAM or (
        action.kind == ActionKind.AGENT and action.param("mode") == AgentMode.ASK
    ))


def _runs(runs: list) -> str:
    """``runs`` for a message: the action's name, or how many."""
    return runs[0].action.name if len(runs) == 1 else f"{len(runs)} actions"


def _users_own(job) -> bool:
    """Whether the user started the job, rather than a program (the live API
    or MCP): only the user's runs copy to the clipboard or paste into an app."""
    return catalog.user_started(getattr(job, "source", ""))


def _output_of(run: _Run) -> str:
    """Where a run's answer goes: an Output value.

    A program's run never copies or pastes: an answer it would put on the
    clipboard or paste into the user's app goes to the Scratchpad, which
    shows it without acting on it.
    """
    action = run.action
    answers = action.kind in (ActionKind.WEBHOOK, ActionKind.PROGRAM) or (
        action.kind == ActionKind.AGENT and action.param("mode") == AgentMode.ASK
    )
    output = action.param("output") if answers else Output.NONE
    if output not in Output.ALL:
        return Output.NONE
    if output in (Output.PASTE, Output.CLIPBOARD) and not _users_own(run.job):
        return Output.SCRATCHPAD
    return output


def _short(text: str, limit: int = NOTICE_MAX_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def scripts_listening(settings) -> bool:
    """Whether anything outside OpenWhisper would act on ``action.ran`` now.

    A live event stream, an enabled webhook or program hook that takes
    ``action.ran``, or an MCP Events subscription. Qt thread; never blocks.
    """
    bus = events.bus()
    if bus.subscribers(SSE_LABEL):
        return True
    from services.automation import models

    hooks = []
    if bus.subscribers(WEBHOOKS_LABEL):
        hooks += models.load_webhooks(settings)
    if bus.subscribers(PROGRAMS_LABEL):
        hooks += models.load_programs(settings)
    if any(hook.enabled and catalog.ACTION_RAN in hook.events for hook in hooks):
        return True
    if bus.subscribers(MCP_EVENTS_LABEL):
        # Subscribed while MCP Events is on, whether or not an agent is.
        from services.agent_mcp import events_ext

        return events_ext.subscription_count() > 0
    return False


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

    def deliver(self, job, text: str, *, raw_text: Optional[str], html: str,
                notice: str = "") -> None:
        """Run ``job``'s action with ``text``; Qt thread, returns at once.

        Shows the outcome, and publishes ``action.ran`` (and
        ``channel.message`` for a channel action). Words a program supplied
        or changed reach an agent or channel action only as
        ``program_words_allowed`` says.
        """
        # A routed dictation took a clipboard snapshot for a paste that
        # will not happen.
        self.controller.ui_controller.discard_clipboard_prefetch()
        settings = settings_manager.load_all_settings()
        action = find_action(settings, job.action_id)
        run = _Run(job=job, action=action or Action(job.action_id or "", "", ""),
                   text=text, raw_text=raw_text, html=html or "", settings=settings,
                   notice=notice or "")
        if action is None or not action.enabled:
            self._finish(run, ActionResult(
                False, MISSING_MESSAGE if action is None else DISABLED_MESSAGE))
            return
        if job.program_words and not program_words_allowed(action, settings):
            # Not sent; the user's own run keeps the words on the clipboard.
            self._finish(run, ActionResult(False, PROGRAM_WORDS_MESSAGE))
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
        """Cancel the runs still waiting and stop those that can be; Qt thread.

        A waiting run never starts, and an agent's quick answer or a program
        is stopped with everything it started: ``action.ran`` says
        ``Canceled``, and a Settings "Try it" gets that result too. A run
        already under way that can't be stopped (a link opening, a webhook
        posting, a session being handed off, or any run whose work has
        already returned) finishes, and ``action.ran`` reports what really
        happened; only its answer is dropped, so nothing is pasted, copied,
        shown or put in the Scratchpad for it. Unless ``tests_only``, the
        status line and a notice say what was canceled and what was too late.

        Args:
            tests_only: Only Settings' "Try it" runs (its page went away).

        Returns:
            How many runs the cancel reached.
        """
        with self._lock:
            stopped, late = [], []
            for token, pending in list(self._pending.items()):
                if pending.canceled or (tests_only and pending.run.done is None):
                    continue
                # A future that cancels hadn't started and now never will.
                waiting = pending.future is None or pending.future.cancel()
                if waiting or _stoppable(pending.run):
                    stopped.append((token, self._pending.pop(token)))
                else:
                    pending.canceled = True
                    late.append(pending.run)
        if stopped and _relay is not None:
            _relay.drop([token for token, _pending in stopped])
        for _token, pending in stopped:
            pending.run.cancel.set()
        for _token, pending in stopped:
            run = pending.run
            self._publish_ran(run, False, CANCELED_MESSAGE, Output.NONE)
            if run.done is not None:
                run.done(ActionResult(False, CANCELED_MESSAGE))
        if stopped or late:
            logger.info("Canceled %d action run(s); %d too far along to stop",
                        len(stopped), len(late))
            if not tests_only:
                self._say_canceled([pending.run for _token, pending in stopped], late)
        return len(stopped) + len(late)

    def _say_canceled(self, stopped: list, late: list) -> None:
        """What the user's Cancel did: the runs it stopped, and those too far along."""
        parts = [f"Canceled {_runs(stopped)}"] if stopped else []
        if late:
            parts.append(f"too late to stop {_runs(late)}")
        message = " — ".join(parts)
        message = message[:1].upper() + message[1:]
        ui = self.controller.ui_controller
        ui.set_status(message)
        ui.show_notice(message)

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
        if kind == ActionKind.CLIPBOARD and not _users_own(run.job):
            # Like an answer a program's run would copy (_output_of).
            self.controller.ui_controller.show_in_scratchpad(run.text)
            self._finish(run, ActionResult(True, "Put in the Scratchpad"))
        elif kind == ActionKind.CLIPBOARD:
            copied = self._copy(run.text, run.html)
            self._finish(run, ActionResult(copied, "Copied" if copied else "Couldn't copy the words"))
        elif kind == ActionKind.SCRIPTS:
            # action.ran carries the words to whatever listens.
            if scripts_listening(run.settings):
                self._finish(run, ActionResult(True, "Sent to your scripts"))
            else:
                self._finish(run, ActionResult(False, NO_SCRIPTS_MESSAGE))
        elif kind == ActionKind.CHANNEL:
            self._finish(run, self._send_to_channel(run))
        elif kind == ActionKind.OPEN_LINK:
            try:
                uri = expand_link(action.param("template") or "", run.text)
            except ValueError as exc:
                self._finish(run, ActionResult(False, str(exc)))
                return
            self._submit(run, partial(_open_link, uri, run.text))
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
        emit, cancel, finished = relay.finished.emit, run.cancel, run.finished

        def task() -> None:
            if cancel.is_set():
                return  # stopped, or the app closed, as a worker took it
            try:
                result = work()
            except Exception as exc:
                logger.warning("A %s action failed: %s", kind, _described(exc))
                result = ActionResult(False, "something went wrong")
            finished.set()
            emit(token, result)

        try:
            pending.future = executor.submit(task)
        except RuntimeError:
            pass  # closed meanwhile; close() dropped the token

    def _finish_from_worker(self, token: int, run: _Run, result: ActionResult) -> None:
        with self._lock:
            pending = self._pending.pop(token, None)
        if not result.ok and run.done is None:
            result = ActionResult(False, f"{run.action.name}: {result.message}")
        if pending is not None and pending.canceled and run.done is None:
            # Canceled too late to stop: say what happened, use no answer.
            message = result.message.rstrip(".") if not result.ok else (
                result.message or f"Sent to {run.action.name}")
            self._publish_ran(run, result.ok, message, Output.NONE)
            return
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
            # Words the user spoke or typed are kept on the clipboard; a
            # program's are not its to overwrite the clipboard with. A copy
            # action's failure is the copy itself.
            if (run.action.kind != ActionKind.CLIPBOARD and _users_own(run.job)
                    and self._copy(run.text, run.html)):
                message, where = message + COPIED_INSTEAD, Output.CLIPBOARD
            self._say(run, message, ok=False)
            self._publish_ran(run, False, message, where)
            return
        ok, message, where = self._route(run, result)
        self._publish_ran(run, ok, message, where)

    def _route(self, run: _Run, result: ActionResult) -> tuple[bool, str, str]:
        """Put the answer where the action sends it; ``(ok, message, where it went)``."""
        name = run.action.name
        output = _output_of(run)
        answer = (result.output or "").strip()
        if output == Output.NONE or not answer:
            message = result.message or f"Sent to {name}"
            if output != Output.NONE:
                message = f"Sent to {name} — nothing came back"
            self._say(run, message, ok=True)
            return True, message, Output.NONE
        if output == Output.PASTE:
            return self._paste(run, answer)
        if output == Output.SCRATCHPAD:
            self.controller.ui_controller.show_in_scratchpad(answer)
            message = f"{name} answered in the Scratchpad"
            self._say(run, message, ok=True)
            return True, message, Output.SCRATCHPAD
        if output == Output.NOTICE:
            shown, message = _short(answer), f"Showed {name}'s answer"
            # A notice holds only the start of a long answer; the clipboard
            # keeps all of it, for the user's own runs.
            if (len(" ".join(answer.split())) > NOTICE_MAX_CHARS and _users_own(run.job)
                    and self._copy(answer)):
                shown, message = shown + FULL_ANSWER_COPIED, message + FULL_ANSWER_COPIED
            self._say(run, shown, ok=True)
            return True, message, Output.NOTICE
        if self._copy(answer):
            message = f"Copied {name}'s answer"
            self._say(run, message, ok=True)
            return True, message, Output.CLIPBOARD
        message = f"Couldn't copy {name}'s answer"
        self._say(run, message, ok=False)
        return False, message, Output.NONE

    def _paste(self, run: _Run, answer: str) -> tuple[bool, str, str]:
        """Paste ``answer`` only into the app the job started in, else copy it.

        Only the user's runs get here (``_output_of``).
        """
        from services.dictation_pipeline import PasteTarget, paste_target

        name = run.action.name
        target = paste_target(run.job)
        if target == PasteTarget.SAME:
            if self.controller.transcription_runtime.paste_text_now(answer, job=run.job):
                message = f"Pasted {name}'s answer"
                self._say(run, message, ok=True)
                return True, message, Output.PASTE
            reason = "couldn't paste it"
        elif target == PasteTarget.CHANGED:
            reason = "the app changed"
        else:
            reason = "couldn't confirm the app"
        if self._copy(answer):
            message = f"Copied — {reason}"
            self._say(run, message, ok=True)
            return True, message, Output.CLIPBOARD
        message = f"Couldn't paste or copy {name}'s answer"
        self._say(run, message, ok=False)
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

    def _say(self, run: _Run, message: str, *, ok: bool) -> None:
        """The outcome in the status line and near the pointer, with the run's notice."""
        if run.notice:
            message = f"{message} — {run.notice}"
        ui = self.controller.ui_controller
        ui.set_status(message)
        ui.show_notice(message, done=ok)

    @staticmethod
    def _publish_ran(run: _Run, ok: bool, message: str, where: str) -> None:
        # "Send to my scripts" exists to hand its listeners the words.
        text = run.text if run.action.kind == ActionKind.SCRIPTS else None
        events.publish(catalog.ACTION_RAN, catalog.action_ran(
            run.job, run.action, ok=ok, message=message, output=where, text=text))
