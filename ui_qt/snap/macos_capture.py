"""Asynchronous desktop screenshots with visible controls filtered out (macOS 14+)."""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage

from ui_qt.snap import capture, macos

logger = logging.getLogger(__name__)
TIMEOUT_MS = 5000


def start(screen, keep_out, parent) -> Capture | None:
    """Return a pending capture, or None when native filtering is unavailable."""
    if not macos._on_macos():
        return None
    try:
        import ScreenCaptureKit as sck

        if not hasattr(sck, "SCScreenshotManager"):
            return None
        excluded = {
            int(macos._ns_window(window).windowNumber())
            for window in capture._top_levels(keep_out)
            if window.isVisible() and not window.isMinimized()
        }
        return Capture(sck, screen.geometry(), screen.devicePixelRatio(), excluded, parent)
    except Exception:
        logger.warning("Preparing filtered screen capture failed", exc_info=True)
        return None


def _image(cg_image) -> QImage:
    from AppKit import NSBitmapImageFileTypePNG, NSBitmapImageRep

    bitmap = NSBitmapImageRep.alloc().initWithCGImage_(cg_image)
    data = bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    return QImage.fromData(bytes(data))


class Capture(QObject):
    finished = pyqtSignal(object)
    _content_ready = pyqtSignal(object, object)
    _image_ready = pyqtSignal(object)

    def __init__(self, sck, area, scale, excluded, parent):
        super().__init__(parent)
        self._sck, self._area, self._scale = sck, area, scale
        self._excluded = excluded
        self._done = False
        # Native completions run on framework threads. All state transitions
        # and Qt timers stay on the GUI thread, including cancellation.
        self._content_ready.connect(self._capture, Qt.ConnectionType.QueuedConnection)
        self._image_ready.connect(self._complete, Qt.ConnectionType.QueuedConnection)
        self._timeout = QTimer(self, singleShot=True)
        self._timeout.timeout.connect(lambda: self._complete(None))
        self._begin = QTimer(self, singleShot=True)
        self._begin.timeout.connect(self._query)
        self._begin.start(0)

    def cancel(self):
        self._done = True
        self._begin.stop()
        self._timeout.stop()

    def _query(self):
        self._timeout.start(TIMEOUT_MS)
        try:
            self._sck.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                False, True, self._received_content
            )
        except Exception:
            logger.warning("Listing screen capture content failed", exc_info=True)
            self._complete(None)

    def _received_content(self, content, error):
        try:
            self._content_ready.emit(content, error)
        except RuntimeError:
            pass  # The session can be deleted before the native callback arrives.

    def _capture(self, content, error):
        if self._done:
            return
        if error is not None or content is None:
            self._complete(None)
            return
        try:
            area = self._area
            # SCDisplay uses the same top-left desktop coordinates as Qt,
            # including negative origins for displays above or to the left.
            display = next((item for item in content.displays()
                            if (round(item.frame().origin.x), round(item.frame().origin.y),
                                round(item.frame().size.width), round(item.frame().size.height))
                            == (area.x(), area.y(), area.width(), area.height())), None)
            if display is None:
                self._complete(None)
                return
            windows = [window for window in content.windows()
                       if int(window.windowID()) in self._excluded]
            content_filter = self._sck.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
                display, windows
            )
            config = self._sck.SCStreamConfiguration.alloc().init()
            config.setWidth_(round(area.width() * self._scale))
            config.setHeight_(round(area.height() * self._scale))
            config.setShowsCursor_(False)
            self._sck.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
                content_filter, config, self._received_image
            )
        except Exception:
            logger.warning("Capturing a filtered screenshot failed", exc_info=True)
            self._complete(None)

    def _received_image(self, cg_image, error):
        image = None
        if error is None and cg_image is not None:
            try:
                image = _image(cg_image)
            except Exception:
                logger.warning("Converting a filtered screenshot failed", exc_info=True)
        try:
            self._image_ready.emit(image)
        except RuntimeError:
            pass

    def _complete(self, image):
        if self._done:
            return
        self.cancel()
        if image is not None and not image.isNull():
            image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(image.width() / self._area.width())
        else:
            image = None
        self.finished.emit(image)
