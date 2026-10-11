"""ActionRuntime: every kind and output, with fakes for the blocking work."""

import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QCoreApplication

from services import credentials, focus_context
from services.actions import agents, launch, runtime
from services.actions.launch import LaunchError
from services.actions.model import Action, ActionKind, action_secret_name
from services.actions.runtime import ActionResult, ActionRuntime
from services.automation import catalog, events, programs, signing, webhooks
from services.automation.events import EventBus
from services.automation.webhooks import DeliveryRecord
from services.dictation_pipeline import DictationJob, JobMode
from services.focus_context import (
    AppIdentity,
    ContextCaptureService,
    FocusSnapshot,
    TextContext,
)
from services.recognition_context import RecognitionContext
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings

OUTLOOK = AppIdentity("outlook.exe", "Outlook", pid=10, window="0x1")
SLACK = AppIdentity("slack.exe", "Slack", pid=11, window="0x2")


class FakeUI:
    def __init__(self):
        self.statuses, self.notices, self.copied, self.scratchpad = [], [], [], []
        self.discarded = 0
        self.copy_ok = True
        self.threads = []

    def discard_clipboard_prefetch(self):
        self.discarded += 1

    def set_status(self, text):
        self.statuses.append(text)

    def show_notice(self, text, *, done=False):
        self.threads.append(threading.current_thread())
        self.notices.append((text, done))

    def copy_to_clipboard(self, text, html=""):
        self.copied.append((text, html))
        return self.copy_ok

    def show_in_scratchpad(self, text):
        self.scratchpad.append(text)


class FakeTranscription:
    def __init__(self):
        self.pasted = []
        self.paste_ok = True

    def paste_text_now(self, text, *, job=None):
        self.pasted.append((text, job))
        return self.paste_ok


class FakeService(ContextCaptureService):
    def __init__(self, current=None):
        self.current = current

    def request(self, *, include_text, include_selection=False):
        future: Future = Future()
        future.set_result(FocusSnapshot(identity=self.current))
        return future

    def current_identity(self):
        return self.current

    def reread(self, identity, callback):
        callback(None)

    def shutdown(self):
        pass


def _resolved(value) -> Future:
    future: Future = Future()
    future.set_result(value)
    return future


def _job(action_id, *, identity=OUTLOOK, text=None, language="en"):
    snapshot = FocusSnapshot(identity=identity, text=text)
    return DictationJob(
        mode=JobMode.ACTION, action_id=action_id, source="hotkey",
        focus=_resolved(snapshot), recognition=RecognitionContext(language=language),
    )


@pytest.fixture
def h(monkeypatch):
    ui = FakeUI()
    settings = InMemorySettings({SettingsKey.ACTIONS: []})
    monkeypatch.setattr(runtime, "settings_manager", settings)
    bus = EventBus()
    previous = events.set_bus(bus)
    service = FakeService(current=OUTLOOK)
    focus_context.set_service(service)
    controller = SimpleNamespace(ui_controller=ui, transcription_runtime=FakeTranscription())
    rt = ActionRuntime(controller)

    def add(action):
        settings.values[SettingsKey.ACTIONS].append(action.to_dict())
        return action

    try:
        yield SimpleNamespace(ui=ui, settings=settings, bus=bus, rt=rt, add=add,
                              controller=controller, service=service)
    finally:
        rt.close()
        events.set_bus(previous)
        bus.close()


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out")


def _ran(h):
    return [e.data for e in h.bus.recent(types=[catalog.ACTION_RAN])]


def _finished(h, count=1):
    _wait(lambda: len(_ran(h)) >= count)
    return _ran(h)[-1]


# -- lookup ------------------------------------------------------------------


def test_a_missing_action_copies_the_words_and_says_so(h):
    h.rt.deliver(_job("gone"), "hello there", raw_text=None, html="<b>hello</b> there")

    assert h.ui.discarded == 1
    assert h.ui.copied == [("hello there", "<b>hello</b> there")]
    assert h.ui.notices == [("That action no longer exists — copied instead", False)]
    assert h.ui.statuses[-1] == "That action no longer exists — copied instead"
    ran = _ran(h)[-1]
    assert ran["action_id"] == "gone" and ran["ok"] is False and ran["output"] == "clipboard"


