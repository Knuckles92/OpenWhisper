"""A walkthrough's screenshots while it records."""

from datetime import datetime

from PyQt6.QtGui import QColor, QImage

from services.walkthrough import session as session_module
from services.walkthrough.session import WalkthroughSession


def _image(color: str = "#ff4f3a") -> QImage:
    image = QImage(40, 30, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return image


def _session(tmp_path, made=None):
    def make_folder(started_at):
        if made is not None:
            made.append(started_at)
        folder = tmp_path / "capture"
        folder.mkdir(exist_ok=True)
        return folder

    return WalkthroughSession(started_at=datetime(2026, 10, 21, 14, 32, 5), make_folder=make_folder)


def test_screenshots_are_written_in_the_background_and_numbered(tmp_path):
    session = _session(tmp_path)

    first = session.add_shot(_image(), 12.5)
    second = session.add_shot(_image("#2f7cf6"), 31.0)
    saved = session.saved_shots()

    assert [(shot.number, shot.path.name, shot.kept_at_s) for shot in saved] == [
        (1, "shot-1.png", 12.5),
        (2, "shot-2.png", 31.0),
    ]
    assert first.path.exists() and second.path.exists()
    assert QImage(str(second.path)).pixelColor(5, 5) == QColor("#2f7cf6")
    session.end()


def test_the_folder_is_made_only_when_a_screenshot_needs_it(tmp_path):
    made = []
    session = _session(tmp_path, made)
    assert made == []

    session.add_shot(_image(), 1.0)
    session.add_shot(_image(), 2.0)

    assert made == [datetime(2026, 10, 21, 14, 32, 5)]
    session.end()


def test_a_stopped_walkthrough_takes_no_more_screenshots(tmp_path):
    session = _session(tmp_path)
    session.add_shot(_image(), 1.0)
    session.close()

    assert not session.open
    assert session.add_shot(_image(), 2.0) is None
    assert session.shot_count == 1
    session.end()


def test_a_screenshot_that_could_not_be_written_is_left_out(tmp_path, monkeypatch):
    real_write = session_module._write_png
    first = tmp_path / "capture" / "shot-1.png"

    # By path, not by call: an earlier test's writer may still be finishing.
    def flaky(image, path):
        return False if path == first else real_write(image, path)

    monkeypatch.setattr(session_module, "_write_png", flaky)
    session = _session(tmp_path)
    session.add_shot(_image(), 1.0)
    session.add_shot(_image(), 2.0)

    saved = session.saved_shots()

    assert [(shot.number, shot.path.name, shot.kept_at_s) for shot in saved] == [(1, "shot-2.png", 2.0)]
    session.end()


def test_canceling_deletes_the_screenshots_but_nothing_else(tmp_path):
    session = _session(tmp_path)
    shot = session.add_shot(_image(), 1.0)
    session.saved_shots()
    keep = tmp_path / "capture" / "notes.txt"
    keep.write_text("mine", encoding="utf-8")

    session.discard()

    assert not shot.path.exists()
    assert keep.exists()


def test_canceling_after_the_documents_are_written_deletes_them_too(tmp_path):
    session = _session(tmp_path)
    session.add_shot(_image(), 1.0)
    session.saved_shots()
    folder = tmp_path / "capture"
    for name in ("walkthrough.md", "walkthrough.pdf", "walkthrough.json", "write-up.md"):
        (folder / name).write_text("written", encoding="utf-8")

    session.discard()

    assert not folder.exists()


def test_canceling_removes_the_folder_it_made(tmp_path):
    session = _session(tmp_path)
    session.add_shot(_image(), 1.0)

    session.discard()

    assert not (tmp_path / "capture").exists()


def test_ending_keeps_the_files(tmp_path):
    session = _session(tmp_path)
    shot = session.add_shot(_image(), 1.0)
    session.saved_shots()

    session.end()

    assert shot.path.exists()
    assert not session.open


def test_a_folder_that_cannot_be_made_refuses_the_screenshot_not_the_walkthrough(tmp_path):
    import errno

    import pytest

    from services.walkthrough.session import ScreenshotNotSaved

    def read_only(started_at):
        raise PermissionError(errno.EACCES, "Permission denied")

    session = WalkthroughSession(started_at=datetime(2026, 10, 21, 14, 32, 5), make_folder=read_only)

    with pytest.raises(ScreenshotNotSaved, match="Permission denied"):
        session.add_shot(_image(), 1.0)

    assert session.open
    assert session.shot_count == 0
    assert session.saved_shots() == []
    session.end()


class _FullDiskImage:
    """Saves the way a full disk does: part of the file, then failure."""

    def copy(self):
        return self

    def save(self, path, fmt):
        with open(path, "wb") as out:
            out.write(b"\x89PNG truncated")
        return False


def test_a_screenshot_that_fails_to_write_is_reported_and_leaves_no_partial_file(tmp_path):
    failed = []
    session = _session(tmp_path)
    session.on_save_failed = failed.append

    shot = session.add_shot(_FullDiskImage(), 1.0)
    saved = session.saved_shots()

    assert saved == []
    assert failed == [1]
    assert session.lost_shots == 1
    assert not shot.path.exists()
    session.end()


# --- the board: moving and removing screenshots while it records -------------


def _three_shots(tmp_path):
    session = _session(tmp_path)
    for kept_at in (2.0, 4.0, 6.0):
        session.add_shot(_image(), kept_at)
    return session


def test_untouched_the_board_reads_each_piece_in_order(tmp_path):
    session = _three_shots(tmp_path)

    board = session.storyboard()

    assert [shot.path.name for shot in board.shots] == ["shot-1.png", "shot-2.png", "shot-3.png"]
    assert board.cuts_s == (2.0, 4.0, 6.0)
    assert board.sections == ((0,), (1,), (2,), (3,))
    session.end()


def test_a_moved_screenshot_takes_its_words_with_it(tmp_path):
    session = _three_shots(tmp_path)

    assert session.move_shot(3, -1)
    assert session.move_shot(3, -1)
    board = session.storyboard()

    assert [(shot.number, shot.path.name) for shot in board.shots] == [
        (1, "shot-3.png"), (2, "shot-1.png"), (3, "shot-2.png")]
    assert board.sections == ((2,), (0,), (1,), (3,))
    session.end()


def test_the_words_still_being_spoken_stay_last(tmp_path):
    session = _three_shots(tmp_path)

    assert not session.move_shot(3, 1)
    assert not session.move_shot(1, -1)
    assert not session.move_shot(9, 1)
    assert session.storyboard().sections == ((0,), (1,), (2,), (3,))
    session.end()


def test_a_removed_screenshot_leaves_its_words_to_the_next_section(tmp_path):
    session = _three_shots(tmp_path)

    assert session.remove_shot(2)
    board = session.storyboard()

    assert [shot.path.name for shot in board.shots] == ["shot-1.png", "shot-3.png"]
    # Its words and the next section's are one stretch: no cut between them.
    assert board.cuts_s == (2.0, 6.0)
    assert board.sections == ((0,), (1,), (2,))
    assert not (tmp_path / "capture" / "shot-2.png").exists()
    assert session.shot_count == 2
    assert not session.remove_shot(2)
    session.end()


def test_words_that_moved_away_from_their_neighbours_are_cut_apart(tmp_path):
    session = _three_shots(tmp_path)
    session.move_shot(1, 1)  # 2, 1, 3

    session.remove_shot(2)  # its words open section 1's
    board = session.storyboard()

    assert [shot.path.name for shot in board.shots] == ["shot-1.png", "shot-3.png"]
    assert board.cuts_s == (2.0, 4.0, 6.0)
    assert board.sections == ((1, 0), (2,), (3,))
    session.end()


def test_undo_takes_the_newest_screenshot_wherever_it_moved(tmp_path):
    session = _three_shots(tmp_path)
    session.move_shot(3, -1)

    assert session.undo_last() == 3
    assert session.undo_last() == 2
    board = session.storyboard()

    assert [shot.path.name for shot in board.shots] == ["shot-1.png"]
    # Their words stay in the order the board showed them: shot 3's had
    # moved above shot 2's.
    assert board.cuts_s == (2.0, 4.0, 6.0)
    assert board.sections == ((0,), (2, 1, 3))
    session.end()


def test_undoing_in_the_order_kept_rejoins_the_recording(tmp_path):
    session = _three_shots(tmp_path)

    session.undo_last()
    session.undo_last()
    board = session.storyboard()

    assert board.cuts_s == (2.0,)
    assert board.sections == ((0,), (1,))
    session.end()


def test_a_screenshot_kept_after_an_edit_closes_the_section_being_spoken(tmp_path):
    session = _three_shots(tmp_path)
    session.undo_last()

    session.add_shot(_image(), 8.0)
    board = session.storyboard()

    assert [shot.path.name for shot in board.shots] == ["shot-1.png", "shot-2.png", "shot-4.png"]
    assert board.cuts_s == (2.0, 4.0, 8.0)
    assert board.sections == ((0,), (1,), (2,), (3,))
    session.end()


def test_a_stopped_walkthrough_keeps_its_board(tmp_path):
    session = _three_shots(tmp_path)
    session.close()

    assert not session.remove_shot(1)
    assert not session.move_shot(2, -1)
    assert session.undo_last() is None
    assert session.shot_count == 3
    session.end()


def test_recovery_sees_only_the_screenshots_left_on_the_board(tmp_path):
    import json

    session = _three_shots(tmp_path)
    session.checkpoint_recording("0" * 32)
    session.remove_shot(2)
    session.storyboard()  # waits for the writer

    draft = json.loads((tmp_path / "capture" / "draft.json").read_text(encoding="utf-8"))
    assert [item["shot"] for item in draft["shots"]] == ["shot-1.png", "shot-3.png"]
    session.end()


def test_the_section_a_moment_belongs_to_follows_the_board(tmp_path):
    session = _three_shots(tmp_path)
    session.move_shot(2, -1)

    board = session.storyboard()

    assert board.section_at(1.0) == 1  # said before shot 1, which is now second
    assert board.section_at(3.0) == 0
    assert board.section_at(7.0) == 3
    session.end()
