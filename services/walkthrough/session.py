"""One walkthrough while it records: the screenshots kept so far and where they live.

A session is shared by the Qt thread, which adds a screenshot each time one
is kept, and the job worker, which reads them once the recording stops.
Screenshots are written as PNGs on the session's own thread, because
encoding a full screen can take a few hundred milliseconds; the worker
waits for them before it builds the document.

The session also keeps the board the live column shows: its sections in
order, each the words that led to a screenshot and then the screenshot.
Moving a screenshot moves its words with it, and removing one leaves its
words to the section after it. The recording itself only ever runs
forward, so the board says which pieces of it each section reads. And it
keeps the stretches the user held Hold for note: what they said then is
for the AI write-up, not the document.
"""

from __future__ import annotations

import bisect
import logging
import math
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from PyQt6.QtGui import QImage

logger = logging.getLogger(__name__)

#: How long the worker waits for the last screenshot to finish writing.
SAVE_WAIT_S = 30.0


class ScreenshotNotSaved(OSError):
    """The walkthrough's folder could not be created, so no screenshot can be kept."""


@dataclass(frozen=True)
class Shot:
    #: 1 for the first screenshot kept, and so on.
    number: int
    path: Path
    #: Seconds into the recording when the screenshot was kept.
    kept_at_s: float


@dataclass(frozen=True)
class Storyboard:
    """How a stopped walkthrough reads: its screenshots in order, and whose words are whose.

    The recording is cut at ``cuts_s`` into ``len(cuts_s) + 1`` pieces, in
    time order. Each section reads some of them: the words that led to its
    screenshot, wherever the board moved them.
    """

    #: The screenshots whose file was written, in the board's order, numbered from 1.
    shots: tuple[Shot, ...] = ()
    #: Where the recording is cut, in seconds, in time order.
    cuts_s: tuple[float, ...] = ()
    #: For each section, one per screenshot and then the words after the
    #: last: the pieces it reads, in order.
    sections: tuple[tuple[int, ...], ...] = ((0,),)
    #: The stretches held for a note, (start, end) in seconds, in time order;
    #: they are left out of every piece. A note held to the end ends at inf.
    notes_s: tuple[tuple[float, float], ...] = ()

    def section_at(self, at_s: float) -> int:
        """The section that reads the moment ``at_s`` of the recording."""
        piece = bisect.bisect_right(self.cuts_s, at_s)
        return next(index for index, pieces in enumerate(self.sections) if piece in pieces)


@dataclass(frozen=True)
class Note:
    """Words the user held for the agent: they go to the write-up, never the document."""

    text: str
    #: The section they were said in, an index into the board's sections.
    section: int


@dataclass
class _Section:
    """One section on the board while it records."""

    #: The recording's pieces it reads, in order: piece i is what was said
    #: before screenshot i (0-based, as kept) was kept, and the last piece
    #: is what is said after the last screenshot.
    pieces: list[int] = field(default_factory=list)
    #: Which screenshot (0-based, as kept) closes it; None while it is
    #: still being spoken.
    shot: Optional[int] = None


def _storyboard(board: list[_Section], shots: list[Shot],
                notes_s: tuple[tuple[float, float], ...] = ()) -> Storyboard:
    """``board`` as the pieces the recording is cut into; ``shots`` are all those kept."""
    # Pieces that follow each other in time within one section are one
    # stretch of speech: no cut between them, so no word is split there.
    joined = {a for section in board for a, b in pairwise(section.pieces) if b == a + 1}
    cuts = [index for index in range(len(shots)) if index not in joined]

    def piece(clip: int) -> int:
        return bisect.bisect_left(cuts, clip)

    sections = []
    for section in board:
        pieces: list[int] = []
        for clip in section.pieces:
            if not pieces or pieces[-1] != piece(clip):
                pieces.append(piece(clip))
        sections.append(tuple(pieces))
    on_board = [shots[section.shot] for section in board if section.shot is not None]
    return Storyboard(
        shots=tuple(Shot(number, shot.path, shot.kept_at_s)
                    for number, shot in enumerate(on_board, start=1)),
        cuts_s=tuple(shots[index].kept_at_s for index in cuts),
        sections=tuple(sections),
        notes_s=notes_s,
    )