def test_a_disabled_action_is_not_run(h, monkeypatch):
    h.add(Action("a1", "Ask Claude", ActionKind.OPEN_LINK,
                 {"template": "claude://claude.ai/new?q={text}"}, enabled=False))
    monkeypatch.setattr(launch, "open_uri", lambda uri: pytest.fail("opened"))

    h.rt.deliver(_job("a1"), "hi", raw_text=None, html="")

    assert h.ui.notices == [("That action is turned off — copied instead", False)]


def test_the_action_is_read_fresh_for_every_run(h):
    h.add(Action("a1", "Copy it", ActionKind.CLIPBOARD))
    h.rt.deliver(_job("a1"), "one", raw_text=None, html="")
    h.settings.values[SettingsKey.ACTIONS] = [
        Action("a1", "Tell scripts", ActionKind.SCRIPTS).to_dict()]
    h.bus.subscribe(lambda event: None, label="sse")  # a script follows the events

    h.rt.deliver(_job("a1"), "two", raw_text=None, html="")

    assert h.ui.copied == [("one", "")]
    assert h.ui.notices[-1] == ("Sent to your scripts", True)


# -- kinds that finish at once ------------------------------------------------


def test_clipboard_copies_the_words_with_their_formatting(h):
    h.add(Action("c1", "Copy only", ActionKind.CLIPBOARD))

    h.rt.deliver(_job("c1"), "hello", raw_text=None, html="<i>hello</i>")

    assert h.ui.copied == [("hello", "<i>hello</i>")]
    assert h.ui.notices == [("Copied", True)]
    ran = _ran(h)[-1]
    assert ran == {"job_id": ran["job_id"], "source": "hotkey", "action_id": "c1", "name": "Copy only",
                   "kind": "clipboard", "ok": True, "message": "Copied", "output": "none"}


def test_a_failed_copy_is_not_retried_as_a_fallback(h):
    h.add(Action("c1", "Copy only", ActionKind.CLIPBOARD))
    h.ui.copy_ok = False

    h.rt.deliver(_job("c1"), "hello", raw_text=None, html="")

    assert len(h.ui.copied) == 1
    assert h.ui.notices == [("Couldn't copy the words", False)]


def test_scripts_only_announce_the_run(h):
    h.add(Action("s1", "Send to my scripts", ActionKind.SCRIPTS))
    h.bus.subscribe(lambda event: None, label="sse")  # a script follows the events

    h.rt.deliver(_job("s1"), "hello", raw_text=None, html="")

    assert h.ui.copied == []
    assert h.ui.notices == [("Sent to your scripts", True)]
    assert _ran(h)[-1]["ok"] is True


def test_channel_with_no_listener_copies_instead(h):
    h.add(Action("ch", "Tell my session", ActionKind.CHANNEL))

    h.rt.deliver(_job("ch"), "fix the build", raw_text=None, html="")

    assert h.ui.copied == [("fix the build", "")]
    assert h.ui.notices == [("No Claude Code session is listening — copied instead", False)]
    assert h.bus.recent(types=[catalog.CHANNEL_MESSAGE]) == []


def test_channel_publishes_the_words_to_listening_sessions(h):
    h.add(Action("ch", "Tell my session", ActionKind.CHANNEL))
    h.bus.subscribe(lambda event: None, types=[catalog.CHANNEL_MESSAGE], label="claude-channel")
    job = _job("ch")

    h.rt.deliver(job, "fix the build", raw_text=None, html="")

    (message,) = h.bus.recent(types=[catalog.CHANNEL_MESSAGE])
    assert message.data == {
        "job_id": job.id, "source": "hotkey", "origin": "dictated", "action_id": "ch",
        "text": "fix the build",
        "app": {"id": "outlook.exe", "name": "Outlook", "category": message.data["app"]["category"]},
    }
    assert h.ui.copied == []
    assert h.ui.notices == [("Sent to Tell my session", True)]
    assert _ran(h)[-1]["ok"] is True


