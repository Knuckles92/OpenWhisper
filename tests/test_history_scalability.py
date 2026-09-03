"""Focused Qt history loading regressions."""

import os
import threading
import time
from types import SimpleNamespace
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


def test_large_history_does_not_stretch_section_header():
    app = QApplication.instance() or QApplication([])
    sidebar = HistorySidebar()
    sidebar.resize(sidebar.EXPANDED_WIDTH, 600)
    sidebar._set_sidebar_width(sidebar.EXPANDED_WIDTH)
    sidebar._is_expanded = True

    entries = [
        SimpleNamespace(
            id=f"entry-{index}",
            text="A transcript preview with enough words to wrap onto another line.",
            raw_text="Raw transcript",
            formatted_timestamp="Sep 02, 2026 06:54 PM",
            model="local_whisper (turbo | cuda (float16))",
            audio_file=None,
            file_size=None,
            cleanup_provider="openrouter",
            cleanup_model="google/gemini-3.7-flash",
            source_name=f"recording_{index:03d}.wav",
            preview_text=(
                "A transcript preview with enough words to wrap onto another line."
            ),
        )
        for index in range(sidebar.MAX_HISTORY_ITEMS + 1)
    ]
    sidebar._history_load_generation = 1
    sidebar._apply_history_results(1, "", entries, "")
    sidebar.show()
    app.processEvents()

    assert sidebar.history_header.height() <= 30
    first_item = sidebar.history_list_layout.itemAt(0).widget()
    assert first_item.y() - sidebar.history_header.geometry().bottom() <= 13

    sidebar.deleteLater()
    app.processEvents()
