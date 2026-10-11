"""Windows links: Explorer is asked from a thread of its own, for a limited time (review round 4).

pywin32's ``pythoncom`` initialises COM on the thread that first imports it
and never undoes it, so the action worker that opened the first link stayed
a single-threaded apartment for good. And the calls into Explorer's desktop
had no time limit: a hung Explorer held an action's worker indefinitely.
Without pywin32 (declared since), links open as without Explorer's desktop.

Nothing here reaches the real shell: Explorer is faked (``_explorer_open``,
or ``_desktop_application`` with in-process objects) and ``os.startfile``
is replaced.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from services.actions import launch

ROOT = Path(__file__).resolve().parents[1]


class SlowExplorer:
    """Stands in for ``_explorer_open``: answers ``opens`` once told to."""

    def __init__(self):
        self.answer = threading.Event()
        self.done = threading.Event()
        self.asked = []
        self.opens = True

    def __call__(self, uri):
        self.asked.append(uri)
        try:
            self.answer.wait(5)
            return self.opens
        finally:
            self.done.set()


@pytest.fixture
def explorer(monkeypatch):
    """A slow Explorer and the links opened from this process instead."""
    fake, here = SlowExplorer(), []
    monkeypatch.setattr(launch, "_com", contextlib.nullcontext)
    monkeypatch.setattr(launch, "_explorer_open", fake)
    monkeypatch.setattr(launch.os, "startfile", here.append, raising=False)
    monkeypatch.setattr(launch, "EXPLORER_WAIT_S", 0.3)
    yield fake, here
    fake.answer.set()
    assert launch._explorer_turn.acquire(timeout=5)  # its thread is done with Explorer
    launch._explorer_turn.release()


def test_an_explorer_that_doesnt_answer_in_time_means_not_opened(explorer):
    fake, here = explorer
    fake.opens = False  # a late "no desktop" doesn't open it from here either
    started = time.monotonic()
    with pytest.raises(launch.NoAnswer, match="^Windows didn't answer. Try again.$"):
        launch._windows_open("s:x")
    assert time.monotonic() - started < 3

    fake.answer.set()
    assert fake.done.wait(5)
    time.sleep(0.05)
    assert here == []


def test_no_more_links_queue_behind_one_explorer_hasnt_answered(explorer):
    fake, here = explorer
    with pytest.raises(launch.NoAnswer):
        launch._windows_open("s:first")
    with pytest.raises(launch.NoAnswer, match="Windows didn't answer"):
        launch._windows_open("s:second")
    assert fake.asked == ["s:first"]

    fake.answer.set()
    assert fake.done.wait(5)
    launch._windows_open("s:third")  # Explorer answers again: links go to it
    assert fake.asked == ["s:first", "s:third"] and here == []


def test_explorer_answering_in_time_opens_the_link_once(explorer):
    fake, here = explorer
    fake.answer.set()
    launch._windows_open("s:x")
    assert fake.asked == ["s:x"] and here == []


def test_without_pywin32_a_link_opens_from_here(monkeypatch):
    here = []
    monkeypatch.setitem(sys.modules, "pythoncom", None)  # importing it fails
    monkeypatch.setattr(launch, "_com", contextlib.nullcontext)
    monkeypatch.setattr(launch, "_desktop_application", lambda: pytest.fail("asked Explorer"))
    monkeypatch.setattr(launch.os, "startfile", here.append, raising=False)
    launch._windows_open("s:x")
    assert here == ["s:x"]


def test_pywin32_is_a_declared_windows_dependency():
    lines = [line.partition("#")[0].strip()
             for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    assert any(re.fullmatch(r'pywin32>=[\d.]+ ; sys_platform == "win32"', line) for line in lines)


def test_a_failure_while_asking_explorer_reaches_the_caller(explorer, monkeypatch):
    def broken(uri):
        raise RuntimeError("broken")

    monkeypatch.setattr(launch, "_explorer_open", broken)
    with pytest.raises(RuntimeError, match="broken"):
        launch._windows_open("s:x")


# The first pythoncom import has to happen in a fresh process.
APARTMENT_CHECK = r"""
import ctypes, json, sys, threading
from services.actions import launch

opened = []

class Application:  # Explorer's desktop Shell.Application, in this process
    def GetIDsOfNames(self, name):
        return 7

    def Invoke(self, dispid, lcid, flags, result_wanted, *args):
        opened.append(args[0])

def startfile(uri):
    raise AssertionError("opened from this process")

launch._desktop_application = lambda: (Application(), 0)
launch._allow_foreground = lambda hwnd: None
launch.os.startfile = startfile

ole32 = ctypes.WinDLL("ole32")
ole32.CoGetApartmentType.argtypes = [ctypes.POINTER(ctypes.c_int)] * 2
ole32.CoGetApartmentType.restype = ctypes.c_long
seen = {}

def action_worker():
    launch._windows_open("s:x")
    kind, qualifier = ctypes.c_int(), ctypes.c_int()
    seen["apartment"] = ole32.CoGetApartmentType(ctypes.byref(kind),
                                                 ctypes.byref(qualifier)) & 0xFFFFFFFF

worker = threading.Thread(target=action_worker, name="action-run_0")
worker.start()
worker.join()
print(json.dumps({"apartment": seen["apartment"], "opened": opened,
                  "pythoncom": "pythoncom" in sys.modules}))
"""
CO_E_NOTINITIALIZED = 0x800401F0


@pytest.mark.skipif(sys.platform != "win32", reason="COM apartments are Windows'")
def test_opening_a_link_leaves_the_workers_thread_without_com(tmp_path):
    env = dict(os.environ, OPENWHISPER_DATA_DIR=str(tmp_path))
    completed = subprocess.run([sys.executable, "-c", APARTMENT_CHECK], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    seen = json.loads(completed.stdout.strip().splitlines()[-1])
    assert seen["opened"] == ["s:x"] and seen["pythoncom"]
    assert seen["apartment"] == CO_E_NOTINITIALIZED  # not left a single-threaded apartment