def test_channel_app_is_null_while_the_capture_is_still_running(h):
    h.add(Action("ch", "Tell my session", ActionKind.CHANNEL))
    h.bus.subscribe(lambda event: None, label="claude-channel")
    job = DictationJob(mode=JobMode.ACTION, action_id="ch", source="hotkey", focus=Future())

    h.rt.deliver(job, "words", raw_text=None, html="")

    assert h.bus.recent(types=[catalog.CHANNEL_MESSAGE])[0].data["app"] is None


# -- open_link ------------------------------------------------------------------


def test_open_link_opens_the_expanded_link_on_a_worker(h, monkeypatch):
    h.add(Action("l1", "Ask Claude", ActionKind.OPEN_LINK,
                 {"template": "claude://claude.ai/new?q={text}"}))
    opened = []
    monkeypatch.setattr(launch, "open_uri",
                        lambda uri: opened.append((uri, threading.current_thread().name)))

    h.rt.deliver(_job("l1"), "what's 2+2?", raw_text=None, html="")
    assert h.ui.statuses == ["Ask Claude · working…"]
    ran = _finished(h)

    assert opened == [("claude://claude.ai/new?q=what%27s%202%2B2%3F", opened[0][1])]
    assert opened[0][1].startswith("action-run")
    assert h.ui.notices == [("Sent to Ask Claude", True)]
    assert h.ui.threads == [threading.main_thread()]
    assert ran["ok"] is True and ran["kind"] == "open_link"


def test_a_link_nothing_opens_is_copied_with_the_reason(h, monkeypatch):
    h.add(Action("l1", "Ask Claude", ActionKind.OPEN_LINK,
                 {"template": "claude://claude.ai/new?q={text}"}))

    def refuse(uri):
        raise LaunchError("No app on this computer opens that link.")

    monkeypatch.setattr(launch, "open_uri", refuse)

    h.rt.deliver(_job("l1"), "hello", raw_text=None, html="")
    ran = _finished(h)

    message = "Ask Claude: No app on this computer opens that link — copied instead"
    assert h.ui.notices == [(message, False)]
    assert h.ui.copied == [("hello", "")]
    assert ran["ok"] is False and ran["message"] == message and ran["output"] == "clipboard"


def test_a_refused_link_scheme_never_reaches_a_worker(h, monkeypatch):
    # Saved before the scheme was refused, or edited by hand.
    h.add(Action("l1", "Bad link", ActionKind.OPEN_LINK, {"template": "javascript:{text}"}))
    monkeypatch.setattr(launch, "open_uri", lambda uri: pytest.fail("opened"))

    h.rt.deliver(_job("l1"), "hello", raw_text=None, html="")

    assert h.ui.notices == [("Links that start with javascript: can't be used — copied instead",
                             False)]


# -- webhook ----------------------------------------------------------------------


def test_webhook_posts_a_signed_payload_with_a_secret_kept_in_the_credential_store(
        h, monkeypatch):
    h.add(Action("w1", "Home Assistant", ActionKind.WEBHOOK,
                 {"url": "http://127.0.0.1:8123/hook"}))
    posts = []

    def post_signed_reply(url, payload, secret, *, timeout_s=5.0):
        posts.append((url, payload, secret, timeout_s))
        return DeliveryRecord("", "", "", True, 204, 3), b""

    monkeypatch.setattr(webhooks, "post_signed_reply", post_signed_reply)
    job = _job("w1", language="de")

    h.rt.deliver(job, "Licht an", raw_text="licht an", html="")
    _finished(h)
    h.rt.deliver(_job("w1"), "Licht aus", raw_text="Licht aus", html="")
    _finished(h, 2)

    (url, payload, secret, timeout_s), second = posts
    assert url == "http://127.0.0.1:8123/hook" and timeout_s == 5.0
    assert payload == {
        "v": 1,
        "action": {"id": "w1", "name": "Home Assistant"},
        "job_id": job.id,
        "text": "Licht an",
        "raw_text": "licht an",
        "language": "de",
        "app": {"id": "outlook.exe", "name": "Outlook", "category": payload["app"]["category"]},
    }
    assert second[1]["raw_text"] is None
    assert secret.startswith("whsec_") and second[2] == secret
    assert credentials.store().get(action_secret_name("w1")) == secret
    assert signing.verify(secret, signing.headers(secret, "m", b"{}"), b"{}")
    assert h.ui.notices[-1] == ("Sent to Home Assistant", True)


