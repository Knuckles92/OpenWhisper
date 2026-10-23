"""Where walkthroughs and annotated screenshots are saved.

Everything goes in ``captures`` under the data root, beside ``meetings``: a
folder per walkthrough, named for when it started, and screenshots taken
outside a walkthrough in ``captures/screenshots``, named for when they were
taken. Names sort by time, and a second capture in the same second gets
``-2``, ``-3`` and so on rather than replacing the first.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from config import data_root

CAPTURES_FOLDER = "captures"
SCREENSHOTS_FOLDER = "screenshots"
#: The finished document in a capture folder (written by ``document``).
MARKDOWN_NAME = "walkthrough.md"
PDF_NAME = "walkthrough.pdf"
MANIFEST_NAME = "walkthrough.json"


def captures_root() -> Path:
    """``captures`` under the data root; see ``config.data_root``."""
    return Path(data_root()) / CAPTURES_FOLDER


def new_capture_folder(started_at: datetime) -> Path:
    """Create the folder for a walkthrough started at ``started_at``.

    ``captures/2026-10-21_14-32-05``, or the first free ``-2``, ``-3`` name
    after it. Each name is claimed by creating it, so two walkthroughs can
    never share a folder.
    """
    root = captures_root()
    root.mkdir(parents=True, exist_ok=True)
    return next(folder for folder in _names(root, started_at) if _claimed(folder))


def new_screenshot_path(taken_at: datetime) -> Path:
    """A free path for a screenshot taken at ``taken_at``.

    ``captures/screenshots/2026-10-21_14-32-05.png``, or the first free
    ``-2``, ``-3`` name after it. The folder is created, the file is not:
    the caller saves the image there.
    """
    folder = captures_root() / SCREENSHOTS_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    return next(path for path in _names(folder, taken_at, ".png") if not path.exists())


def _names(folder: Path, when: datetime, suffix: str = "") -> Iterator[Path]:
    """``folder/2026-10-21_14-32-05<suffix>``, then ``-2``, ``-3`` and so on."""
    stem = when.strftime("%Y-%m-%d_%H-%M-%S")
    yield folder / f"{stem}{suffix}"
    for number in itertools.count(2):
        yield folder / f"{stem}-{number}{suffix}"


def _claimed(folder: Path) -> bool:
    """Create ``folder``; False when it already exists."""
    try:
        folder.mkdir()
    except FileExistsError:
        return False
    return True
