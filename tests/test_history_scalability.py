"""Focused Qt history loading regressions."""

import os
import threading
import time
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui_qt.widgets.history_sidebar import HistorySidebar


def test_history_sidebar_loads_only_one_page_off_the_ui_thread():
    app = QApplication.instance() or QApplication([])
    main_thread = threading.get_ident()
    called = threading.Event()
    observed = {}

    def get_history(*, limit):
        observed["limit"] = limit
        observed["thread_id"] = threading.get_ident()
        called.set()
        return []

    sidebar = HistorySidebar()
    with patch(
        "ui_qt.widgets.history_sidebar.history_manager.get_history",
        side_effect=get_history,
    ):
        sidebar._load_history()
        assert called.wait(1.0)
        deadline = time.monotonic() + 1.0
        while sidebar.history_list_layout.count() == 0 and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()

    assert observed["limit"] == sidebar.MAX_HISTORY_ITEMS + 1
    assert observed["thread_id"] != main_thread

    sidebar.deleteLater()
    app.processEvents()