@pytest.mark.parametrize("output, where", [
    ("scratchpad", "scratchpad"), ("clipboard", "clipboard"), ("notice", "notice"),
])
def test_a_webhook_reply_goes_to_the_output(h, monkeypatch, output, where):
    h.add(Action("w1", "Translate", ActionKind.WEBHOOK,
                 {"url": "http://127.0.0.1:9/hook", "output": output}))
    monkeypatch.setattr(runtime, "_post_webhook", lambda url, payload, secret: (
        DeliveryRecord("", "", "", True, 200, 3), b'{"text": "Hallo Welt"}'))

    h.rt.deliver(_job("w1"), "hello world", raw_text=None, html="")
    ran = _finished(h)

    assert ran["ok"] is True and ran["output"] == where
    if output == "scratchpad":
        assert h.ui.scratchpad == ["Hallo Welt"]
        assert h.ui.notices == [("Translate answered in the Scratchpad", True)]
    elif output == "clipboard":
        assert h.ui.copied == [("Hallo Welt", "")]
        assert h.ui.notices == [("Copied Translate's answer", True)]
    else:
        assert h.ui.notices == [("Hallo Welt", True)]
        assert ran["message"] == "Showed Translate's answer"


def test_a_webhook_reply_without_text_is_not_routed(h, monkeypatch):
    h.add(Action("w1", "Translate", ActionKind.WEBHOOK,
                 {"url": "http://127.0.0.1:9/hook", "output": "scratchpad"}))
    monkeypatch.setattr(runtime, "_post_webhook", lambda url, payload, secret: (
        DeliveryRecord("", "", "", True, 200, 3), b'{"ok": true}'))

    h.rt.deliver(_job("w1"), "hello", raw_text=None, html="")
    _finished(h)

    assert h.ui.scratchpad == []
    assert h.ui.notices == [("Sent to Translate — nothing came back", True)]


def test_a_failed_webhook_copies_the_words(h, monkeypatch):
    h.add(Action("w1", "Translate", ActionKind.WEBHOOK, {"url": "http://127.0.0.1:9/hook"}))
    monkeypatch.setattr(webhooks, "post_signed_reply", lambda *a, **k: (DeliveryRecord(
        "", "", "", False, 500, 3), None))

    h.rt.deliver(_job("w1"), "hello", raw_text=None, html="")
    ran = _finished(h)

    assert h.ui.notices == [("Translate: the address answered 500 — copied instead", False)]
    assert ran["ok"] is False and h.ui.copied == [("hello", "")]


def test_a_webhook_whose_secret_cannot_be_kept_is_not_sent(h, monkeypatch):
    h.add(Action("w1", "Translate", ActionKind.WEBHOOK, {"url": "http://127.0.0.1:9/hook"}))

    def locked_backend():
        raise RuntimeError("locked")

    credentials.set_store(credentials.CredentialStore(backend_factory=locked_backend))
    monkeypatch.setattr(webhooks, "post_signed_reply",
                        lambda *a, **k: pytest.fail("sent unsigned"))

    h.rt.deliver(_job("w1"), "hello", raw_text=None, html="")
    ran = _finished(h)

    assert ran["ok"] is False
    assert h.ui.notices[0][0].startswith("Translate: couldn't keep its signing secret (")


def test_an_action_secret_is_made_once_and_can_be_forgotten():
    secret = runtime.ensure_action_secret("w1")

    assert runtime.ensure_action_secret("w1") == secret
    assert runtime.ensure_action_secret("w2") != secret
    runtime.delete_action_secret("w1")
    assert credentials.store().get(action_secret_name("w1")) is None
    assert runtime.ensure_action_secret("w1") != secret


