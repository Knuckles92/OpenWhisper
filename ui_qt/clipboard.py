"""Temporary clipboard ownership for auto-paste."""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from enum import Enum

from PyQt6.QtCore import QByteArray, QMimeData, QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPixmap

logger = logging.getLogger(__name__)

AUTO_PASTE_MARKER_FORMAT = "application/x-openwhisper-auto-paste"


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


class TemporaryClipboard(QObject):
    """Stages transcript text and restores a clipboard snapshot if still owned."""

    restore_failed = pyqtSignal(str)

    def __init__(self, clipboard, parent: QObject | None = None):
        super().__init__(parent)
        self._clipboard = clipboard
        self._pending: ClipboardLease | None = None
        self._restore_timer = QTimer(self)
        self._restore_timer.setSingleShot(True)
        self._restore_timer.timeout.connect(self._restore_pending)

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

        try:
            snapshot = ClipboardSnapshot.capture(self._clipboard.mimeData())
        except Exception as exc:
            logger.warning("Could not snapshot clipboard before auto-paste: %s", exc)
            return ClipboardStageResult(
                self._write_plain_text(text), restore_unavailable=True
            )

        if snapshot.is_blank:
            return ClipboardStageResult(self._write_plain_text(text))

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
        lease = self._pending
        self._restore_timer.stop()
        self._pending = None
        if lease is not None:
            self._restore(lease)

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

    def _discard_pending(self) -> None:
        self._restore_timer.stop()
        self._pending = None