def _remove(board: list[_Section], place: int) -> None:
    """Take the screenshot at ``place`` off ``board``; its words open the next section."""
    removed = board.pop(place)
    board[place].pieces[:0] = removed.pieces


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
    #: Screenshots kept during the recording whose file could not be written.
    lost_shots: int = 0

    @property
    def name(self) -> str:
        """What the status and the card call the document."""
        return "Write-up" if self.written_up else "Walkthrough"

    @property
    def docx(self) -> Path:
        """The same document for Word, beside ``pdf``."""
        return self.pdf.with_suffix(".docx")

    def clipboard(self, copy_format: str) -> tuple[list[Path], str]:
        """The files and the text a copy in ``copy_format`` puts on the clipboard.

        ``copy_format`` is a ``WalkthroughCopyFormat`` value.
        """
        from services.settings import WalkthroughCopyFormat

        files = {
            WalkthroughCopyFormat.PDF: [self.pdf],
            WalkthroughCopyFormat.BOTH: [self.pdf],
            WalkthroughCopyFormat.WORD: [self.docx],
        }.get(copy_format, [])
        texts = (WalkthroughCopyFormat.BOTH, WalkthroughCopyFormat.AGENT)
        return files, self.clipboard_text if copy_format in texts else ""


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
        #: Every screenshot kept, in the order kept, removed ones too: their
        #: moments still divide the recording.
        self._shots: list[Shot] = []
        self._saves: list[Future] = []
        #: The board, once the user changed it; until then it is one section
        #: per screenshot in the order kept (``_board_locked``).
        self._board: Optional[list[_Section]] = None
        #: Stretches held for a note, and when the one being held began.
        self._notes: list[tuple[float, float]] = []
        self._note_from: Optional[float] = None
        self._writer: Optional[ThreadPoolExecutor] = None
        self._open = True
        self._journal_id = ""
        #: The built document; set by the job worker before it delivers.
        self.result: Optional[WalkthroughResult] = None
        #: Why the document could not be written, when the words were kept
        #: without it; set by the job worker.
        self.document_error = ""
        #: Called on the writer thread with a screenshot's number when its
        #: file could not be written.
        self.on_save_failed: Optional[Callable[[int], None]] = None

    @property
    def open(self) -> bool:
        """Whether screenshots can still be added: the recording hasn't stopped."""
        with self._lock:
            return self._open

    def close(self) -> None:
        """Take no more screenshots; the recording stopped or was canceled.

        A note still held runs to the end of the recording.
        """
        with self._lock:
            self._open = False
            if self._note_from is not None:
                self._notes.append((self._note_from, math.inf))
                self._note_from = None

    def hold_note(self, at_s: float) -> None:
        """Hold for note was pressed ``at_s`` into the recording: what follows is for the agent."""
        with self._lock:
            if self._open and self._note_from is None:
                self._note_from = max(0.0, float(at_s))

    def release_note(self, at_s: float) -> None:
        """Hold for note was let go ``at_s`` into the recording."""
        with self._lock:
            start, self._note_from = self._note_from, None
            if start is not None and at_s > start:
                self._notes.append((start, float(at_s)))

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
        """The screenshots on the board: kept and not removed."""
        with self._lock:
            return sum(1 for section in self._board_locked() if section.shot is not None)

    def _board_locked(self) -> list[_Section]:
        if self._board is None:
            count = len(self._shots)
            self._board = [_Section([index], index) for index in range(count)]
            self._board.append(_Section([count]))
        return self._board

    def _place_locked(self, number: int) -> Optional[int]:
        """Where screenshot ``number`` (as kept) sits on the board, or None once removed."""
        return next((place for place, section in enumerate(self._board_locked())
                     if section.shot == number - 1), None)

    def remove_shot(self, number: int) -> bool:
        """Take screenshot ``number`` (as kept) off the board; its words join the next section.

        Its file is deleted once written. False once the recording stopped,
        or when it is already gone.
        """
        with self._lock:
            place = self._place_locked(number) if self._open else None
            if place is None:
                return False
            _remove(self._board_locked(), place)
            # After its own write: the writer runs one task at a time, in order.
            self._writer.submit(self._delete_shot, self._shots[number - 1])
        return True

    def undo_last(self) -> Optional[int]:
        """Take the newest screenshot still on the board off it; its number, or None."""
        with self._lock:
            kept = [section.shot for section in self._board_locked() if section.shot is not None]
            number = max(kept) + 1 if kept and self._open else None
        if number is None or not self.remove_shot(number):
            return None
        return number

    def move_shot(self, number: int, step: int) -> bool:
        """Move screenshot ``number`` (as kept) and its words ``step`` sections (-1 up, 1 down).

        The words still being spoken stay last. False when there is no room,
        once the recording stopped, or when the screenshot was removed.
        """
        with self._lock:
            board = self._board_locked()
            place = self._place_locked(number) if self._open else None
            if place is None or not 0 <= place + step < len(board) - 1:
                return False
            board[place], board[place + step] = board[place + step], board[place]
        return True

    def _delete_shot(self, shot: Shot) -> None:
        shot.path.unlink(missing_ok=True)
        self._checkpoint()

    def add_shot(self, image: "QImage", kept_at_s: float) -> Optional[Shot]:
        """Keep ``image`` as the next screenshot; None once the recording stopped.

        The PNG is written in the background; ``saved_shots`` waits for it.
        Raises ``ScreenshotNotSaved`` when the walkthrough's folder can't be
        created (the captures folder is read-only, or the disk is full); the
        recording itself is unaffected.
        """
        with self._lock:
            if not self._open:
                return None
            try:
                folder = self._folder_locked()
            except OSError as exc:
                logger.error("Could not create the walkthrough folder: %s", exc)
                raise ScreenshotNotSaved(exc.errno, exc.strerror or str(exc)) from exc
            index = len(self._shots)
            shot = Shot(index + 1, folder / f"shot-{index + 1}.png", max(0.0, float(kept_at_s)))
            self._shots.append(shot)
            if self._board is not None:
                self._board[-1].shot = index
                self._board.append(_Section([index + 1]))
            if self._writer is None:
                self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="walkthrough-shots")
            self._saves.append(self._writer.submit(self._write_shot, image.copy(), shot))
        return shot

    def checkpoint_recording(self, journal_id: str) -> None:
        """Link this draft to the recorder's crash-safe journal, off the Qt thread."""
        with self._lock:
            self._journal_id = journal_id
            self._folder_locked()
            if self._writer is None:
                self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="walkthrough-shots")
            self._writer.submit(self._checkpoint)

    def _checkpoint(self) -> None:
        from services.walkthrough.library import DRAFT_NAME, atomic_json

        with self._lock:
            folder = self._folder
            # In the order kept, which recovery needs; a removed screenshot's
            # words go to the next one's section there.
            on_board = sorted(s.shot for s in self._board_locked() if s.shot is not None)
            value = dict(version=1, started_at=self.started_at.isoformat(),
                         journal_id=self._journal_id,
                         shots=[dict(shot=self._shots[i].path.name, kept_at_s=self._shots[i].kept_at_s)
                                for i in on_board])
        if folder is not None:
            try:
                atomic_json(folder / DRAFT_NAME, value)
            except OSError:
                logger.exception("Could not checkpoint walkthrough recovery metadata")

    def preserve_audio(self, source: str) -> None:
        """Keep this recording independent of the next Quick Record's WAV."""
        import os
        import shutil

        from services.walkthrough.library import AUDIO_NAME

        folder = self.folder()
        destination = folder / AUDIO_NAME
        if Path(source).resolve() != destination.resolve():
            temporary = folder / ".audio.tmp"
            try:
                shutil.copyfile(source, temporary)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        self._checkpoint()

    def _write_shot(self, image: "QImage", shot: Shot) -> bool:
        written = _write_png(image, shot.path)
        self._checkpoint()
        if not written and self.on_save_failed is not None:
            try:
                self.on_save_failed(shot.number)
            except Exception:
                logger.exception("Could not report an unsaved walkthrough screenshot")
        return written

    @property
    def lost_shots(self) -> int:
        """Screenshots kept so far whose file could not be written."""
        with self._lock:
            saves = list(self._saves)
        return sum(
            1 for future in saves
            if future.done() and (future.exception() is not None or not future.result())
        )

    def storyboard(self, timeout: float = SAVE_WAIT_S) -> Storyboard:
        """The board once its screenshots are written; worker thread.

        A screenshot whose PNG could not be written leaves the board like a
        removed one: its words join the next section.
        """
        with self._lock:
            shots = list(self._shots)
            pending = list(zip(shots, self._saves, strict=True))
            board = [_Section(list(s.pieces), s.shot) for s in self._board_locked()]
            notes = tuple(self._notes)
        for index, (shot, future) in enumerate(pending):
            try:
                written = future.result(timeout=timeout)
            except Exception:
                logger.exception("Walkthrough screenshot %d could not be written", shot.number)
                written = False
            place = next((p for p, section in enumerate(board) if section.shot == index), None)
            if not written and place is not None:
                _remove(board, place)
        return _storyboard(board, shots, notes)

    def saved_shots(self, timeout: float = SAVE_WAIT_S) -> list[Shot]:
        """The screenshots whose PNG was written, in the board's order; worker thread."""
        return list(self.storyboard(timeout).shots)

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
                 write_up.MARKDOWN_NAME, write_up.PDF_NAME, "draft.json", "audio.wav",
                 "walkthrough.original.json")
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
        # A full disk leaves a truncated file behind; it is not a screenshot.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove the partial screenshot %s", path.name, exc_info=True)
    return written