@pytest.mark.parametrize("body, text", [
    (b'{"text": "hi"}', "hi"), (b'{"text": 3}', None), (b"[1]", None), (b"not json", None),
    (b"", None), (None, None), ('{"text": "str"}', "str"),
])
def test_reply_text(body, text):
    assert runtime.reply_text(body) == text


# -- program ----------------------------------------------------------------------


def test_program_gets_the_words_on_stdin_and_its_output_is_routed(h, monkeypatch):
    h.add(Action("p1", "Shout", ActionKind.PROGRAM, {
        "program": "C:\\tools\\shout.exe", "args": ["--loud", "two words"],
        "output": "clipboard", "timeout_s": 12,
    }))
    runs = []

    def run_once(argv, stdin, *, timeout_s, cwd="", env=None, cancel=None):
        runs.append((argv, stdin, timeout_s, env, threading.current_thread().name))
        return 0, "HÉLLO\r\n".encode(), ""

    monkeypatch.setattr(programs, "run_once", run_once)
    job = _job("p1")

    h.rt.deliver(job, "héllo", raw_text=None, html="")
    ran = _finished(h)

    ((argv, stdin, timeout_s, env, thread),) = runs
    assert argv == ["C:\\tools\\shout.exe", "--loud", "two words"]
    assert stdin == "héllo".encode() and timeout_s == 12.0
    assert (env["OPENWHISPER_ACTION_ID"], env["OPENWHISPER_ACTION_NAME"],
            env["OPENWHISPER_JOB_ID"]) == ("p1", "Shout", job.id)
    assert "PATH" in env or "Path" in env
    assert thread.startswith("action-run")
    assert h.ui.copied == [("HÉLLO", "")]
    assert ran["output"] == "clipboard"


@pytest.mark.parametrize("result, reason", [
    ((2, b"", ""), "the program stopped with code 2"),
    ((None, b"", "The program took longer than 30 s."), "The program took longer than 30 s."),
    ((None, b"", ""), "the program didn't finish"),
])
def test_a_failed_program_copies_the_words(h, monkeypatch, result, reason):
    h.add(Action("p1", "Shout", ActionKind.PROGRAM, {"program": "C:\\shout.exe"}))
    monkeypatch.setattr(programs, "run_once", lambda *a, **k: result)

    h.rt.deliver(_job("p1"), "hello", raw_text=None, html="")
    ran = _finished(h)

    message = f"Shout: {reason.rstrip('.')} — copied instead"
    assert h.ui.notices == [(message, False)]
    assert ran["message"] == message


def test_program_output_none_ignores_stdout(h, monkeypatch):
    h.add(Action("p1", "Log it", ActionKind.PROGRAM, {"program": "C:\\log.exe"}))
    monkeypatch.setattr(programs, "run_once", lambda *a, **k: (0, b"lots of output", ""))

    h.rt.deliver(_job("p1"), "hello", raw_text=None, html="")
    _finished(h)

    assert h.ui.copied == [] and h.ui.scratchpad == []
    assert h.ui.notices == [("Sent to Log it", True)]


# -- agent ------------------------------------------------------------------------


def test_agent_handoff_passes_folder_and_surface(h, monkeypatch):
    h.add(Action("agent", "Agent hotkey", ActionKind.AGENT, {
        "agent": "codex", "mode": "handoff", "surface": "terminal", "folder": "D:\\work"}))
    calls = []

    def handoff(agent_id, text, *, folder="", surface="auto"):
        calls.append((agent_id, text, folder, surface))
        return ActionResult(True, "Opened Codex in a terminal")

    monkeypatch.setattr(agents, "handoff", handoff)

    h.rt.deliver(_job("agent"), "add a test", raw_text=None, html="")
    _finished(h)

    assert calls == [("codex", "add a test", "D:\\work", "terminal")]
    assert h.ui.statuses[0] == "Agent hotkey · working…"
    assert h.ui.notices == [("Opened Codex in a terminal", True)]


