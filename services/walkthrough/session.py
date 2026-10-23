"""One walkthrough while it records: the screenshots kept so far and where they live.

A session is shared by the Qt thread, which adds a screenshot each time one
is kept, and the job worker, which reads them once the recording stops.
Screenshots are written as PNGs on the session's own thread, because
encoding a full screen can take a few hundred milliseconds; the worker
waits for them before it builds the document.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from PyQt6.QtGui import QImage

logger = logging.getLogger(__name__)

#: How long the worker waits for the last screenshot to finish writing.
SAVE_WAIT_S = 30.0


@dataclass(frozen=True)
class Shot:
    #: 1 for the first screenshot kept, and so on.
    number: int
    path: Path
    #: Seconds into the recording when the screenshot was kept.
    kept_at_s: float


@dataclass(frozen=True)
class WalkthroughResult:
    """A finished walkthrough's document, as delivered and saved."""

    folder: Path
    pdf: Path
    markdown: Path
    #: The Markdown with absolute image paths: what a terminal agent gets.
    clipboard_text: str
    #: The words with a marker where each screenshot sits, for History.
    history_text: str
    #: The same before AI cleanup, or None when cleanup changed nothing.
    history_raw: Optional[str]
    shots: int
    duration_s: float
    #: The first screenshot, for the card that says it's ready.
    first_shot: Optional[Path]
    #: Whether ``pdf`` and ``markdown`` are Claude Code's write-up rather
    #: than the words as spoken.
    written_up: bool = False
    #: Why a write-up that was asked for isn't there, or "".
    write_up_error: str = ""
    #: 1-based sections whose transcript was much shorter than their audio.
    short_sections: tuple[int, ...] = ()

    @property
    def name(self) -> str:
        """What the status and the card call the document."""
        return "Write-up" if self.written_up else "Walkthrough"


def _default_folder(started_at: datetime) -> Path:
    from services.walkthrough.storage import new_capture_folder

    return new_capture_folder(started_at)


class WalkthroughSession:
    """The screenshots of a walkthrough being recorded; thread-safe."""

    def __init__(self, *, started_at: Optional[datetime] = None,
                 make_folder: Callable[[datetime], Path] = _default_folder):
        self.started_at = started_at or datetime.now()
        self._make_folder = make_folder
        self._lock = threading.Lock()
        self._folder: Optional[Path] = None
        self._shots: list[Shot] = []
        self._saves: list[Future] = []
        self._writer: Optional[ThreadPoolExecutor] = None
        self._open = True
        #: The built document; set by the job worker before it delivers.
        self.result: Optional[WalkthroughResult] = None

    @property
    def open(self) -> bool:
        """Whether screenshots can still be added: the recording hasn't stopped."""
        with self._lock:
            return self._open

    def close(self) -> None:
        """Take no more screenshots; the recording stopped or was canceled."""
        with self._lock:
            self._open = False

    def folder(self) -> Path:
        """The walkthrough's folder, created the first time it is needed."""
        with self._lock:
            return self._folder_locked()

    def _folder_locked(self) -> Path:
        if self._folder is None:
            self._folder = self._make_folder(self.started_at)
        return self._folder

    @property
    def shot_count(self) -> int:
        with self._lock:
            return len(self._shots)

    def add_shot(self, image: "QImage", kept_at_s: float) -> Optional[Shot]:
        """Keep ``image`` as the next screenshot; None once the recording stopped.

        The PNG is written in the background; ``saved_shots`` waits for it.
        """
        with self._lock:
            if not self._open:
                return None
            shot = Shot(len(self._shots) + 1, self._folder_locked() / f"shot-{len(self._shots) + 1}.png",
                        max(0.0, float(kept_at_s)))
            self._shots.append(shot)
            if self._writer is None:
                self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="walkthrough-shots")
            self._saves.append(self._writer.submit(_write_png, image.copy(), shot.path))
        return shot

    def saved_shots(self, timeout: float = SAVE_WAIT_S) -> list[Shot]:
        """The screenshots whose PNG was written, renumbered in order; worker thread."""
        with self._lock:
            pending = list(zip(self._shots, self._saves, strict=True))
        saved = []
        for shot, future in pending:
            try:
                written = future.result(timeout=timeout)
            except Exception:
                logger.exception("Walkthrough screenshot %d could not be written", shot.number)
                written = False
            if written:
                saved.append(Shot(len(saved) + 1, shot.path, shot.kept_at_s))
        return saved

    def discard(self) -> None:
        """Delete what the walkthrough wrote, and its folder if that empties it.

        The user canceled. A Cancel during the write-up comes after the
        as-spoken documents are written, so they go with the screenshots.
        """
        from services.walkthrough import document, write_up

        self.close()
        with self._lock:
            writer, self._writer = self._writer, None
            shots, folder = list(self._shots), self._folder
        if writer is not None:
            writer.shutdown(wait=True)
        if folder is None:
            return
        names = (document.MARKDOWN_NAME, document.PDF_NAME, document.MANIFEST_NAME,
                 write_up.MARKDOWN_NAME, write_up.PDF_NAME)
        for path in [shot.path for shot in shots] + [folder / name for name in names]:
            path.unlink(missing_ok=True)
        try:
            folder.rmdir()
        except OSError:
            logger.debug("Walkthrough folder kept: it isn't empty", exc_info=True)

    def end(self) -> None:
        """Let the writer thread go once the screenshots are written; files stay."""
        self.close()
        with self._lock:
            writer, self._writer = self._writer, None
        if writer is not None:
            writer.shutdown(wait=False)


def _write_png(image: "QImage", path: Path) -> bool:
    written = image.save(str(path), "PNG")
    if not written:
        logger.error("Could not write walkthrough screenshot %s", path.name)
    return written
