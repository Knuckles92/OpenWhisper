"""Shared pytest configuration and fixtures for the OpenWhisper test suite.

Putting the project root on sys.path here removes the need for the
``sys.path.insert`` boilerplate that used to be repeated in most test
modules.
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pytest


def _disable_tqdm_monitor() -> None:
    """Stop tqdm from starting its watchdog thread.

    The monitor is a daemon thread that wakes every few seconds. One awake
    inside native code while the interpreter finalizes takes the process down
    with an access violation, which replaces pytest's exit status after every
    test has already passed. Setting the interval on the base class before any
    bar exists also covers the subclasses huggingface_hub installs.
    """
    try:
        from tqdm import tqdm
    except ModuleNotFoundError:
        return
    tqdm.monitor_interval = 0


_disable_tqdm_monitor()


_session_status: list[int] = []


def pytest_sessionfinish(session, exitstatus):
    _session_status.append(int(exitstatus))


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    """Leave on pytest's status instead of racing native teardown.

    Qt, PortAudio, and the ML runtimes are unloaded in an order Python does not
    control, and about one full-suite run in three dies with an access
    violation *after* the report is written — replacing a passing status with
    0xC0000005. This hook runs once the report and every plugin's teardown are
    done, so there is nothing left to observe. Set
    ``OPENWHISPER_TEST_FULL_TEARDOWN=1`` to keep finalization when debugging
    that crash.
    """
    if os.environ.get("OPENWHISPER_TEST_FULL_TEARDOWN"):
        return
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_session_status[-1] if _session_status else 0)


@pytest.fixture
def db(tmp_path):
    """A DatabaseManager backed by a throwaway sqlite file."""
    from services.database import DatabaseManager

    manager = DatabaseManager(db_path=str(tmp_path / "test.db"))
    yield manager
    manager.close()


@pytest.fixture
def repo(db):
    """A SqlMeetingRepository on top of the shared ``db`` fixture."""
    from meeting.persist.repository import SqlMeetingRepository

    return SqlMeetingRepository(db=db)