def test_agent_ask_routes_the_answer_to_the_scratchpad_by_default(h, monkeypatch):
    h.add(Action("q1", "Quick answer", ActionKind.AGENT, {
        "agent": "claude_code", "mode": "ask", "model": "haiku"}))
    asks = []

    def ask(agent_id, text, *, model="", timeout_s=120.0, cancel=None):
        asks.append((agent_id, text, model, cancel))
        return ActionResult(True, "", "Forty-two.")

    monkeypatch.setattr(agents, "ask", ask)

    h.rt.deliver(_job("q1"), "what is six times seven", raw_text=None, html="")
    ran = _finished(h)

    ((agent_id, text, model, cancel),) = asks
    assert (agent_id, text, model) == ("claude_code", "what is six times seven", "haiku")
    assert isinstance(cancel, threading.Event) and not cancel.is_set()
    assert h.ui.scratchpad == ["Forty-two."]
    assert ran["output"] == "scratchpad"


def test_a_failed_agent_copies_the_words(h, monkeypatch):
    h.add(Action("q1", "Quick answer", ActionKind.AGENT, {"mode": "ask"}))
    monkeypatch.setattr(agents, "ask", lambda *a, **k: ActionResult(
        False, "Claude Code isn't installed."))

    h.rt.deliver(_job("q1"), "hello", raw_text=None, html="")
    _finished(h)

    assert h.ui.notices == [("Quick answer: Claude Code isn't installed — copied instead", False)]


def test_agent_context_adds_the_known_selection_and_app(h, monkeypatch):
    h.add(Action("q1", "Explain", ActionKind.AGENT, {"mode": "ask", "include_selection": True}))
    prompts = []
    monkeypatch.setattr(agents, "ask", lambda agent_id, text, **k: (
        prompts.append(text) or ActionResult(True, "", "ok")))
    selection = TextContext(selected="def f(): pass", selection_known=True, source="uia")

    h.rt.deliver(_job("q1", text=selection), "what does this do", raw_text=None, html="")
    _finished(h)

    assert prompts == [
        "what does this do\n\n"
        '<context source="OpenWhisper">\n'
        "<app>Outlook</app>\n"
        "<selected_text>\n"
        "def f(): pass\n"
        "</selected_text>\n"
        "</context>"
    ]


@pytest.mark.parametrize("context", [
    None,
    TextContext(selected="", selection_known=True),
    TextContext(selected="secret", selection_known=False),
    TextContext(selected="", selection_known=False, blocked=True, source="excluded"),
])
def test_agent_context_adds_nothing_when_the_selection_is_unknown_or_empty(context):
    snapshot = FocusSnapshot(identity=OUTLOOK, text=context)
    assert runtime.prompt_with_context("words", snapshot) == "words"
    assert runtime.prompt_with_context("words", None) == "words"


def test_agent_context_without_the_app_name(h):
    snapshot = FocusSnapshot(identity=None,
                             text=TextContext(selected="x", selection_known=True))
    assert runtime.prompt_with_context("q", snapshot) == (
        'q\n\n<context source="OpenWhisper">\n<selected_text>\nx\n</selected_text>\n</context>')


def test_agent_context_is_read_without_selection_unless_asked(h, monkeypatch):
    h.add(Action("q1", "Explain", ActionKind.AGENT, {"mode": "handoff"}))
    prompts = []
    monkeypatch.setattr(agents, "handoff", lambda agent_id, text, **k: (
        prompts.append(text) or ActionResult(True, "")))
    selection = TextContext(selected="code", selection_known=True)

    h.rt.deliver(_job("q1", text=selection), "explain", raw_text=None, html="")
    _finished(h)

    assert prompts == ["explain"]
    assert h.ui.notices == [("Sent to Explain", True)]


# -- paste output --------------------------------------------------------------------


def _answering(h, monkeypatch, output="paste"):
    h.add(Action("p1", "Fix grammar", ActionKind.PROGRAM,
                 {"program": "C:\\fix.exe", "output": output}))
    monkeypatch.setattr(programs, "run_once", lambda *a, **k: (0, b"Fixed words.", ""))


