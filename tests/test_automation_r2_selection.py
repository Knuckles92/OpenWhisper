"""A program-run agent action never gets the user's selected text.

Review round 2, N4. The text readers filled ``selected`` whenever text near
the cursor was read, even for a job that never asked for the selection, so
an agent action a program ran (``run_action_text(source="api")``) still put
``<selected_text>`` in the agent's prompt. Now a reader returns the selection
only when asked, and only a job the user started asks (begin_job).
"""

from __future__ import annotations

import contextlib
import sys
import threading
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from services import focus_context
from services.actions import agents
from services.actions import runtime as actions_runtime
from services.actions.runtime import ActionResult
from services.dictation_pipeline import JobMode, begin_job
from services.focus_context import AppIdentity, ContextCaptureService, FocusSnapshot, TextContext
from services.focus_context import _mac
from services.focus_context._capture import CaptureService
from services.settings import SettingsKey
from tests.test_personalize_s1_identity import FakeAX

MAIL = AppIdentity("com.apple.mail", "Mail", pid=4242)
SAMPLE = "Notes: SELECTED: q3 salary table, then lunch"
SELECTED = "SELECTED: q3 salary table"
AWARE = {SettingsKey.APP_CONTEXT_ENABLED: True, SettingsKey.APP_CONTEXT_READ_TEXT: True}


@pytest.fixture
def ax(monkeypatch):
    """The macOS reader over a fake Accessibility API, with SELECTED selected."""
    fake = FakeAX(SAMPLE, SAMPLE.index(SELECTED), len(SELECTED))
    monkeypatch.setitem(sys.modules, "HIServices", fake)
    monkeypatch.setitem(sys.modules, "objc",
                        SimpleNamespace(autorelease_pool=contextlib.nullcontext))
    monkeypatch.setattr(_mac, "_AX_TEXT_ENABLED", True)
    monkeypatch.setattr(_mac, "_secure_input_enabled", lambda: False)
    return fake


# -- the readers ------------------------------------------------------------------------

def test_the_mac_reader_returns_the_selection_only_when_asked(ax):
    reader = _mac.AxTextReader()

    text_only = reader.read(MAIL, include_text=True, include_selection=False)
    both = reader.read(MAIL, include_text=True, include_selection=True)

    assert text_only == TextContext("Notes: ", "", ", then lunch", caret_known=True,
                                    selection_known=False, source="ax")
    assert both == TextContext("Notes: ", SELECTED, ", then lunch", caret_known=True,
                               selection_known=True, source="ax")


def test_a_long_selection_doesnt_hide_the_text_around_it_when_not_asked(monkeypatch):
    long_text = "before " + "x" * (focus_context.MAX_SELECTION_CHARS + 5) + " after"
    fake = FakeAX(long_text, 7, focus_context.MAX_SELECTION_CHARS + 5)
    monkeypatch.setitem(sys.modules, "HIServices", fake)
    monkeypatch.setitem(sys.modules, "objc",
                        SimpleNamespace(autorelease_pool=contextlib.nullcontext))
    monkeypatch.setattr(_mac, "_AX_TEXT_ENABLED", True)
    monkeypatch.setattr(_mac, "_secure_input_enabled", lambda: False)

    context = _mac.AxTextReader().read(MAIL, include_text=True, include_selection=False)

    assert (context.before, context.after, context.selected) == ("before ", " after", "")


# -- who asks for it --------------------------------------------------------------------

class _Asked(ContextCaptureService):
    def __init__(self):
        self.asked = []

    def request(self, *, include_text, include_selection=False):
        self.asked.append(include_selection)
        future: Future = Future()
        future.set_result(FocusSnapshot())
        return future

    def current_identity(self):
        return None

    def reread(self, identity, callback):
        callback(None)

    def shutdown(self):
        pass


@pytest.mark.parametrize("mode, source, include_selection, settings, asks", [
    (JobMode.DICTATION, "hotkey", False, AWARE, True),
    (JobMode.DICTATION, "dbus", False, AWARE, True),
    (JobMode.DICTATION, "hotkey", False, {SettingsKey.APP_CONTEXT_READ_TEXT: False}, False),
    (JobMode.DICTATION, "api", False, AWARE, False),
    (JobMode.DICTATION, "mcp", False, AWARE, False),
    (JobMode.ACTION, "hotkey", True, AWARE, True),
    (JobMode.ACTION, "hotkey", False, AWARE, False),
    (JobMode.ACTION, "api", True, AWARE, False),
    (JobMode.ACTION, "mcp", True, AWARE, False),
    (JobMode.COMMAND, "hotkey", False, {}, True),
])
def test_only_a_job_the_user_started_asks_for_the_selection(
    mode, source, include_selection, settings, asks
):
    service = _Asked()
    focus_context.set_service(service)

    begin_job(mode, settings, source=source, include_selection=include_selection)

    assert service.asked == [asks]


# -- end to end: the capture service, the reader and the agent's prompt -----------------

class _MacPlatform:
    name = "fake-mac"
    sync_identity = True
    text_supported = True

    def identity(self):
        return MAIL

    def text_reader(self):
        return _mac.AxTextReader()


@pytest.fixture
def capture(ax):
    service = CaptureService(_MacPlatform(), deadline_s=2.0, settings=lambda: {},
                             metrics=lambda **values: None)
    focus_context.set_service(service)
    yield service
    service.shutdown()


@pytest.fixture
def prompts(monkeypatch):
    seen = []

    def ask(agent_id, text, **kwargs):
        seen.append(text)
        return ActionResult(True, "", "an answer")

    monkeypatch.setattr(agents, "ask", ask)
    return seen


PARAMS = {"agent": "claude_code", "mode": "ask", "model": "", "include_selection": True,
          "surface": "auto", "folder": ""}


@pytest.mark.parametrize("source, shared", [("api", False), ("mcp", False), ("hotkey", True)])
def test_an_agent_prompt_carries_the_selection_only_for_the_users_job(
    capture, prompts, source, shared
):
    # The review's reproduction: with text near the cursor on, a program's
    # run of an agent action that includes the selection got it anyway.
    job = begin_job(JobMode.ACTION, AWARE, action_id="ag", source=source, include_selection=True)

    actions_runtime._agent(PARAMS, "summarize this", job, threading.Event())

    (prompt,) = prompts
    assert (SELECTED in prompt) is shared
    assert ("<selected_text>" in prompt) is shared
    assert job.snapshot(timeout=2).text.before == "Notes: "  # the text near it was still read
