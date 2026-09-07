"""Render main surfaces in a theme to PNGs for a visual pass.

Usage: python .migration_tools/shoot.py light|dark
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import patch  # noqa: E402

from PyQt6.QtCore import QPoint, QRect, QRectF  # noqa: E402
from PyQt6.QtGui import QPainter, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

theme = sys.argv[1] if len(sys.argv) > 1 else "light"
out = Path(__file__).resolve().parent / "shots" / theme
out.mkdir(parents=True, exist_ok=True)

from services.settings import UiTheme  # noqa: E402
from ui_qt.app import QtApplication  # noqa: E402

qt_app = QtApplication()
app = QApplication.instance()
qt_app.set_theme(UiTheme.LIGHT if theme == "light" else UiTheme.DARK)


def shoot(widget, name, size=None):
    if size:
        widget.resize(*size)
    widget.show()
    for _ in range(6):
        app.processEvents()
    pix = widget.grab()
    pix.save(str(out / f"{name}.png"))
    print("wrote", out / f"{name}.png", pix.width(), pix.height())


from ui_qt.loading_screen import LoadingScreen  # noqa: E402

splash = LoadingScreen()
splash.update_status("Loading application...")
shoot(splash, "00_loading")
splash.destroy()

from ui_qt.main_window import MainWindow  # noqa: E402

window = MainWindow()
window.resize(1100, 760)
window.show()
app.processEvents()
tabs = window.findChild(object, "tabbedContent") or getattr(window, "tabbed_content", None)
shoot(window, "01_main_dictation")
try:
    from ui_qt.widgets.tabbed_content import TabbedContentWidget

    tabbed = window.findChild(TabbedContentWidget)
    for idx, name in enumerate(["dictation", "upload", "meeting"]):
        tabbed.setCurrentIndex(idx)
        shoot(window, f"01_main_{idx}_{name}")
except Exception as exc:  # noqa: BLE001
    print("tabs:", exc)

# Fill the dictation transcript so the pane has content.
try:
    window.set_transcription("Hello there. This is a **sample** transcript with `code` and a [link](https://x.y).")
    shoot(window, "01_main_with_text")
except Exception as exc:  # noqa: BLE001
    print("transcript:", exc)

from ui_qt.dialogs.settings_dialog import SettingsDialog  # noqa: E402

dialog = SettingsDialog()
dialog.show()
app.processEvents()
rail = getattr(dialog, "nav", None) or getattr(dialog, "nav_rail", None)
shoot(dialog, "02_settings_general")
try:
    stack = dialog.stack if hasattr(dialog, "stack") else dialog.findChild(__import__("PyQt6.QtWidgets").QtWidgets.QStackedWidget)
    for i in range(stack.count()):
        stack.setCurrentIndex(i)
        if rail is not None and hasattr(rail, "setCurrentRow"):
            rail.setCurrentRow(i)
        shoot(dialog, f"02_settings_{i:02d}")
except Exception as exc:  # noqa: BLE001
    print("settings pages:", exc)
dialog.close()

try:
    from ui_qt.dialogs.model_manager_dialog import ModelManagerDialog

    with patch("ui_qt.dialogs.model_manager_dialog.scan_cached_models", return_value={}):
        mm = ModelManagerDialog(get_loaded_model=lambda: None, background_cache_scan=False)
        mm.show()
        app.processEvents()
        stack = mm.findChild(__import__("PyQt6.QtWidgets").QtWidgets.QStackedWidget)
        for i in range(stack.count()):
            stack.setCurrentIndex(i)
            shoot(mm, f"03_models_{i:02d}")
        mm.close()
except Exception as exc:  # noqa: BLE001
    print("model manager:", exc)

try:
    from ui_qt.dialogs.downloads_dialog import DownloadsDialog

    dd = DownloadsDialog()
    shoot(dd, "04_downloads")
    dd.close()
except Exception as exc:  # noqa: BLE001
    print("downloads:", exc)

try:
    from ui_qt.overlays.waveform_overlay import WaveformOverlay

    ov = WaveformOverlay()
    for state in ["recording", "processing", "transcribing", "cleaning", "copied", "stt_enable", "canceling"]:
        try:
            ov.set_state(getattr(ov, f"STATE_{state.upper()}"))
            ov.animation_time = 0.9
            ov.show()
            app.processEvents()
            pix = QPixmap(ov.size())
            pix.fill(__import__("PyQt6.QtCore").QtCore.Qt.GlobalColor.transparent)
            ov.render(pix)
            pix.save(str(out / f"05_overlay_{state}.png"))
        except Exception as exc:  # noqa: BLE001
            print("overlay", state, exc)
    ov.close()
except Exception as exc:  # noqa: BLE001
    print("overlay:", exc)

try:
    from ui_qt.dialogs.transcript_viewer_dialog import TranscriptViewerDialog

    tv = TranscriptViewerDialog()
    tv.set_transcript("# Title\n\nSome **prose** with `code`.\n\n> a quote\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```\ncode block\n```")
    shoot(tv, "06_viewer", (900, 600))
    tv.close()
except Exception as exc:  # noqa: BLE001
    print("viewer:", exc)

window._force_quit = True
window.close()
print("done")