def test_paste_lands_only_in_the_app_the_job_started_in(h, monkeypatch):
    _answering(h, monkeypatch)
    job = _job("p1")

    h.rt.deliver(job, "fix words", raw_text=None, html="")
    ran = _finished(h)

    assert h.controller.transcription_runtime.pasted == [("Fixed words.", job)]
    assert h.ui.copied == []
    assert h.ui.notices == [("Pasted Fix grammar's answer", True)]
    assert ran["output"] == "paste"


@pytest.mark.parametrize("current, message", [
    (SLACK, "Copied — the app changed"),
    (None, "Copied — couldn't confirm the app"),
])
def test_paste_into_another_app_is_copied_instead(h, monkeypatch, current, message):
    _answering(h, monkeypatch)
    h.service.current = current

    h.rt.deliver(_job("p1"), "fix words", raw_text=None, html="")
    ran = _finished(h)

    assert h.controller.transcription_runtime.pasted == []
    assert h.ui.copied == [("Fixed words.", "")]
    assert h.ui.notices == [(message, True)]
    assert ran["output"] == "clipboard"


def test_a_refused_paste_is_copied_instead(h, monkeypatch):
    _answering(h, monkeypatch)
    h.controller.transcription_runtime.paste_ok = False

    h.rt.deliver(_job("p1"), "fix words", raw_text=None, html="")
    _finished(h)

    assert h.ui.copied == [("Fixed words.", "")]
    assert h.ui.notices == [("Copied — couldn't paste it", True)]


def test_a_job_without_a_start_app_never_pastes(h, monkeypatch):
    _answering(h, monkeypatch)

    h.rt.deliver(DictationJob(mode=JobMode.ACTION, action_id="p1", source="hotkey"), "x",
                 raw_text=None, html="")
    _finished(h)

    assert h.controller.transcription_runtime.pasted == []
    assert h.ui.notices == [("Copied — couldn't confirm the app", True)]


def test_a_long_answer_is_shortened_for_a_notice(h, monkeypatch):
    h.add(Action("p1", "Explain", ActionKind.PROGRAM,
                 {"program": "C:\\x.exe", "output": "notice"}))
    answer = "word " * 100
    monkeypatch.setattr(programs, "run_once", lambda *a, **k: (0, answer.encode(), ""))

    h.rt.deliver(_job("p1"), "x", raw_text=None, html="")
    _finished(h)

    (shown, done), = h.ui.notices
    start = shown.removesuffix(runtime.FULL_ANSWER_COPIED)  # all of it is on the clipboard
    assert len(start) == runtime.NOTICE_MAX_CHARS and start.endswith("…") and done


# -- Settings "Try it" ------------------------------------------------------------------


def test_trying_an_action_reports_to_settings_only(h, monkeypatch):
    action = Action("draft", "Shout", ActionKind.PROGRAM,
                    {"program": "C:\\shout.exe", "output": "paste"})
    monkeypatch.setattr(programs, "run_once", lambda *a, **k: (0, b"HELLO\n", ""))
    results = []

    h.rt.test(action, "hello", lambda result: results.append(
        (result, threading.current_thread())))
    _wait(lambda: results)

    ((result, thread),) = results
    assert result == ActionResult(True, "Sent to Shout", "HELLO")
    assert thread is threading.main_thread()
    assert h.ui.notices == [] and h.ui.statuses == [] and h.ui.copied == []
    assert h.controller.transcription_runtime.pasted == []
    assert _ran(h)[-1]["action_id"] == "draft"


def test_trying_a_failing_action_does_not_copy(h, monkeypatch):
    action = Action("draft", "Open", ActionKind.OPEN_LINK, {"template": "x-none://{text}"})

    def refuse(uri):
        raise LaunchError("No app on this computer opens that link.")

    monkeypatch.setattr(launch, "open_uri", refuse)
    results = []

    h.rt.test(action, "hello", results.append)
    _wait(lambda: results)

    assert results == [ActionResult(False, "No app on this computer opens that link.")]
    assert h.ui.copied == [] and h.ui.notices == []


