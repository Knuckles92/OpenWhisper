"""Opt-in real-desktop smoke test (not run by pytest).

Run from the checkout with its Python on macOS, Windows, X11, or Hyprland. This briefly
covers the selected screen with a test card, captures/annotates that card, then
checks review and PDF generation. It never records the microphone, contacts an
AI provider, changes settings, or keeps screenshots of the user's desktop.
Use OPENWHISPER_DATA_DIR pointing at a temporary folder when testing remotely.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QColor, QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from services.walkthrough import finish, library
from services.walkthrough.session import WalkthroughSession
from ui_qt.snap import affinity, support
from ui_qt.snap.session import start_snap


def main():
    app = QApplication.instance() or QApplication([])
    supported, reason = support.snap_support()
    if not supported:
        raise RuntimeError(reason)
    card = QWidget()
    card.setWindowTitle("OpenWhisper desktop smoke test")
    card.setStyleSheet("background: #194754; color: white; font-size: 32px;")
    layout = QVBoxLayout(card)
    label = QLabel("OpenWhisper\nWalkthrough capture test")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(label)
    # A bare X server need not have a window manager to honor fullscreen.
    card.setGeometry(app.primaryScreen().geometry())
    card.showFullScreen()
    card.activateWindow()
    QTest.qWait(1000)
    marker = QWidget(card)
    marker.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                          | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
    marker.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    marker.setStyleSheet("background: #ff00ff;")
    area = card.geometry()
    marker.setGeometry(area.x() + area.width() // 4 - 30,
                       area.y() + area.height() // 4 - 30, 60, 60)
    marker.show()
    QTest.qWait(300)
    # Exercise the same exclusion decision as the recording overlay. Merely
    # checking a successful native API call misses macOS accepting the legacy
    # sharing flag while still capturing the window's pixels.
    excluded = affinity.keep_out_of_screenshots(marker, True)
    snap = None
    real_overlays = []
    overlay_samples = []
    try:
        if app.platformName() == "cocoa":
            from ui_qt.snap import macos
            from ui_qt.overlays.waveform_overlay import WaveformOverlay
            from ui_qt.widgets.walkthrough_panel import WalkthroughPanel
            from ui_qt.widgets.walkthrough_ready import WalkthroughReadyCard

            assert macos.make_nonactivating(marker), "Could not create a non-activating panel"
            previous = macos.set_level(marker, macos.PARKED_LEVEL)
            assert previous is not None, "Could not park above the snap overlay"
            assert macos.set_level(marker, previous) == macos.PARKED_LEVEL
            pill = WaveformOverlay()
            pill.show_at_cursor(pill.STATE_RECORDING)
            pill.set_walkthrough_controls(True)
            column = WalkthroughPanel()
            column.begin()
            ready = WalkthroughReadyCard()
            real_overlays.extend((pill, column, ready))
            for widget in (column, ready):
                assert not macos._ns_window(widget).hidesOnDeactivate(), (
                    f"{type(widget).__name__} hides while the user works in another app")
            QTest.qWait(300)
            # Translucent animated windows take longer to disappear than the
            # solid marker. Check their old positions in the kept pixels too.
            for widget in (pill, column):
                for x, y in ((40, 20), (widget.width() - 40, widget.height() - 30)):
                    overlay_samples.append((widget.x() - area.x() + x,
                                            widget.y() - area.y() + y))
        # Keep the capture deferred until signals are connected. The test card
        # stays visible throughout; no desktop pixels are saved to disk.
        snap = start_snap(keep_out=(marker, *real_overlays), hint="Desktop smoke test",
                          float_above=(real_overlays[0],) if real_overlays else ())
        kept, failed = [], []
        snap.kept.connect(kept.append)
        snap.failed.connect(failed.append)
        for _ in range(100):
            QTest.qWait(30)
            if failed or snap._overlay is not None:
                break
        if failed:
            raise RuntimeError(failed[0])
        assert snap._overlay is not None, "Capture did not open the annotation overlay"
        assert marker.isVisible(), "Excluded overlay was not restored"
        # Exercise annotation and the actual Enter-to-keep event path.
        overlay = snap._overlay
        QTest.mouseDClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
        QTest.keyClick(overlay, Qt.Key.Key_B)
        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
        move = QMouseEvent(QEvent.Type.MouseMove, QPointF(200, 160),
                           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(overlay, move)
        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(200, 160))
        assert len(overlay.annotations) == 1, "Box annotation was not kept"
        QTest.keyClick(overlay, Qt.Key.Key_Return)
        QTest.qWait(200)
        assert kept and not kept[0].isNull(), "No image kept"
        image = kept[0]
        expected = QColor("#194754")
        # A solid card proves the capture came from the test, not a stale frame
        # or XWayland's empty root. Avoid the text in its centre.
        sample = image.pixelColor(image.width() // 4, image.height() // 4)
        assert sum(abs(sample.getRgb()[i] - expected.getRgb()[i]) for i in range(3)) < 20, (
            f"Unexpected captured card colour: {sample.name()}")
        for x, y in overlay_samples:
            colour = image.pixelColor(round(x * image.devicePixelRatio()),
                                      round(y * image.devicePixelRatio()))
            assert sum(abs(colour.getRgb()[i] - expected.getRgb()[i]) for i in range(3)) < 20, (
                f"A walkthrough overlay entered the capture: {colour.name()}")
        with tempfile.TemporaryDirectory(prefix="openwhisper-desktop-smoke-") as root:
            folder = Path(root) / "2026-10-21_14-32-05"
            folder.mkdir()
            session = WalkthroughSession(started_at=datetime(2026, 10, 21, 14, 32, 5),
                                          make_folder=lambda _: folder)
            session.add_shot(image, 1.0)
            result = finish.build_documents(session, session.saved_shots(),
                                             ["Test capture", "Finished"],
                                             ["Test capture", "Finished"], duration_s=2.0)
            session.end()
            assert result.pdf.read_bytes().startswith(b"%PDF")
            reviewed = library.save_review(folder, ["Reviewed capture", "Finished"], [True, False])
            assert "Reviewed capture" in reviewed.clipboard_text
            assert reviewed.shots == 1
        print(json.dumps(dict(platform=app.platformName(),
                              capture="grim" if support.uses_grim() else f"Qt {app.platformName()}",
                              overlay_exclusion="native" if excluded else "hide before grab",
                              width=image.width(), height=image.height(),
                              ratio=image.devicePixelRatio(), pixel=sample.name(),
                              capture_keep_review_pdf="passed")))
    finally:
        if snap is not None and snap.active:
            snap.drop()
        for widget in real_overlays:
            widget.close()
            widget.deleteLater()
        marker.close()
        card.close()
        app.processEvents()


if __name__ == "__main__":
    main()
