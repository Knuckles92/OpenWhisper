"""The side panel's Walkthroughs page: one card per capture folder, newest first."""

import json
import time
from datetime import date, datetime

import pytest
from PyQt6.QtCore import QCoreApplication, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtTest import QSignalSpy, QTest

from services.walkthrough import library
from ui_qt.widgets import past_walkthroughs
from ui_qt.widgets.history_sidebar import HistorySidebar
from ui_qt.widgets.past_walkthroughs import PastWalkthroughsPanel, summarize

TODAY = date(2026, 10, 25)


def _walkthrough(root, name, sections, *, write_up=""):
    folder = root / name
    folder.mkdir(parents=True)
    listed = []
    for number, (text, with_shot) in enumerate(sections, 1):
        shot = None
        if with_shot:
            shot = f"shot-{number}.png"
            image = QImage(400, 300, QImage.Format.Format_RGB32)
            image.fill(QColor("#3a6ea5"))
            image.save(str(folder / shot))
        listed.append({"text": text, "shot": shot, "kept_at_s": float(number)})
    started = datetime.strptime(name, "%Y-%m-%d_%H-%M-%S").isoformat()
    (folder / "walkthrough.json").write_text(json.dumps(
        {"version": 1, "started_at": started, "duration_s": 106.0, "sections": listed}))
    if write_up:
        (folder / "write-up.md").write_text(write_up, encoding="utf-8")
        (folder / "write-up.pdf").write_bytes(b"%PDF-1.4")
    return folder


def _draft(root, name):
    folder = root / name
    folder.mkdir(parents=True)
    started = datetime.strptime(name, "%Y-%m-%d_%H-%M-%S").isoformat()
    (folder / library.DRAFT_NAME).write_text(json.dumps({"started_at": started}))
    return folder


def _loaded(panel):
    panel.refresh()
    deadline = time.monotonic() + 5.0
    while panel._total is None:
        assert time.monotonic() < deadline, "the folders were never read"
        QCoreApplication.processEvents()
        time.sleep(0.005)
    return panel


@pytest.fixture
def root(tmp_path):
    return tmp_path / "captures"


@pytest.fixture
def panel(root):
    widget = PastWalkthroughsPanel(root=root, today=lambda: TODAY)
    yield widget
    widget.close()


def test_a_written_up_walkthrough_is_titled_by_its_heading(root):
    folder = _walkthrough(
        root, "2026-10-25_22-46-05",
        [("I think we don't need this whole recent area", True), ("and this", True)],
        write_up="---\nfont: Georgia\n---\n\n# Rethinking the Recent area\n\nBody.")

    summary = summarize(folder, TODAY)

    assert summary.title == "Rethinking the Recent area"
    assert summary.meta == "2 screenshots · 1:46"
    assert summary.pill == "AI write-up"
    assert "recent area" in summary.words
    assert summary.thumb is not None
    assert summary.thumb.size() == past_walkthroughs.THUMB_SIZE


def test_an_as_spoken_walkthrough_is_titled_by_its_first_words(root):
    long_words = "word " * 40
    folder = _walkthrough(root, "2026-10-25_09-00-00", [(long_words, False)])

    summary = summarize(folder, TODAY)

    assert summary.title.endswith("…")
    assert len(summary.title) <= past_walkthroughs.TITLE_CHARS + 1
    assert summary.pill == ""
    assert summary.thumb is None


def test_an_interrupted_draft_says_so(root):
    summary = summarize(_draft(root, "2026-10-24_09-00-00"), TODAY)

    assert summary.title == "Not finished"
    assert (summary.pill, summary.pill_tone) == ("Interrupted", "warning")


def test_cards_list_newest_first_and_open_in_the_library(panel, root):
    older = _walkthrough(root, "2026-10-24_09-00-00", [("older one", True)])
    newer = _walkthrough(root, "2026-10-25_10-30-00", [("newer one", False)])
    spy = QSignalSpy(panel.walkthrough_selected)

    _loaded(panel)

    assert [card.summary.folder for card in panel.cards] == [newer, older]
    assert panel.cards[0].when_label.text() == "Today · 10:30 AM"
    assert panel.count_header.text() == "WALKTHROUGHS (2)"
    QTest.mouseClick(panel.cards[1], Qt.MouseButton.LeftButton)
    QTest.keyClick(panel.cards[0], Qt.Key.Key_Return)
    panel.library_button.click()
    assert [args[0] for args in spy] == [older, newer, None]


def test_search_keeps_the_cards_holding_every_word(panel, root):
    _walkthrough(root, "2026-10-24_09-00-00", [("the settings dialog is slow", True)])
    _walkthrough(root, "2026-10-25_10-30-00", [("the history panel", False)])
    _loaded(panel)

    panel.search_input.setText("History  PANEL")
    assert [card.isVisibleTo(panel) for card in panel.cards] == [True, False]
    assert panel.count_header.text() == "WALKTHROUGHS (1)"

    panel.search_input.setText("nothing like this")
    assert panel.empty_label.text() == "No walkthrough says that"


def test_without_walkthroughs_the_page_says_so(panel):
    _loaded(panel)

    assert panel.cards == []
    assert panel.empty_label.isVisibleTo(panel)
    assert panel.empty_label.text().startswith("Nothing yet")


def test_only_the_newest_are_built(panel, root, monkeypatch):
    monkeypatch.setattr(past_walkthroughs, "MAX_SHOWN", 2)
    for hour in (7, 8, 9):
        _walkthrough(root, f"2026-10-25_0{hour}-00-00", [(f"at {hour}", False)])

    _loaded(panel)

    assert len(panel.cards) == 2
    assert panel.count_header.text() == "WALKTHROUGHS (2+)"
    assert "all 3" in panel.empty_label.text()


def test_a_stale_read_is_dropped(panel, root):
    _walkthrough(root, "2026-10-25_10-30-00", [("one", False)])
    _loaded(panel)
    panel._token += 1  # a newer read is on its way

    past_walkthroughs._relay().loaded.emit(panel._token - 1, ([], 0))

    assert len(panel.cards) == 1


def test_the_sidebar_shows_walkthroughs_while_their_tab_is_open():
    sidebar = HistorySidebar()
    try:
        selected = QSignalSpy(sidebar.walkthrough_selected)
        assert sidebar.walkthroughs_content_widget is None  # made when first shown

        sidebar.set_page(sidebar.WALKTHROUGHS)
        panel = sidebar.walkthroughs_content_widget
        assert panel is not None and panel.isVisibleTo(sidebar)
        assert not sidebar.content_widget.isVisibleTo(sidebar)
        panel.library_button.click()
        assert [args[0] for args in selected] == [None]

        sidebar.set_page(sidebar.HISTORY)
        assert sidebar.content_widget.isVisibleTo(sidebar)
        assert not panel.isVisibleTo(sidebar)
    finally:
        sidebar.close()
