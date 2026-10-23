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
    calls = []

    def flaky(image, path):
        calls.append(path.name)
        return False if len(calls) == 1 else real_write(image, path)

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
