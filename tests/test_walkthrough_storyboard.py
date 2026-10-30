"""The walkthrough storyboard: reorder, write-up editing, and the Walkthrough tab showing the newest."""
from unittest.mock import Mock, patch

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QLabel

from services.walkthrough import library
from tests.test_walkthrough_library import capture as capture_fixture
from tests.test_walkthrough_library import noted_capture
from ui_qt.widgets.walkthrough_review import WalkthroughReview

capture = capture_fixture

WRITE_UP = (
    "# Two slow spots\n\n"
    "1. Billing stalls.\n\n"
    "   ![Billing](shot-1.png)\n\n"
    "2. Invoices hang.\n\n"
    "![Invoices](shot-2.png)\n\n"
    "Both happen after sign-in."
)


def _written(capture):
    (capture.folder / "write-up.md").write_text(WRITE_UP, encoding="utf-8")
    (capture.folder / "write-up.pdf").write_bytes(b"%PDF-1.4 old")


def _wait(review):
    for _ in range(500):
        if not review._busy:
            return
        QTest.qWait(10)
    pytest.fail("Review worker did not finish")


# ---- the library ----

def test_saving_in_a_new_order_moves_each_screenshot_with_its_words(capture):
    library.save_review(capture.folder, ["second", "first", "last"], [True] * 3, [1, 0, 2])
    saved = library.load_document(capture.folder)
    assert [s.text for s in saved.sections] == ["second", "first", "last"]
    assert [s.shot.name if s.shot else None for s in saved.sections] == [
        "shot-2.png", "shot-1.png", None]
    assert library.load_result(capture.folder).history_text == (
        "second\n\n[Screenshot 1]\n\nfirst\n\n[Screenshot 2]\n\nlast")


def test_an_order_that_is_not_every_section_once_is_refused(capture):
    with pytest.raises(ValueError):
        library.save_review(capture.folder, ["a", "b", "c"], [True] * 3, [0, 0, 2])


def test_a_write_up_cuts_at_its_screenshots_and_joins_back_unchanged():
    parts = library.write_up_parts(WRITE_UP)
    assert parts == [
        ("# Two slow spots\n\n1. Billing stalls.", "   ![Billing](shot-1.png)"),
        ("2. Invoices hang.", "![Invoices](shot-2.png)"),
        ("Both happen after sign-in.", ""),
    ]
    assert library.join_write_up(parts) == WRITE_UP


def test_a_write_up_line_names_a_shot_only_when_it_is_this_walkthroughs(capture):
    assert library.write_up_shot(capture.folder, "  ![x](shot-2.png)") == capture.folder / "shot-2.png"
    assert library.write_up_shot(capture.folder, "![x](elsewhere.png)") is None
    assert library.write_up_shot(capture.folder, "![x](shot-9.png)") is None


def test_saving_an_edited_write_up_rebuilds_its_documents(capture):
    _written(capture)
    result = library.save_write_up(capture.folder, "# Fixed\n\nNew words.\n\n![Billing](shot-1.png)")
    assert result.written_up
    assert (capture.folder / "write-up.md").read_text(encoding="utf-8").startswith("# Fixed")
    assert (capture.folder / "write-up.pdf").read_bytes().startswith(b"%PDF")
    assert (capture.folder / "write-up.docx").is_file()
    assert "New words." in library.load_result(capture.folder).clipboard_text


# ---- the storyboard ----

@pytest.fixture
def review(capture):
    widget = WalkthroughReview(copy_files=Mock(return_value=True))
    widget.changed.connect(widget.show_folder)
    widget.show_folder(capture.folder)
    yield widget
    widget._dirty = False
    widget.close()


def test_rows_show_each_screenshot_beside_its_words(review):
    assert [row.editor.toPlainText() for row in review.rows] == ["first", "second", "last"]
    assert [row.check.isChecked() for row in review.rows] == [True, True, False]
    assert not review.version_bar.isVisible()


def test_held_notes_show_on_their_rows_and_stay_through_a_save(tmp_path):
    folder = noted_capture(tmp_path)
    widget = WalkthroughReview(copy_files=Mock(return_value=True))
    widget.changed.connect(widget.show_folder)
    widget.show_folder(folder)
    shown = [[label.text().split("\n")[1] for label in row.findChildren(QLabel, "wlNote")]
             for row in widget.rows]
    assert shown == [[], ["make it purple"], ["sign off warmly"]]
    widget.rows[0].editor.setPlainText("first, fixed")
    widget.save_button.click()
    _wait(widget)
    assert [s.notes for s in library.load_document(folder).sections] == [
        (), ("make it purple",), ("sign off warmly",)]
    widget.close()


def test_moving_a_row_renumbers_it_and_saving_keeps_the_new_order(review, capture):
    review._move(1, 0)
    assert review._dirty
    assert [row.step.text() for row in review.rows] == ["1", "2", "3"]
    assert review.rows[0].editor.accessibleName() == "Section 1 transcript"
    review.save_button.click()
    _wait(review)
    assert [s.text for s in library.load_document(capture.folder).sections] == [
        "second", "first", "last"]


