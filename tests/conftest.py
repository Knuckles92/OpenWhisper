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


@pytest.fixture(scope="session", autouse=True)
def _session_qt_application():
    from PyQt6.QtWidgets import QApplication
    from ui_qt.utils.font_scale import WidgetStyleFilter

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if sys.platform == "win32":
        # Qt's offscreen plugin does not discover the Windows system fonts.
        os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ["WINDIR"], "Fonts"))
    app = QApplication.instance() or QApplication([])
    # Standalone widgets need the same token resolution as QtApplication;
    # unresolved local QSS changes both their appearance and size hints.
    style_filter = WidgetStyleFilter(app)
    app.installEventFilter(style_filter)
    yield app
    app.removeEventFilter(style_filter)


@pytest.fixture(scope="session", autouse=True)
def _session_settings_store(tmp_path_factory):
    from config import config
    from services.settings import settings_manager

    # Keep a disposable fallback through shutdown: Qt callbacks can outlive
    # per-test patches and must never regain access to the user's settings.
    settings_path = str(tmp_path_factory.mktemp("settings-session") / "settings.json")
    config.SETTINGS_FILE = settings_path
    settings_manager.settings_file = settings_path


@pytest.fixture(autouse=True)
def _isolated_qt_widgets(
    _session_qt_application, _isolated_settings_store, _isolated_credential_store
):
    """Destroy each test's widgets before the next test can restyle them.

    close() only hides most Qt windows, and processEvents() does not flush
    DeferredDelete events without a running event loop. Signal connections
    can also keep Python wrappers alive. Leaving those widgets in allWidgets()
    made later font/theme tests repolish thousands of abandoned controls until
    CI's 20-minute guard killed the suite.

    Preserve widgets owned by broader-scoped fixtures. Settings and credentials
    remain isolated while destruction callbacks run.
    """
    from PyQt6.QtCore import QCoreApplication, QEvent

    app = _session_qt_application
    existing = set(app.allWidgets())
    yield
    created = set(app.allWidgets()) - existing
    for widget in created:
        # Let Qt destroy children in ownership order; deleting controls before
        # their container can invalidate internal popup/layout pointers.
        if widget.parentWidget() not in created:
            widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture(autouse=True)
def _isolated_settings_store(_session_settings_store, tmp_path):
    from config import config
    from services.settings import settings_manager

    with pytest.MonkeyPatch.context() as patcher:
        settings_path = str(tmp_path / "settings.json")
        patcher.setattr(config, "SETTINGS_FILE", settings_path)
        patcher.setattr(settings_manager, "settings_file", settings_path)
        yield


@pytest.fixture(autouse=True)
def _isolated_credential_store():
    """Point every test at an in-memory credential store.

    The real store is the developer's own Windows Credential Manager or
    Keychain; a test must neither read a real key out of it nor leave one
    behind. Also applies to unittest.TestCase classes.
    """
    from services import credentials

    previous = credentials.set_store(
        credentials.CredentialStore(backend_factory=credentials.memory_backend)
    )
    try:
        yield
    finally:
        credentials.set_store(previous)


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
