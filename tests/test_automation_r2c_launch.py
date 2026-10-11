"""Opening links on Windows (review round 2): handlers that are COM objects.

Nothing real opens: the association lookup and the shell are fakes. The
association tables are what AssocQueryStringW answers on a real Windows 11.
"""

import sys
import threading

import pytest

from services.actions import launch
from tests import test_automation_agents_launch as base

fresh_handler_cache = base.fresh_handler_cache
FakeAssoc = base.FakeAssoc
NO_ASSOCIATION = base.NO_ASSOCIATION
COMMAND, EXECUTABLE, DELEGATE_EXECUTE = 1, 2, 18  # ASSOCSTR_*
APPX_PROTOCOL = "{A56A841F-E974-45C1-8001-7E3F8A085917}"
WINDOWS_PAGES = "{4ed3a719-cea8-4bd9-910d-e252f997afc2}"
CLAUDE_CODE = r"C:\Users\u\.local\bin\claude.exe"

#: How schemes are registered: a classic command, a packaged app, Windows' own pages.
REGISTERED = FakeAssoc({
    ("claude-cli", COMMAND): (0, f'"{CLAUDE_CODE}" --handle-uri "%1"'),
    ("claude-cli", EXECUTABLE): (0, CLAUDE_CODE),
    ("https", COMMAND): (0, r'"C:\Program Files\Browser\browser.exe" --single-argument %1'),
    ("https", EXECUTABLE): (0, r"C:\Program Files\Browser\browser.exe"),
    ("codex", DELEGATE_EXECUTE): (0, APPX_PROTOCOL),
    ("ms-settings", DELEGATE_EXECUTE): (0, WINDOWS_PAGES),
    # Both registered: ShellExecute uses the COM object.
    ("tel", COMMAND): (0, r'C:\WINDOWS\system32\OpenWith.exe "%1"'),
    ("tel", DELEGATE_EXECUTE): (0, "{e44e9428-bdbc-4987-a099-40dc8fd255e7}"),
})


# -- #3: DelegateExecute handlers count -------------------------------------------------


@pytest.mark.parametrize("scheme", ["ms-settings", "codex", "tel"])
def test_a_com_handler_counts(monkeypatch, scheme):
    monkeypatch.setattr(launch, "_assoc_query", REGISTERED)
    assert launch._windows_handler(scheme) is True


@pytest.mark.parametrize("table, expected", [
    ({}, False),                                                        # nothing at all
    ({("bingmaps", 20): (0, "bingmaps")}, False),                       # a bare key
    ({("bingmaps", COMMAND): (0x80004005, ""),
      ("bingmaps", DELEGATE_EXECUTE): (0x80004005, "")}, None),         # lookup failed
])
def test_only_no_handler_at_all_is_refused(monkeypatch, table, expected):
    monkeypatch.setattr(launch, "_assoc_query", FakeAssoc(table))
    assert launch._windows_handler("bingmaps") is expected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows opens links through associations")
@pytest.mark.parametrize("uri", ["ms-settings:display", "ms-settings:sound"])
def test_windows_own_pages_open(monkeypatch, uri):
    opened = []
    monkeypatch.setattr(launch, "_assoc_query", REGISTERED)
    monkeypatch.setattr(launch.os, "startfile", opened.append)
    launch.open_uri(uri)
    assert opened == [uri]


def _delegate_with_com(scheme):
    """The DelegateExecute answer on a fresh thread that has COM, as ShellExecute has."""
    import ctypes

    answers = []

    def look():
        ole32 = ctypes.WinDLL("ole32")
        initialized = ole32.CoInitializeEx(None, 0x6)
        try:
            answers.append(launch._assoc_query(scheme, DELEGATE_EXECUTE))
        finally:
            if initialized >= 0:
                ole32.CoUninitialize()

    worker = threading.Thread(target=look)
    worker.start()
    worker.join(10)
    return answers[0]


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows association API")
def test_a_worker_thread_sees_windows_own_packaged_protocols():
    # Windows reads some packaged apps' protocols (ms-actioncenter:, the
    # notification centre) only through COM; a worker thread without COM was
    # told they had no handler. Read-only: nothing is opened.
    scheme = "ms-actioncenter"
    if _delegate_with_com(scheme)[0] != 0:
        pytest.skip(f"{scheme}: isn't registered on this computer")
    found = []
    worker = threading.Thread(target=lambda: found.append(launch._windows_handler(scheme)))
    worker.start()
    worker.join(10)
    assert found == [True]
