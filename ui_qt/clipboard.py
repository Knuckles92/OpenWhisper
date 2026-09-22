"""Temporary clipboard ownership for auto-paste."""

from __future__ import annotations

import ctypes
import logging
import secrets
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from PyQt6.QtCore import (
    QByteArray,
    QMimeData,
    QObject,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QColor, QGuiApplication, QImage, QPixmap

logger = logging.getLogger(__name__)

AUTO_PASTE_MARKER_FORMAT = "application/x-openwhisper-auto-paste"

#: Delay between a recording starting and its clipboard prefetch. The capture
#: holds the Qt thread (60 ms for a 4K screenshot copied by a .NET app such as
#: ShareX, up to 150 ms when a Qt app offers it in every image format), so it
#: waits until the waveform overlay (30 fps) has painted its first frames; the
#: user goes on speaking for seconds after this.
CLIPBOARD_PREFETCH_DELAY_MS = 100

#: Returns the current clipboard change counter, or None when it is unknown.
ClipboardSequenceSource = Callable[[], Optional[int]]

_CF_UNICODETEXT = 13


def _windows_clipboard_user32():
    """user32 for clipboard calls when Qt is on the real Windows clipboard.

    Qt's offscreen platform (tests) keeps an in-process clipboard that the
    Windows clipboard never sees. The WinDLL is private, so the argtypes set
    on it cannot leak into other ``ctypes.windll.user32`` users such as the
    keyboard hook.
    """
    if sys.platform != "win32" or QGuiApplication.platformName() != "windows":
        return None
    try:
        return ctypes.WinDLL("user32")
    except OSError:
        return None


def system_clipboard_sequence() -> ClipboardSequenceSource | None:
    """Return a reader for the OS clipboard change counter, if there is one.

    Windows bumps ``GetClipboardSequenceNumber`` on every write by any
    process, while reads (even ones that make another app render a delayed
    format) leave it alone, so an unchanged number proves the clipboard still
    holds what an earlier snapshot captured. Other platforms return None and
    always capture at paste time: ``QClipboard.dataChanged`` misses other
    apps' copies on macOS until OpenWhisper is activated, and trusting a
    stale snapshot would restore it over the user's newer copy.
    """
    user32 = _windows_clipboard_user32()
    if user32 is None:
        return None
    read = user32.GetClipboardSequenceNumber
    read.argtypes = []
    read.restype = ctypes.c_uint32  # DWORD

    def sequence() -> int | None:
        # Zero means this window station has no clipboard access.
        return int(read()) or None

    return sequence


def system_text_renderer() -> Callable[[], None] | None:
    """Return a function that renders our clipboard text into Windows now.

    Qt offers clipboard data with delayed rendering, so the app a paste lands
    in has its read answered by the Qt thread, which right after the paste
    keystroke is saving history. With a 90 ms save there, a Win32 reader got
    the text 96 ms after the transcript was ready; rendered before the
    keystroke, 6 ms. Reading CF_UNICODETEXT ourselves makes Windows keep the
    rendered text, and neither bumps the sequence number nor notifies
    clipboard listeners. OLE readers (.NET, Office) still ask this thread;
    only OleFlushClipboard frees those, and it announces a second clipboard
    change to every clipboard listener.
    """
    user32 = _windows_clipboard_user32()
    if user32 is None:
        return None
    open_clipboard = user32.OpenClipboard
    open_clipboard.argtypes = [ctypes.c_void_p]
    open_clipboard.restype = ctypes.c_int
    get_data = user32.GetClipboardData
    get_data.argtypes = [ctypes.c_uint]
    get_data.restype = ctypes.c_void_p
    close_clipboard = user32.CloseClipboard
    close_clipboard.argtypes = []
    close_clipboard.restype = ctypes.c_int

    def render() -> None:
        # No retry: if another app has the clipboard open, the target just
        # asks this thread for the text as it always did.
        if not open_clipboard(None):
            return
        try:
            get_data(_CF_UNICODETEXT)
        finally:
            close_clipboard()

    return render


@dataclass(frozen=True)
class ClipboardSnapshot:
    """Deep copy of the Qt-visible system clipboard payload."""

    formats: tuple[tuple[str, bytes], ...]
    text: str | None
    html: str | None
    urls: tuple[QUrl, ...] | None
    image: QImage | None
    color: QColor | None

    @classmethod
    def capture(cls, mime_data: QMimeData | None) -> ClipboardSnapshot:
        if mime_data is None:
            return cls((), None, None, None, None, None)

        formats = tuple(
            (mime_format, bytes(mime_data.data(mime_format)))
            for mime_format in mime_data.formats()
        )
        text = str(mime_data.text()) if mime_data.hasText() else None
        html = str(mime_data.html()) if mime_data.hasHtml() else None
        urls = (
            tuple(QUrl(url) for url in mime_data.urls())
            if mime_data.hasUrls()
            else None
        )

        image = None
        if mime_data.hasImage():
            image_data = mime_data.imageData()
            if isinstance(image_data, QImage):
                image = image_data.copy()
            elif isinstance(image_data, QPixmap):
                image = image_data.toImage().copy()

        color = None
        if mime_data.hasColor():
            color_data = mime_data.colorData()
            if isinstance(color_data, QColor):
                color = QColor(color_data)

        return cls(formats, text, html, urls, image, color)

    @property
    def is_blank(self) -> bool:
        if any(
            not mime_format.lower().startswith("text/plain")
            for mime_format, _payload in self.formats
        ):
            return False
        return not (self.text or "").strip()

    def to_mime_data(self) -> QMimeData:
        mime_data = QMimeData()
        for mime_format, payload in self.formats:
            mime_data.setData(mime_format, QByteArray(payload))
        if self.text is not None:
            mime_data.setText(self.text)
        if self.html is not None:
            mime_data.setHtml(self.html)
        if self.urls is not None:
            mime_data.setUrls(list(self.urls))
        if self.image is not None:
            mime_data.setImageData(self.image.copy())
        if self.color is not None:
            mime_data.setColorData(QColor(self.color))
        return mime_data


@dataclass(frozen=True)
class ClipboardLease:
    snapshot: ClipboardSnapshot
    token: bytes


@dataclass(frozen=True)
class ClipboardStageResult:
    written: bool
    lease: ClipboardLease | None = None
    restore_unavailable: bool = False


class ClipboardRestoreOutcome(Enum):
    RESTORED = "restored"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class _PrefetchedSnapshot:
    snapshot: ClipboardSnapshot
    #: Clipboard sequence number read just before the capture began.
    sequence: int


class TemporaryClipboard(QObject):
    """Stages transcript text and restores a clipboard snapshot if still owned.

    Capturing the snapshot copies every format of the user's clipboard, and
    with a screenshot there 6 of 28 logged pastes waited 100-263 ms on it.
    ``request_prefetch`` takes it while the user is still speaking, and
    ``stage_text`` reuses it only while the clipboard provably has not changed.
    """

    restore_failed = pyqtSignal(str)
    # Queued, so hotkey threads can ask for a prefetch and the clipboard is
    # still only touched on this object's (the Qt) thread, never inline in a
    # caller that is busy starting the recorder and overlay.
    _prefetch_requested = pyqtSignal(int)
    _prefetch_discard_requested = pyqtSignal()

    def __init__(
        self,
        clipboard,
        parent: QObject | None = None,
        sequence_source: ClipboardSequenceSource | None = None,
    ):
        """
        Args:
            clipboard: The QClipboard (or a stand-in with the same methods).
            parent: Owning QObject.
            sequence_source: Clipboard change counter used to validate a
                prefetched snapshot; defaults to the OS counter, and without
                one prefetching is disabled.
        """
        super().__init__(parent)
        self._clipboard = clipboard
        self._pending: ClipboardLease | None = None
        self._restore_timer = QTimer(self)
        self._restore_timer.setSingleShot(True)
        self._restore_timer.timeout.connect(self._restore_pending)
        self._sequence = (
            sequence_source
            if sequence_source is not None
            else system_clipboard_sequence()
        )
        self._render_text = system_text_renderer()
        self._prefetched: _PrefetchedSnapshot | None = None
        self._prefetch_timer = QTimer(self)
        self._prefetch_timer.setSingleShot(True)
        self._prefetch_timer.timeout.connect(self._capture_prefetch)
        self._prefetch_requested.connect(
            self._start_prefetch, Qt.ConnectionType.QueuedConnection
        )
        self._prefetch_discard_requested.connect(
            self._discard_prefetch, Qt.ConnectionType.QueuedConnection
        )

    def request_prefetch(self, delay_ms: int = CLIPBOARD_PREFETCH_DELAY_MS) -> None:
        """Snapshot the clipboard ``delay_ms`` from now for the next stage.

        Safe to call from any thread. Replaces an earlier prefetch, and does
        nothing where clipboard changes cannot be detected.
        """
        self._prefetch_requested.emit(int(delay_ms))

    def discard_prefetch(self) -> None:
        """Drop the prefetched snapshot, or cancel one not yet taken.

        Safe to call from any thread; it is applied after any prefetch
        requested before it.
        """
        self._prefetch_discard_requested.emit()

    def write_text(self, text: str) -> bool:
        written = self._write_plain_text(text)
        if written:
            self._discard_pending()
        return written

    def stage_text(self, text: str) -> ClipboardStageResult:
        self._resolve_pending_before_stage()
        if self._clipboard is None:
            logger.error("No Qt clipboard available")
            return ClipboardStageResult(False)

        # Only after the pending restore above: rewriting the clipboard bumps
        # its sequence number, so a prefetch taken while the previous
        # transcript was staged can never pass for the user's content.
        snapshot = self._take_prefetched_snapshot()
        if snapshot is not None:
            logger.debug("Reusing the clipboard snapshot taken while recording")
        else:
            try:
                snapshot = ClipboardSnapshot.capture(self._clipboard.mimeData())
            except Exception as exc:
                logger.warning(
                    "Could not snapshot clipboard before auto-paste: %s", exc
                )
                return ClipboardStageResult(
                    self._stage_plain_text(text), restore_unavailable=True
                )

        if snapshot.is_blank:
            return ClipboardStageResult(self._stage_plain_text(text))

        lease = ClipboardLease(snapshot=snapshot, token=secrets.token_bytes(16))
        mime_data = QMimeData()
        mime_data.setText(text or "")
        mime_data.setData(AUTO_PASTE_MARKER_FORMAT, QByteArray(lease.token))
        try:
            self._clipboard.setMimeData(mime_data)
        except Exception as exc:
            logger.error("Failed to stage clipboard for auto-paste: %s", exc)
            return ClipboardStageResult(False)

        if not self._owns(lease):
            logger.error("Clipboard write could not be verified before auto-paste")
            return ClipboardStageResult(False)

        self._pending = lease
        self._render_staged_text()
        return ClipboardStageResult(True, lease=lease)

    def schedule_restore(self, lease: ClipboardLease, delay_ms: int) -> bool:
        if self._pending is not lease or not self._owns(lease):
            logger.info("Clipboard changed before restoration could be scheduled")
            self._discard_pending()
            return False
        self._restore_timer.start(max(0, int(delay_ms)))
        return True

    def commit_text(self, lease: ClipboardLease, text: str) -> bool:
        if self._pending is not lease or not self._owns(lease):
            self._discard_pending()
            return False
        written = self._write_plain_text(text)
        if written:
            self._discard_pending()
        return written

    def restore_now(self, lease: ClipboardLease) -> ClipboardRestoreOutcome:
        if self._pending is lease:
            self._restore_timer.stop()
            self._pending = None
        return self._restore(lease)

    def cleanup(self) -> None:
        self._discard_prefetch()
        lease = self._pending
        self._restore_timer.stop()
        self._pending = None
        if lease is not None:
            self._restore(lease)

    @pyqtSlot(int)
    def _start_prefetch(self, delay_ms: int) -> None:
        # Never let a previous recording's snapshot survive into this one.
        self._discard_prefetch()
        if self._sequence is None or self._clipboard is None:
            return
        self._prefetch_timer.start(max(0, delay_ms))

    def _capture_prefetch(self) -> None:
        if self._pending is not None:
            # The previous transcript is staged until its restore runs, so
            # the clipboard is not the user's yet; stage_text will capture.
            logger.debug("Skipped clipboard prefetch while a restore is pending")
            return
        sequence = self._sequence()
        if sequence is None:
            return
        started = time.perf_counter()
        try:
            snapshot = ClipboardSnapshot.capture(self._clipboard.mimeData())
        except Exception as exc:
            logger.debug("Clipboard prefetch failed; auto-paste will capture: %s", exc)
            return
        self._prefetched = _PrefetchedSnapshot(snapshot, sequence)
        logger.debug(
            "Prefetched clipboard snapshot in %.0f ms",
            (time.perf_counter() - started) * 1000,
        )

    @pyqtSlot()
    def _discard_prefetch(self) -> None:
        self._prefetch_timer.stop()
        self._prefetched = None

    def _take_prefetched_snapshot(self) -> ClipboardSnapshot | None:
        """Consume the prefetch; return it only if the clipboard is unchanged."""
        prefetched = self._prefetched
        self._discard_prefetch()
        if prefetched is None:
            return None
        if self._sequence is None or self._sequence() != prefetched.sequence:
            logger.debug("Clipboard changed since the prefetch; capturing it again")
            return None
        return prefetched.snapshot

    def _restore_pending(self) -> None:
        lease = self._pending
        self._pending = None
        if lease is not None:
            self._restore(lease)

    def _resolve_pending_before_stage(self) -> None:
        lease = self._pending
        if lease is None:
            return
        self._restore_timer.stop()
        self._pending = None
        self._restore(lease)

    def _restore(self, lease: ClipboardLease) -> ClipboardRestoreOutcome:
        if not self._owns(lease):
            logger.info("Clipboard changed; skipped auto-paste restoration")
            return ClipboardRestoreOutcome.SKIPPED
        try:
            self._clipboard.setMimeData(lease.snapshot.to_mime_data())
            logger.info("Clipboard restored after auto-paste")
            return ClipboardRestoreOutcome.RESTORED
        except Exception as exc:
            message = str(exc) or "unknown clipboard error"
            logger.error("Failed to restore clipboard after auto-paste: %s", message)
            self.restore_failed.emit(message)
            return ClipboardRestoreOutcome.FAILED

    def _owns(self, lease: ClipboardLease) -> bool:
        if self._clipboard is None:
            return False
        try:
            mime_data = self._clipboard.mimeData()
            if mime_data is None or not mime_data.hasFormat(AUTO_PASTE_MARKER_FORMAT):
                return False
            return bytes(mime_data.data(AUTO_PASTE_MARKER_FORMAT)) == lease.token
        except Exception:
            return False

    def _write_plain_text(self, text: str) -> bool:
        if self._clipboard is None:
            logger.error("No Qt clipboard available")
            return False
        try:
            self._clipboard.setText(text or "")
            return True
        except Exception as exc:
            logger.error("Failed to copy to clipboard: %s", exc)
            return False

    def _stage_plain_text(self, text: str) -> bool:
        written = self._write_plain_text(text)
        if written:
            self._render_staged_text()
        return written

    def _render_staged_text(self) -> None:
        """Hand the staged text to Windows before the paste keystroke goes out.

        See ``system_text_renderer``; a no-op elsewhere.
        """
        if self._render_text is None:
            return
        try:
            self._render_text()
        except Exception as exc:
            logger.debug("Could not pre-render the staged clipboard text: %s", exc)

    def _discard_pending(self) -> None:
        self._restore_timer.stop()
        self._pending = None