@pytest.mark.parametrize("listeners, result", [
    (0, ActionResult(False, "No Claude Code session is listening")),
    (1, ActionResult(True, "1 Claude Code session is listening")),
    (2, ActionResult(True, "2 Claude Code sessions are listening")),
])
def test_trying_a_channel_action_counts_listeners_without_sending(h, listeners, result):
    for _ in range(listeners):
        h.bus.subscribe(lambda event: None, label="claude-channel")
    results = []

    h.rt.test(Action("ch", "Session", ActionKind.CHANNEL), "hello", results.append)

    assert results == [result]
    assert h.bus.recent(types=[catalog.CHANNEL_MESSAGE, catalog.ACTION_RAN]) == []


def test_trying_a_webhook_returns_its_reply(h, monkeypatch):
    monkeypatch.setattr(runtime, "_post_webhook", lambda url, payload, secret: (
        DeliveryRecord("", "", "", True, 200, 3), b'{"text": "pong"}'))
    results = []
    action = Action("w9", "Ping", ActionKind.WEBHOOK,
                    {"url": "http://127.0.0.1:9/", "output": "none"})

    h.rt.test(action, "ping", results.append)
    _wait(lambda: results)

    assert results == [ActionResult(True, "Sent to Ping", "pong")]


# -- privacy ------------------------------------------------------------------------------


def test_words_links_arguments_and_replies_are_never_logged(h, monkeypatch, caplog):
    caplog.set_level("DEBUG")
    h.add(Action("l1", "Link", ActionKind.OPEN_LINK, {"template": "https://example.com/?q={text}"}))
    h.add(Action("p1", "Prog", ActionKind.PROGRAM,
                 {"program": "C:\\secret-tool.exe", "args": ["--token-arg"]}))
    h.add(Action("w1", "Hook", ActionKind.WEBHOOK,
                 {"url": "http://127.0.0.1:9/private-path", "output": "notice"}))

    def boom(uri):
        raise RuntimeError(uri)

    monkeypatch.setattr(launch, "open_uri", boom)
    monkeypatch.setattr(programs, "run_once", lambda argv, *a, **k: (1, b"private-reply", ""))
    monkeypatch.setattr(runtime, "_post_webhook", lambda url, payload, secret: (
        DeliveryRecord("", "", "", True, 200, 3), b'{"text": "private-answer"}'))

    for action_id in ("l1", "p1", "w1"):
        h.rt.deliver(_job(action_id), "my private words", raw_text=None, html="")
    _finished(h, 3)

    assert "A open_link action failed: RuntimeError at test_automation_actions_runtime.py:" in (
        caplog.text)
    for secret in ("my private words", "my%20private", "example.com", "secret-tool",
                   "--token-arg", "private-path", "private-reply", "private-answer"):
        assert secret not in caplog.text


def test_a_program_action_never_sees_openwhisper_keys(h, monkeypatch):
    from services import credentials

    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-openwhisper-dotenv")
    monkeypatch.setenv("OPENWHISPER_LLM_API_KEY", "internal")
    monkeypatch.setenv("USER_OWN_VAR", "kept")
    monkeypatch.setattr(credentials, "dotenv_injected_names", lambda: {"OPENAI_API_KEY"})
    h.add(Action("p2", "Env", ActionKind.PROGRAM, {"program": "C:\tools\env.exe"}))
    seen = []
    monkeypatch.setattr(programs, "run_once",
                        lambda argv, stdin, *, timeout_s, cwd="", env=None, cancel=None:
                        seen.append(env) or (0, b"", ""))

    h.rt.deliver(_job("p2"), "hi", raw_text=None, html="")
    _finished(h)

    env, = seen
    assert "OPENAI_API_KEY" not in env and "OPENWHISPER_LLM_API_KEY" not in env
    assert env["USER_OWN_VAR"] == "kept" and env["OPENWHISPER_ACTION_ID"] == "p2"
