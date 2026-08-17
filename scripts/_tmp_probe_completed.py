"""Temporary probe: who caps the step list in the completed state?"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QWidget

from config import config
from ui_qt.main_window import MainWindow
from ui_qt.widgets.tabbed_content import TabbedContentWidget

STEPS = [
    {"id": "redecode", "name": "Audio Re-transcription", "status": "completed", "detail": "Done"},
    {"id": "polish", "name": "Transcript Cleanup", "status": "completed", "detail": "Done"},
    {"id": "consolidation", "name": "Summary & Action Items", "status": "completed", "detail": "Done"},
    {"id": "finalize", "name": "State Finalization", "status": "completed", "detail": "Done"},
]
STATS = {"duration_s": 150.0, "segments": 32, "words": 520, "key_points": 4, "action_items": 2, "decisions": 1}

app = QApplication.instance() or QApplication([])
win = MainWindow()
win._max_usable_height = lambda: 2000
win.tabbed_content.set_current_index(TabbedContentWidget.TAB_MEETING_MODE)
win.resize(config.MAIN_WINDOW_DEFAULT_WIDTH, config.MAIN_WINDOW_DEFAULT_HEIGHT)
win.show()
tab = win.meeting_mode_tab


def settle(n=12):
    for _ in range(n):
        app.processEvents()


settle()
tab.set_meeting_state({
    "active": False,
    "status": "ended",
    "finalization": {
        "status": "completed",
        "message": "Final insights ready - 32 segments, 4 key points, 2 action items, 1 decisions.",
        "current_step": 4,
        "total_steps": 4,
        "step_details": "All finalization passes completed successfully.",
        "steps": STEPS,
        "summary_stats": STATS,
    },
    "dashboard_available": True,
})
settle()

card = tab.finalization_card
content = tab.findChild(QWidget, "meetingModeContent")


def info(w, label):
    return (
        f"{label}: h={w.height()} hint={w.sizeHint().height()} "
        f"minHint={w.minimumSizeHint().height()} explicitMin={w.minimumHeight()} "
        f"max={w.maximumHeight()} policy_v={w.sizePolicy().verticalPolicy().name} "
        f"hfw={w.hasHeightForWidth()}"
    )


print(f"win={win.height()} page_h={tab.height()} content_h={content.height()}")
print(info(content, "content"))
print(info(card, "card"))
for i in range(card.layout.count()):
    item = card.layout.itemAt(i)
    w = item.widget()
    if w is None:
        print(f"    sub-layout item: min={item.minimumSize().height()} hint={item.sizeHint().height()}")
        continue
    print("    " + info(w, w.objectName() or type(w).__name__))
    print(
        f"        item_min={item.minimumSize().height()} item_hint={item.sizeHint().height()} "
        f"item_max={item.maximumSize().height()} hfw={item.hasHeightForWidth()}"
    )

sw = tab.finalization_steps_widget
print(info(sw, "steps widget"))
print(
    f"steps layout: hint={sw.layout().sizeHint().height()} "
    f"min={sw.layout().minimumSize().height()}"
)
for i in range(sw.layout().count()):
    row = sw.layout().itemAt(i).widget()
    print("    " + info(row, f"row{i}"))

win._force_quit = True
win.close()
app.processEvents()
