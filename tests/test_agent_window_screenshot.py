"""An agent action's screenshot of the focused window (services/focus_context/screenshot.py).

No real window is captured: conftest gives every test a desktop with no
capture, and these tests install a fake one.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future

import pytest
from PyQt6.QtGui import QColor, QImage

from services.actions import agents
from services.actions import runtime as actions_runtime
from services.actions.model import Action, ActionKind, load_actions
from services.actions.runtime import ActionResult
from services.dictation_pipeline import DictationJob, JobMode, begin_job
from services.focus_context import AppIdentity, screenshot
from services.focus_context.screenshot import WindowImage
from services.settings import SettingsKey

EDITOR = AppIdentity("code.exe", "Visual Studio Code", pid=4242, window="0x1234",
                     platform="windows")
VAULT = AppIdentity("1password.exe", "1Password", pid=77, window="0x99", platform="windows")
SELF = AppIdentity("openwhisper", "OpenWhisper", pid=os.getpid(), is_self=True)


class _Desktop:
    """A desktop whose focused window is ``identity``, drawn ``size`` big."""

    def __init__(self, identity, size=(320, 200)):
        self.identity = identity
        self.size = size
        self.grabbed = []

    def focused(self):
        return (self.identity, self.identity.window) if self.identity else None

    def grab(self, handle):
        self.grabbed.append(handle)
        image = QImage(*self.size, QImage.Format.Format_RGB32)
        image.fill(QColor("#2050c0"))
        return image


def _capture(monkeypatch, desktop, settings=None):
    monkeypatch.setattr(screenshot, "_platform", lambda: desktop)
    return screenshot.request(settings or {}).result(timeout=5)


# ---- capture ----

def test_the_focused_window_comes_back_as_png_with_its_app(monkeypatch):
    desktop = _Desktop(EDITOR)
    image = _capture(monkeypatch, desktop)
    assert image.app == "Visual Studio Code" and desktop.grabbed == ["0x1234"]
    decoded = QImage.fromData(image.png, "PNG")
    assert (decoded.width(), decoded.height()) == (320, 200)


@pytest.mark.parametrize("identity, settings", [
    (SELF, {}),
    (VAULT, {SettingsKey.APP_CONTEXT_EXCLUDED_APPS: ["1Password"]}),
    (None, {}),
])
def test_own_windows_excluded_apps_and_no_window_are_never_captured(monkeypatch, identity, settings):
    desktop = _Desktop(identity)
    assert _capture(monkeypatch, desktop, settings) is None
    assert desktop.grabbed == []


def test_a_desktop_without_capture_gives_nothing(monkeypatch):
    assert not screenshot.supported()
    assert screenshot.request({}).result(timeout=5) is None


def test_a_failing_grab_gives_nothing(monkeypatch):
    desktop = _Desktop(EDITOR)
    desktop.grab = lambda handle: (_ for _ in ()).throw(OSError("denied"))
    assert _capture(monkeypatch, desktop) is None


def test_a_large_window_is_scaled_to_the_longest_edge(monkeypatch):
    image = _capture(monkeypatch, _Desktop(EDITOR, size=(3840, 1600)))
    decoded = QImage.fromData(image.png, "PNG")
    assert (decoded.width(), decoded.height()) == (screenshot.MAX_EDGE_PX, 800)


# ---- saving ----

def test_save_writes_a_png_and_removes_day_old_ones(monkeypatch, tmp_path):
    monkeypatch.setattr(screenshot, "folder", lambda: str(tmp_path / "shots"))
    first = screenshot.save(WindowImage(b"old", "Editor"))
    stale = time.time() - screenshot.KEEP_S - 60
    os.utime(first, (stale, stale))
    recent = screenshot.save(WindowImage(b"recent", "Editor"))
    path = screenshot.save(WindowImage(b"new", "Editor"))
    assert path.endswith(".png") and open(path, "rb").read() == b"new"
    assert not os.path.exists(first) and os.path.exists(recent)


def test_save_says_so_when_it_cannot_write(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    monkeypatch.setattr(screenshot, "folder", lambda: str(blocker / "shots"))
    assert screenshot.save(WindowImage(b"png", "Editor")) == ""


# ---- who takes one ----

@pytest.mark.parametrize("mode, source, wanted, taken", [
    (JobMode.ACTION, "hotkey", True, True),
    (JobMode.ACTION, "ui", True, True),
    (JobMode.ACTION, "hotkey", False, False),
    (JobMode.ACTION, "api", True, False),
    (JobMode.ACTION, "mcp", True, False),
    (JobMode.DICTATION, "hotkey", True, False),
])
def test_only_the_users_action_job_takes_a_screenshot(monkeypatch, mode, source, wanted, taken):
    requests = []
    monkeypatch.setattr(screenshot, "request", lambda settings: requests.append(settings) or Future())
    # App awareness is off: the action's own switch is the consent.
    job = begin_job(mode, {}, source=source, include_screenshot=wanted)
    assert bool(requests) is taken and (job.screen is not None) is taken


# ---- what the agent gets ----

def _done(value) -> Future:
    future: Future = Future()
    future.set_result(value)
    return future


IMAGE = WindowImage(b"\x89PNG-bytes", "Visual Studio Code")
PARAMS = {"agent": "codex", "mode": "handoff", "surface": "terminal", "folder": "",
          "new_folder": False, "include_selection": False, "include_screenshot": True,
          "model": ""}


def test_a_handoff_saves_the_screenshot_and_names_it_in_the_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(screenshot, "folder", lambda: str(tmp_path))
    calls = []
    monkeypatch.setattr(agents, "handoff", lambda agent_id, text, **kwargs: (
        calls.append((text, kwargs)) or ActionResult(True, "Opened Codex")))
    job = DictationJob(mode=JobMode.ACTION, source="hotkey", screen=_done(IMAGE))

    actions_runtime._agent(PARAMS, "why does this fail", job, threading.Event())

    ((text, kwargs),) = calls
    path = kwargs["image"]
    assert open(path, "rb").read() == IMAGE.png
    assert text == ("why does this fail\n\n"
                    '<context source="OpenWhisper">\n'
                    "<app>Visual Studio Code</app>\n"
                    f"<window_screenshot>{path}</window_screenshot>\n"
                    "</context>")


def test_an_ask_attaches_the_bytes_and_saves_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(screenshot, "folder", lambda: str(tmp_path / "unused"))
    asks = []
    monkeypatch.setattr(agents, "ask", lambda agent_id, text, **kwargs: (
        asks.append((text, kwargs["image"])) or ActionResult(True, "", "ok")))
    job = DictationJob(mode=JobMode.ACTION, source="hotkey", screen=_done(IMAGE))

    actions_runtime._agent({**PARAMS, "mode": "ask"}, "what is this", job, threading.Event())

    assert asks == [("what is this", IMAGE.png)]
    assert not (tmp_path / "unused").exists()


def test_no_screenshot_leaves_the_words_alone(monkeypatch):
    calls = []
    monkeypatch.setattr(agents, "handoff", lambda agent_id, text, **kwargs: (
        calls.append((text, kwargs["image"])) or ActionResult(True, "Opened Codex")))
    job = DictationJob(mode=JobMode.ACTION, source="hotkey", screen=_done(None))

    actions_runtime._agent(PARAMS, "hello", job, threading.Event())

    assert calls == [("hello", "")]


@pytest.mark.parametrize("agent_id, expected", [
    ("claude_code", ["claude", "--add-dir", os.path.join("shots"), "--", "look at this"]),
    ("codex", ["codex", "--image", os.path.join("shots", "w.png"), "--", "look at this"]),
    ("opencode_cli", ["opencode", "--prompt=look at this"]),
])
def test_a_terminal_session_gets_the_screenshot_its_own_way(agent_id, expected):
    program = expected[0]
    argv = agents.terminal_argv(agent_id, program, "look at this", os.path.join("shots", "w.png"))
    assert argv == expected


def test_the_screenshot_option_is_saved_with_the_action():
    settings = {SettingsKey.ACTIONS: [Action(
        "a1", "Agent", ActionKind.AGENT, {"include_screenshot": True}).to_dict()]}
    (action,) = load_actions(settings)
    assert action.param("include_screenshot") is True
    assert Action("a2", "Other", ActionKind.AGENT).param("include_screenshot") is False


# ---- the editor ----

@pytest.mark.qt
@pytest.mark.parametrize("supported", [True, False])
def test_the_agent_fields_switch_round_trips_and_is_off_where_unsupported(
        monkeypatch, _session_qt_application, supported):
    from ui_qt.widgets.action_copy import SCREENSHOT_DETAIL, SCREENSHOT_UNAVAILABLE
    from ui_qt.widgets.action_fields import AgentFields

    monkeypatch.setattr(screenshot, "supported", lambda: supported)
    fields = AgentFields()
    row = fields.screenshot_row
    assert row.switch.isEnabled() is supported
    assert row.detail_label.text() == (SCREENSHOT_DETAIL if supported else SCREENSHOT_UNAVAILABLE)
    assert fields.params()["include_screenshot"] is False
    fields.set_params({"include_screenshot": True})
    assert row.switch.isChecked() and fields.params()["include_screenshot"] is True
