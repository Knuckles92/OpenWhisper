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
    sidebar._apply_history_results(1, "", [], "")
    sidebar.show()
    app.processEvents()
    empty_header_height = sidebar.history_header.height()

    sidebar._apply_history_results(1, "", entries, "")
    app.processEvents()

    assert sidebar.history_header.height() == empty_header_height
    first_item = sidebar.history_list_layout.itemAt(0).widget()
    assert first_item.y() - sidebar.history_header.geometry().bottom() <= 13

    sidebar.deleteLater()
    app.processEvents()


def _expanded_sidebar(app):
    sidebar = HistorySidebar()
    sidebar.resize(sidebar.EXPANDED_WIDTH, 600)
    sidebar._set_sidebar_width(sidebar.EXPANDED_WIDTH)
    sidebar._is_expanded = True
    sidebar._history_load_generation = 1
    sidebar.show()
    app.processEvents()
    return sidebar


def _visible_cards(sidebar):
    from ui_qt.widgets.history_sidebar import HistoryItemWidget

    content = sidebar.scroll_area.widget()
    return [card for card in content.findChildren(HistoryItemWidget) if card.isVisibleTo(content)]


def _entries(prefix, count):
    from services.models import TranscriptionHistory

    return [
        TranscriptionHistory.create(text=f"{prefix} {index}", model="local_whisper",
                                    source_name=f"{prefix}-{index}.wav")
        for index in range(count)
    ]


def test_history_header_counts_the_cards_after_a_batch_lands_on_three_uploads():
    app = QApplication.instance() or QApplication([])
    sidebar = _expanded_sidebar(app)
    singles = _entries("single", 3)
    sidebar._apply_history_results(1, "", list(singles), "")
    app.processEvents()
    assert sidebar.history_header.text() == "HISTORY (3)"

    batch = _entries("batch", 20)
    sidebar._apply_history_results(1, "", batch + singles, "")
    app.processEvents()

    assert sidebar.rendered_card_count() == 23
    assert len(_visible_cards(sidebar)) == 23
    assert sidebar.history_header.text() == "HISTORY (23)"
    sidebar.deleteLater()
    app.processEvents()


def test_history_header_matches_the_cards_when_a_card_fails_to_build():
    """The header was set before the cards: a failed build left it over the old list."""
    from ui_qt.widgets import history_sidebar as module

    app = QApplication.instance() or QApplication([])
    sidebar = _expanded_sidebar(app)
    singles = _entries("single", 3)
    sidebar._apply_history_results(1, "", list(singles), "")
    batch = _entries("batch", 20)
    real = module.HistoryItemWidget

    def flaky(entry, *args, **kwargs):
        if entry is batch[5]:
            raise RuntimeError("card could not be built")
        return real(entry, *args, **kwargs)

    with patch.object(module, "HistoryItemWidget", side_effect=flaky):
        try:
            sidebar._apply_history_results(1, "", batch + singles, "")
        except RuntimeError:
            pass
    app.processEvents()

    shown = len(_visible_cards(sidebar))
    assert sidebar.rendered_card_count() == shown
    assert sidebar.history_header.text() == f"HISTORY ({shown})"
    sidebar.deleteLater()
    app.processEvents()


def test_history_shows_one_card_per_entry_id():
    app = QApplication.instance() or QApplication([])
    sidebar = _expanded_sidebar(app)
    entries = _entries("entry", 4)
    sidebar._apply_history_results(1, "", [*entries, entries[1], entries[2]], "")
    app.processEvents()
    sidebar._apply_history_results(1, "", list(entries), "")
    app.processEvents()

    assert len(_visible_cards(sidebar)) == 4
    assert sidebar.history_header.text() == "HISTORY (4)"
    sidebar.deleteLater()
    app.processEvents()


def test_cleared_history_cards_leave_the_list_at_once():
    app = QApplication.instance() or QApplication([])
    sidebar = _expanded_sidebar(app)
    sidebar._apply_history_results(1, "", _entries("entry", 3), "")
    app.processEvents()
    # Held, as a playing card's audio player can be, so they outlive the clear.
    cards = _visible_cards(sidebar)

    sidebar._apply_history_results(1, "", [], "")

    # Before the event loop runs the deferred deletes.
    assert _visible_cards(sidebar) == []
    assert not any(card.isVisible() for card in cards)
    assert sidebar.history_header.text() == "HISTORY"
    sidebar.deleteLater()
    app.processEvents()
