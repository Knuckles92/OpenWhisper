"""Shared pytest configuration and fixtures for the OpenWhisper test suite.

Putting the project root on sys.path here removes the need for the
``sys.path.insert`` boilerplate that used to be repeated in most test
modules.
"""
from __future__ import annotations

import os
import sys
import ast
from functools import lru_cache
from pathlib import Path

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


@lru_cache(maxsize=None)
def _qt_imports(path: str):
    root = Path(PROJECT_ROOT)
    source_path = Path(path)
    try:
        source = source_path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
    except (OSError, SyntaxError, UnicodeError):
        return True, ()
    if "ui_qt" in source or "PyQt6" in source:
        return True, ()
    package = source_path.relative_to(root).parent.parts
    dependencies = set()
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = (".".join(package[:len(package) - node.level + 1]) + "."
                    if node.level else "") + (node.module or "")
            names = [base] + [f"{base}.{alias.name}" for alias in node.names]
        for name in names:
            parts = name.split(".")
            module = root.joinpath(*parts)
            candidates = [module.with_suffix(".py")]
            candidates.extend(root.joinpath(*parts[:i], "__init__.py")
                              for i in range(1, len(parts) + 1))
            dependencies.update(str(candidate) for candidate in candidates if candidate.is_file())
    return False, tuple(dependencies)


@lru_cache(maxsize=None)
def _requires_qt(path: str) -> bool:
    """Conservatively follow local imports, including deferred UI imports.

    Tests may also opt in explicitly with ``pytest.mark.qt``. UI references
    count for monkeypatch/importlib targets. Cache parsed modules so deciding
    fixture scope does not repeat the same import graph for each test case.
    """
    pending, visited = [path], set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        direct, dependencies = _qt_imports(current)
        if direct:
            return True
        pending.extend(dependencies)
    return False


def pytest_collection_modifyitems(items):
    for item in items:
        if _requires_qt(str(item.path)):
            item.add_marker(pytest.mark.qt)


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


@pytest.fixture(scope="session")
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
    config.RECORDED_AUDIO_FILE = str(tmp_path_factory.mktemp("recording-session") / "recorded_audio.wav")
    # From source, recordings/ is CWD-relative: the checkout's own saved
    # audio, which retention rotation deletes without a Recycle Bin.
    config.RECORDINGS_FOLDER = str(tmp_path_factory.mktemp("recordings-session"))
    # openwhisper.db is CWD-relative too: the checkout's real history. An
    # empty database imports HISTORY_FILE and renames it, so move both.
    database_folder = tmp_path_factory.mktemp("database-session")
    config.DATABASE_FILE = str(database_folder / "openwhisper.db")
    config.HISTORY_FILE = str(database_folder / "transcription_history.json")
    # Diagnostic handlers retain their paths after a controller shuts down.
    # Keep that session fallback away from the checkout and the user's logs.
    config.LOG_FILE = str(tmp_path_factory.mktemp("diagnostics-session") / "openwhisper.log")
    from services.database import db
    from services.history_manager import history_manager

    # The lazy singletons bind their paths on first use; rebuild them here.
    db.close()
    history_manager._instance = None
    yield
    db.close()


@pytest.fixture(autouse=True)
def _isolated_qt_widgets(
    request, _isolated_settings_store, _isolated_credential_store
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
    if request.node.get_closest_marker("qt") is None:
        yield
        return
    from PyQt6.QtCore import QCoreApplication, QEvent

    app = request.getfixturevalue("_session_qt_application")
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
        patcher.setattr(config, "RECORDED_AUDIO_FILE", str(tmp_path / "recorded_audio.wav"))
        patcher.setattr(config, "RECORDINGS_FOLDER", str(tmp_path / "recordings"))
        patcher.setattr(config, "DATABASE_FILE", str(tmp_path / "openwhisper.db"))
        patcher.setattr(config, "HISTORY_FILE", str(tmp_path / "transcription_history.json"))
        patcher.setattr(config, "LOG_FILE", str(tmp_path / "openwhisper.log"))
        # Bind the real module now: some tests swap services.database in
        # sys.modules, and teardown must still reach this manager.
        from services.database import db
        from services.history_manager import history_manager

        patcher.setattr(db, "_instance", None)
        patcher.setattr(history_manager, "_instance", None)
        yield
        # Release the test's SQLite handles; Windows cannot delete tmp_path
        # while the file, or its -wal and -shm siblings, is still open.
        db.close()


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


@pytest.fixture(autouse=True)
def _no_real_network_discovery():
    """Keep every test off this computer's real network.

    A sharing host answers discovery on a fixed UDP port and a client
    broadcasts to the LAN (services/remote_asr/discovery.py); Windows also
    reads its network profiles through PowerShell. Here hosts answer on a
    free port, searches go nowhere unless a test names its targets, and
    there are no network profiles.
    """
    from services.remote_asr import discovery, reachability

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(discovery, "DISCOVERY_PORT", 0)
        patcher.setattr(discovery, "broadcast_targets", lambda: [])
        patcher.setattr(discovery, "sweep_addresses", lambda: [])
        patcher.setattr(reachability, "windows_network_profiles", lambda timeout=8.0: [])
        yield


@pytest.fixture(autouse=True)
def _no_installed_agent_probes(request):
    """Keep Settings from running the user's real coding agents.

    Opening Meeting Mode → Intelligence scans for Claude Code, Codex, and
    OpenCode by running their command lines. Here the scan finds nothing
    unless a test patches ``ui_qt.widgets.agent_picker`` itself.
    """
    if request.node.get_closest_marker("qt") is None:
        yield
        return
    from services.installed_agents import AGENT_ORDER, AGENT_SPECS, AgentModel
    from ui_qt.widgets import agent_picker

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(agent_picker, "scan_installed_agents",
                        lambda refresh=False: dict.fromkeys(AGENT_ORDER))
        patcher.setattr(agent_picker, "cached_agents", lambda: None)
        patcher.setattr(agent_picker, "list_agent_models", lambda agent: [
            AgentModel("", f"{AGENT_SPECS[agent.id].name} default")
        ])
        yield


@pytest.fixture(autouse=True)
def _before_openai_transcription_shutdown():
    """Pin the OpenAI retirement clock to before 2027-02-26.

    Tests that select a retiring OpenAI model must not start failing when the
    real date passes. Tests of the post-shutdown behavior pass ``today`` or
    re-pin ``openai_retirement._today``.
    """
    from datetime import date

    from services import openai_retirement

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(openai_retirement, "_today", lambda: date(2026, 9, 26))
        yield


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