def test_alt_arrows_in_the_words_move_their_row(review):
    last = review.rows[2]
    QTest.keyClick(last.editor, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    assert review.rows.index(last) == 1
    QTest.keyClick(review.rows[0].editor, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    assert review.rows.index(last) == 1


def test_a_write_up_opens_on_itself_and_can_switch_to_the_words_as_spoken(review, capture):
    _written(capture)
    review.show_folder(capture.folder)
    assert review.version_bar.isVisibleTo(review)
    assert [row.editor.toPlainText() for row in review.rows][1] == "2. Invoices hang."
    assert review.rows[0].line == "   ![Billing](shot-1.png)"
    assert not review.rows[0].check.isVisibleTo(review)
    assert review.chip.name.text() == "write-up.pdf"
    review._on_version_chosen(1)
    assert [row.editor.toPlainText() for row in review.rows] == ["first", "second", "last"]
    assert review.chip.name.text() == "walkthrough.pdf"


def test_the_version_switch_sits_inside_the_header_beside_the_title(review, capture):
    _written(capture)
    review.resize(900, 600)
    review.show()
    review.show_folder(capture.folder)
    QTest.qWait(20)
    bar = review.version_bar.geometry()
    assert bar.width() > 0
    assert bar.right() < review.width()
    assert review.title.geometry().right() < bar.left()


def test_saving_edited_write_up_rows_rebuilds_the_write_up_in_their_order(review, capture):
    _written(capture)
    review.show_folder(capture.folder)
    review._move(1, 0)
    review.rows[0].editor.setPlainText("2. Invoices hang for four seconds.")
    review.save_button.click()
    _wait(review)
    text = (capture.folder / "write-up.md").read_text(encoding="utf-8")
    assert text.index("Invoices hang for four seconds") < text.index("Billing stalls")
    assert text.index("shot-2.png") < text.index("shot-1.png")


def test_editing_the_words_as_spoken_offers_to_redo_the_write_up(review, capture, monkeypatch):
    _written(capture)
    review.show_folder(capture.folder, spoken=True)
    review.rows[0].editor.setPlainText("first, fixed")
    assert review.save_redo_button.isVisibleTo(review)
    assert review.save_button.text() == "Save without write-up"
    redone = Mock(side_effect=lambda folder, cancel_event: library.load_result(folder))
    monkeypatch.setattr(library, "retry_write_up", redone)
    review.save_redo_button.click()
    _wait(review)
    redone.assert_called_once()
    assert library.load_document(capture.folder).sections[0].text == "first, fixed"


def test_copying_waits_for_saved_edits_and_copies_the_version_shown(review, capture):
    review.rows[0].editor.setPlainText("unsaved")
    review.pdf_button.click()
    review._copy_files.assert_not_called()
    review.show_folder(capture.folder)
    review.pdf_button.click()
    args, _kwargs = review._copy_files.call_args
    assert args == ([capture.folder / "walkthrough.pdf"],)


# ---- the Walkthrough tab ----

@pytest.fixture
def tab(capture):
    from ui_qt.widgets import walkthrough_tab

    with patch.object(walkthrough_tab, "captures_root", return_value=capture.root):
        widget = walkthrough_tab.WalkthroughTab()
        yield widget
        widget.review._dirty = False
        widget.close()


def test_the_tab_opens_on_the_newest_walkthrough_with_setup_folded(tab, capture):
    assert tab.review.folder == capture.folder
    assert tab.fills_height()
    assert tab.setup_row.isVisibleTo(tab)
    assert not tab.copy_area.isVisibleTo(tab)
    assert not tab.write_up_card.isVisibleTo(tab)
    tab.setup_toggle.click()
    assert tab.copy_area.isVisibleTo(tab)
    assert tab.write_up_card.isVisibleTo(tab)
    assert "Copies PDF" in tab.setup_summary.text()


def test_with_no_walkthrough_the_choices_show_as_before(tmp_path):
    from ui_qt.widgets import walkthrough_tab

    with patch.object(walkthrough_tab, "captures_root", return_value=tmp_path / "none"):
        tab = walkthrough_tab.WalkthroughTab()
    assert tab.review.isHidden()
    assert not tab.fills_height()
    assert not tab.setup_row.isVisibleTo(tab)
    assert tab.copy_area.isVisibleTo(tab)
    assert tab.write_up_card.isVisibleTo(tab)
    tab.close()


def test_a_finished_walkthrough_never_replaces_unsaved_edits(tab, capture):
    tab.review.rows[0].editor.setPlainText("mine")
    assert not tab.show_walkthrough(capture.folder)
    assert tab.review.rows[0].editor.toPlainText() == "mine"


def test_saving_in_the_tab_says_the_walkthrough_lists_are_stale(tab, capture):
    changed = QSignalSpy(tab.walkthroughs_changed)
    tab.review.rows[0].editor.setPlainText("fixed")
    tab.review.save_button.click()
    _wait(tab.review)
    assert len(changed) == 1
    assert tab.review.folder == capture.folder
    assert tab.review.rows[0].editor.toPlainText() == "fixed"


def test_a_side_panel_pick_opens_in_the_tab_and_open_library_opens_the_dialog(capture):
    from ui_qt.main_window import MainWindow
    from ui_qt.widgets import walkthrough_tab

    with patch("ui_qt.dialogs.meeting_intro_dialog.maybe_show_meeting_mode_intro"), \
            patch.object(walkthrough_tab, "captures_root", return_value=capture.root):
        window = MainWindow()
        try:
            library_spy = QSignalSpy(window.walkthrough_library_requested)
            window.history_sidebar.walkthrough_selected.emit(capture.folder)
            assert window.walkthrough_tab.review.folder == capture.folder
            assert len(library_spy) == 0
            window.history_sidebar.walkthrough_selected.emit(None)
            assert library_spy[0] == [None]
        finally:
            window._force_quit = True
            window.close()
