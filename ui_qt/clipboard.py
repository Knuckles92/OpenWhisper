"""Temporary clipboard ownership for auto-paste."""

from __future__ import annotations

import ctypes
import logging
import os
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
    QThread,
    QTimer,
    QUrl,
    pyqtSignal,
    pyqtSlot,
    qVersion,
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

    Windows bumps ``GetClipboardSequenceNumber`` and macOS
    ``NSPasteboard.changeCount`` on every write by any process, while reads
    (even ones that make another app render a delayed format) leave them
    alone, so an unchanged number proves the clipboard still holds what an
    earlier snapshot captured. X11 and Wayland return None and always
    capture at paste time: trusting a stale snapshot would restore it over
    the user's newer copy.
    """
    if sys.platform == "darwin":
        return _mac_change_count()
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


def _mac_change_count() -> ClipboardSequenceSource | None:
    """The general pasteboard's change count, once AppKit is loaded.

    Qt's Cocoa clipboard never signals another app's copy while OpenWhisper
    is in the background, which it is during Command Mode. AppKit comes with
    pynput's pyobjc but is slow to import, so the first reads start the
    import in the background and report no count until it is ready.
    """
    if QGuiApplication.platformName() != "cocoa":
        return None

    def sequence() -> int | None:
        appkit, objc = sys.modules.get("AppKit"), sys.modules.get("objc")
        if appkit is None or objc is None:
            from services.focus_context import _mac

            _mac._appkit()
            return None
        try:
            with objc.autorelease_pool():
                return int(appkit.NSPasteboard.generalPasteboard().changeCount())
        except Exception as exc:
            logger.debug("Could not read the pasteboard change count: %s", exc)
            return None

    return sequence


_DATA_CONTROL_ENV = "QT_WAYLAND_USE_DATA_CONTROL"
#: Earlier Qt releases lack wlr-data-control or its change signal.
_DATA_CONTROL_QT = (6, 10)


def prefer_wayland_data_control() -> None:
    """Ask Qt to use wlr-data-control for the clipboard; call before the QApplication exists.

    A Wayland compositor sends the clipboard only to the focused window,
    while Command Mode, transforms and auto-paste read and restore it with
    another app focused. Through data-control, which Qt uses only when this
    variable asks for it, Qt sees every copy and writes without focus on
    compositors that offer it, such as Hyprland. A value the user set is kept.
    """
    if sys.platform.startswith("linux"):
        os.environ.setdefault(_DATA_CONTROL_ENV, "1")


def foreign_copies_visible() -> bool:
    """Whether Qt's clipboard sees other apps' copies while OpenWhisper is in the background.

    False on Wayland when Qt was not asked for data-control or is too old
    for it. Qt then keeps a dead offer that reads back empty, so a copy sent
    to read the selection would land unseen, replacing a clipboard that
    could not be put back.

    Limitation: a compositor without data-control (GNOME) leaves Qt blind
    even when asked, and Qt gives no way to tell. OpenWhisper sends a copy
    on Wayland only through Hyprland, which has it.
    """
    if not QGuiApplication.platformName().startswith("wayland"):
        return True
    try:
        requested = int(os.environ.get(_DATA_CONTROL_ENV, "").strip() or "0", 0) > 0
        version = tuple(int(part) for part in qVersion().split(".")[:2])
    except ValueError:
        return False
    return requested and version >= _DATA_CONTROL_QT


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
    return _clipboard_renderer(user32, _CF_UNICODETEXT)


def system_html_renderer() -> Callable[[], None] | None:
    """Like ``system_text_renderer``, for the rich text Qt offers as CF_HTML.

    Apps that take rich text (Office, browsers, mail) ask for "HTML Format"
    before plain text, so staged rich text is rendered too.
    """
    user32 = _windows_clipboard_user32()
    if user32 is None:
        return None
    register = user32.RegisterClipboardFormatW
    register.argtypes = [ctypes.c_wchar_p]
    register.restype = ctypes.c_uint
    # The name Qt registers for text/html; the same name gives the same id.
    html_format = register("HTML Format")
    if not html_format:
        return None
    return _clipboard_renderer(user32, html_format)


def _clipboard_renderer(user32, clipboard_format: int) -> Callable[[], None]:
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
            get_data(clipboard_format)
        finally:
            close_clipboard()

    return render


def _html_document(html: str) -> str:
    """``html`` as a document that declares UTF-8 and marks its fragment.

    CF_HTML is UTF-8, but some readers (older Office) guess the ANSI code
    page for markup without a charset and garble accents and curly quotes,
    so the tag is written the way browsers write it. The fragment markers
    keep Qt from adding its own around the whole document.
    """
    if html.lstrip()[:5].lower() == "<html":
        return html
    return (
        '<html><head><meta charset="utf-8"></head><body>'
        f"<!--StartFragment-->{html}<!--EndFragment--></body></html>"
    )


def _text_mime_data(text: str, html: str = "") -> QMimeData:
    mime_data = QMimeData()
    mime_data.setText(text or "")
    if html:
        mime_data.setHtml(_html_document(html))
    return mime_data


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
    #: Rich text staged with the transcript, kept when the paste fails.
    html: str = ""


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


#: How often a selection capture looks for the copy to land.
SELECTION_POLL_MS = 10
#: Without a change counter, every this many polls the clipboard's text is
#: compared with the original, for copies Qt does not signal.
SELECTION_TEXT_POLLS = 5


@dataclass
class _SelectionCapture:
    """One synthetic copy in flight; see TemporaryClipboard.capture_selection."""

    original: ClipboardSnapshot
    callback: Callable[[str], None]
    deadline: float
    #: Sequence number before the copy, or None without a sequence source.
    sequence: int | None
    #: Last sequence number seen after a change, to wait until it settles.
    settling: int | None = None
    #: The clipboard changed since the copy was sent.
    changed: bool = False
    #: The copy shortcut went out, so the clipboard may hold the selection.
    sent: bool = False
    polls: int = 0


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
    _selection_requested = pyqtSignal(object, object, int)

    def __init__(
        self,
        clipboard,
        parent: QObject | None = None,
        sequence_source: ClipboardSequenceSource | None = None,
        sees_foreign_copies: bool | None = None,
    ):
        """
        Args:
            clipboard: The QClipboard (or a stand-in with the same methods).
            parent: Owning QObject.
            sequence_source: Clipboard change counter used to validate a
                prefetched snapshot; defaults to the OS counter, and without
                one prefetching is disabled.
            sees_foreign_copies: Whether ``clipboard`` shows other apps'
                copies while OpenWhisper is unfocused; defaults to
                ``foreign_copies_visible()``.
        """
        super().__init__(parent)
        self._clipboard = clipboard
        self._sees_foreign_copies = (
            foreign_copies_visible() if sees_foreign_copies is None else sees_foreign_copies
        )
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
        self._render_html = system_html_renderer()
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
        self._selection: _SelectionCapture | None = None
        self._selection_timer = QTimer(self)
        self._selection_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._selection_timer.setInterval(SELECTION_POLL_MS)
        self._selection_timer.timeout.connect(self._poll_selection)
        self._selection_requested.connect(
            self._start_selection_capture, Qt.ConnectionType.QueuedConnection
        )
        data_changed = getattr(clipboard, "dataChanged", None)
        if data_changed is not None:
            # Only consulted without a sequence number (macOS, Linux).
            data_changed.connect(self._on_clipboard_data_changed)

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

    def capture_selection(
        self,
        send_copy: Callable[[], None],
        callback: Callable[[str | None], None],
        timeout_ms: int,
    ) -> None:
        """Read the focused app's selection through a copy, then put the
        user's clipboard back.

        Order: a pending restore is resolved; the user's clipboard is kept
        (the prefetch while it is provably current, else a fresh capture);
        ``send_copy()`` runs; the clipboard is watched for the copy to land;
        its text is read and the original written straight back and kept as
        the next stage's snapshot; only then ``callback(text)`` runs. Safe to
        call from any thread; the work and the callback run on the Qt thread.

        Args:
            send_copy: Sends the copy shortcut to the focused app.
            callback: Receives the selected text, or "" when the clipboard
                did not change within ``timeout_ms``, nothing could be read,
                or the copy could not be sent. None when this clipboard
                can't see other apps' copies; nothing is copied or written
                then.
            timeout_ms: How long to wait for the copy to land.
        """
        if QThread.currentThread() != self.thread():
            self._selection_requested.emit(send_copy, callback, int(timeout_ms))
            return
        self._start_selection_capture(send_copy, callback, int(timeout_ms))

    def write_text(self, text: str, html: str = "") -> bool:
        self._abort_selection_capture()
        written = self._write_text(text, html)
        if written:
            self._discard_pending()
        return written

    def stage_text(
        self, text: str, html: str | None = None, *, restore: bool = True
    ) -> ClipboardStageResult:
        """Put ``text`` on the clipboard for a paste, keeping the user's to restore.

        ``html`` is a rich-text alternative staged with ``text``: apps that
        take rich text paste it, the rest paste ``text``. With ``restore``
        off the user's clipboard is not copied, so no lease is returned and
        the text stays after the paste.
        """
        html = html or ""
        self._abort_selection_capture()
        self._resolve_pending_before_stage()
        if self._clipboard is None:
            logger.error("No Qt clipboard available")
            return ClipboardStageResult(False)
        if not restore:
            self._discard_prefetch()
            return ClipboardStageResult(self._stage_without_lease(text, html))

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
                    self._stage_without_lease(text, html), restore_unavailable=True
                )

        if snapshot.is_blank:
            return ClipboardStageResult(self._stage_without_lease(text, html))

        lease = ClipboardLease(
            snapshot=snapshot, token=secrets.token_bytes(16), html=html
        )
        mime_data = _text_mime_data(text, html)
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
        self._render_staged_text(rich=bool(html))
        return ClipboardStageResult(True, lease=lease)

    def schedule_restore(self, lease: ClipboardLease, delay_ms: int) -> bool:
        if self._pending is not lease or not self._owns(lease):
            logger.info("Clipboard changed before restoration could be scheduled")
            self._discard_pending()
            return False
        self._restore_timer.start(max(0, int(delay_ms)))
        return True

    def commit_text(self, lease: ClipboardLease, text: str) -> bool:
        """Leave ``text``, with any rich text staged alongside, on the clipboard."""
        if self._pending is not lease or not self._owns(lease):
            self._discard_pending()
            return False
        written = self._write_text(text, lease.html)
        if written:
            self._discard_pending()
        return written

    def restore_now(self, lease: ClipboardLease) -> ClipboardRestoreOutcome:
        if self._pending is lease:
            self._restore_timer.stop()
            self._pending = None
        return self._restore(lease)

    def cleanup(self) -> None:
        self._abort_selection_capture()
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
        if self._selection is not None:
            # The clipboard holds the copied selection until it is restored.
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

    @pyqtSlot(object, object, int)
    def _start_selection_capture(self, send_copy, callback, timeout_ms: int) -> None:
        if self._selection is not None:
            # A second copy now would read the first one's text.
            logger.info("Selection capture already running; ignoring another")
            self._call_back(callback, "")
            return
        if self._clipboard is None:
            self._call_back(callback, "")
            return
        if not self._sees_foreign_copies:
            logger.info("This clipboard can't see other apps' copies; not copying the selection")
            self._call_back(callback, None)
            return
        self._resolve_pending_before_stage()
        original = self._take_prefetched_snapshot()
        if original is None:
            try:
                original = ClipboardSnapshot.capture(self._clipboard.mimeData())
            except Exception as exc:
                # Without a copy of the user's clipboard it could not be put
                # back, so no copy is sent at all.
                logger.warning("Could not snapshot clipboard before copying the selection: %s", exc)
                self._call_back(callback, "")
                return
        sequence = self._sequence() if self._sequence is not None else None
        self._selection = _SelectionCapture(
            original=original,
            callback=callback,
            deadline=time.monotonic() + max(0, timeout_ms) / 1000,
            sequence=sequence,
        )
        try:
            send_copy()
        except Exception as exc:
            logger.warning("Could not send the copy shortcut: %s", exc)
            self._finish_selection("")
            return
        self._selection.sent = True
        self._selection_timer.start()

    def _poll_selection(self) -> None:
        capture = self._selection
        if capture is None:
            self._selection_timer.stop()
            return
        expired = time.monotonic() >= capture.deadline
        capture.polls += 1
        if capture.sequence is None and not capture.changed and (
            expired or capture.polls % SELECTION_TEXT_POLLS == 0
        ):
            # dataChanged alone misses other apps' copies on macOS without
            # AppKit; the text is read at a fraction of the poll rate.
            capture.changed = self._clipboard_text() != (capture.original.text or "")
        if capture.sequence is not None:
            current = self._sequence()
            if current is not None and current != capture.sequence:
                capture.changed = True
                # Apps empty the clipboard and then fill it, each a change;
                # read once the number has held still for one poll.
                settled = current == capture.settling
                capture.settling = current
                if not settled and not expired:
                    return
        if capture.changed:
            text = self._clipboard_text()
            # Without a counter the change may be the user's own clipboard
            # handed over again (a clipboard manager taking ownership), so
            # only different text ends the wait early.
            new = capture.sequence is not None or text != (capture.original.text or "")
            if (text and new) or expired:
                self._finish_selection(text)
            return
        if expired:
            self._finish_selection("")

    def _on_clipboard_data_changed(self) -> None:
        capture = self._selection
        if capture is not None and capture.sequence is None:
            capture.changed = True

    def _abort_selection_capture(self) -> None:
        """End a capture in flight with no text, putting the user's clipboard back."""
        if self._selection is not None:
            self._finish_selection("")

    def _finish_selection(self, text: str) -> None:
        capture = self._selection
        self._selection = None
        self._selection_timer.stop()
        if capture is None:
            return
        changed = capture.changed
        if not changed and capture.sequence is not None:
            current = self._sequence()
            changed = current is not None and current != capture.sequence
        elif capture.sequence is None and capture.sent:
            # Only a counter proves the copy never landed, and the text
            # compare misses one of the original's own text, so the original
            # always goes back.
            changed = True
        holds_original = True
        if changed:
            try:
                self._clipboard.setMimeData(capture.original.to_mime_data())
            except Exception as exc:
                holds_original = False
                message = str(exc) or "unknown clipboard error"
                logger.error("Failed to restore clipboard after copying the selection: %s", message)
                self.restore_failed.emit(message)
        if holds_original and self._sequence is not None:
            # The clipboard holds exactly this snapshot again, so the next
            # paste can reuse it instead of capturing the clipboard twice.
            sequence = self._sequence()
            if sequence is not None:
                self._prefetched = _PrefetchedSnapshot(capture.original, sequence)
        self._call_back(capture.callback, text)

    def _clipboard_text(self) -> str:
        try:
            mime_data = self._clipboard.mimeData()
            if mime_data is None or not mime_data.hasText():
                return ""
            return str(mime_data.text())
        except Exception as exc:
            logger.debug("Could not read the copied selection: %s", exc)
            return ""

    @staticmethod
    def _call_back(callback, text: str | None) -> None:
        try:
            callback(text)
        except Exception:
            logger.exception("Selection callback failed")

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

    def _write_text(self, text: str, html: str = "") -> bool:
        if self._clipboard is None:
            logger.error("No Qt clipboard available")
            return False
        try:
            if html:
                self._clipboard.setMimeData(_text_mime_data(text, html))
            else:
                self._clipboard.setText(text or "")
            return True
        except Exception as exc:
            logger.error("Failed to copy to clipboard: %s", exc)
            return False

    def _stage_without_lease(self, text: str, html: str) -> bool:
        written = self._write_text(text, html)
        if written:
            self._render_staged_text(rich=bool(html))
        return written

    def _render_staged_text(self, *, rich: bool = False) -> None:
        """Hand the staged text, and rich text, to Windows before the paste keystroke.

        See ``system_text_renderer``; a no-op elsewhere.
        """
        renderers = [("text", self._render_text)]
        if rich:
            renderers.append(("rich text", self._render_html))
        for name, render in renderers:
            if render is None:
                continue
            try:
                render()
            except Exception as exc:
                logger.debug("Could not pre-render the staged clipboard %s: %s", name, exc)

    def _discard_pending(self) -> None:
        self._restore_timer.stop()
        self._pending = None
