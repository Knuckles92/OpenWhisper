from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, QObject, QRect
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from ui_qt.snap import macos_capture


@pytest.fixture
def native(monkeypatch):
    sck = SimpleNamespace(SCShareableContent=Mock(), SCContentFilter=Mock(),
                          SCStreamConfiguration=Mock(), SCScreenshotManager=Mock())
    area = QRect(-800, -600, 800, 600)
    display = Mock()
    display.frame.return_value = SimpleNamespace(origin=SimpleNamespace(x=-800, y=-600),
                                                size=SimpleNamespace(width=800, height=600))
    excluded, other = Mock(), Mock()
    excluded.windowID.return_value = 42
    other.windowID.return_value = 43
    content = Mock()
    content.displays.return_value = [display]
    content.windows.return_value = [excluded, other]
    image = QImage(1600, 1200, QImage.Format.Format_RGB32)
    image.fill(0)
    monkeypatch.setattr(macos_capture, "_image", lambda _: image)
    parent = QObject()
    request = macos_capture.Capture(sck, area, 2, {42}, parent)
    results = []
    request.finished.connect(results.append)
    QTest.qWait(10)
    content_callback = sck.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_.call_args.args[2]
    yield SimpleNamespace(sck=sck, request=request, results=results, content=content,
                          callback=content_callback, display=display, excluded=excluded)
    parent.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_filter_excludes_only_requested_windows_and_preserves_retina_scale(native):
    worker = threading.Thread(target=native.callback, args=(native.content, None))
    worker.start()
    worker.join()
    assert not native.sck.SCScreenshotManager.mock_calls
    QApplication.processEvents()
    native.sck.SCContentFilter.alloc().initWithDisplay_excludingWindows_.assert_called_once_with(
        native.display, [native.excluded]
    )
    config = native.sck.SCStreamConfiguration.alloc().init()
    config.setWidth_.assert_called_once_with(1600)
    config.setHeight_.assert_called_once_with(1200)
    config.setShowsCursor_.assert_called_once_with(False)
    callback = native.sck.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_.call_args.args[2]
    worker = threading.Thread(target=callback, args=(object(), None))
    worker.start()
    worker.join()
    QApplication.processEvents()
    (image,) = native.results
    assert image.devicePixelRatio() == 2
    assert image.format() == QImage.Format.Format_ARGB32_Premultiplied
    assert not native.request._timeout.isActive()


@pytest.mark.parametrize("reason", ["cancel", "timeout", "deleted", "error", "screen_removed"])
def test_late_native_callbacks_are_ignored(native, reason):
    if reason == "cancel":
        native.request.cancel()
    elif reason == "timeout":
        native.request._timeout.timeout.emit()
    elif reason == "deleted":
        native.request.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    elif reason == "error":
        native.callback(None, "capture denied")
        QApplication.processEvents()
    else:
        native.content.displays.return_value = []
        native.callback(native.content, None)
        QApplication.processEvents()
    native.callback(native.content, None)
    native.request._received_image(object(), None)
    QApplication.processEvents()
    assert native.results == ([] if reason in ("cancel", "deleted") else [None])
    assert not native.sck.SCScreenshotManager.mock_calls
