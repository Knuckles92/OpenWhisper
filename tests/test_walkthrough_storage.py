"""Walkthrough folders and screenshot files under the data root.

conftest makes each test's tmp_path its data root.
"""

from datetime import datetime
from pathlib import Path

from config import data_root
from services.walkthrough.storage import (
    captures_root,
    new_capture_folder,
    new_screenshot_path,
)

STARTED = datetime(2026, 10, 21, 14, 32, 5)


def test_captures_live_in_the_data_root(tmp_path):
    assert Path(data_root()) == tmp_path
    assert captures_root() == tmp_path / "captures"


def test_a_walkthrough_folder_is_named_for_its_start_and_created(tmp_path):
    folder = new_capture_folder(STARTED)

    assert folder == tmp_path / "captures" / "2026-10-21_14-32-05"
    assert folder.is_dir()
    assert list(folder.iterdir()) == []


def test_a_taken_name_gets_the_next_number():
    folders = [new_capture_folder(STARTED) for _ in range(3)]

    assert [folder.name for folder in folders] == [
        "2026-10-21_14-32-05",
        "2026-10-21_14-32-05-2",
        "2026-10-21_14-32-05-3",
    ]
    assert all(folder.is_dir() for folder in folders)


def test_a_file_with_the_name_is_never_written_into():
    captures_root().mkdir(parents=True)
    (captures_root() / "2026-10-21_14-32-05").write_text("someone else's")

    assert new_capture_folder(STARTED).name == "2026-10-21_14-32-05-2"


def test_a_screenshot_path_is_named_for_when_it_was_taken(tmp_path):
    path = new_screenshot_path(STARTED)

    assert path == tmp_path / "captures" / "screenshots" / "2026-10-21_14-32-05.png"
    assert path.parent.is_dir()
    assert not path.exists()


def test_a_saved_screenshot_moves_the_next_one_on():
    paths = []
    for _ in range(3):
        path = new_screenshot_path(STARTED)
        path.write_bytes(b"png")
        paths.append(path)

    assert [path.name for path in paths] == [
        "2026-10-21_14-32-05.png",
        "2026-10-21_14-32-05-2.png",
        "2026-10-21_14-32-05-3.png",
    ]


def test_an_unsaved_screenshot_path_is_offered_again():
    # Only the folder is created; the name is the caller's once it saves.
    assert new_screenshot_path(STARTED) == new_screenshot_path(STARTED)
