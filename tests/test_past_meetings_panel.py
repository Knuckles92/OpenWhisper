"""Qt tests for the Meeting Mode Past Meetings sidebar content."""
import os
from datetime import datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from ui_qt.widgets.past_meetings_panel import PastMeetingItem, PastMeetingsPanel


def _meeting(meeting_id: str, status: str = "ended", title: str = "Review"):
    started = datetime(2025, 1, 2, 9, 30)
    return {
        "id": meeting_id,
        "title": title,
        "status": status,
        "started_at": started.isoformat(),
        "ended_at": (started + timedelta(minutes=42)).isoformat(),
        "paused_total_s": 120,
    }


def test_panel_filters_live_sessions_and_emits_selected_meeting():
    app = QApplication.instance() or QApplication([])
    panel = PastMeetingsPanel(
        meeting_provider=lambda: [
            _meeting("m_live", status="active"),
            _meeting("m_done", title="Planning review"),
        ]
    )
    selected = []
    panel.meeting_selected.connect(selected.append)

    panel.refresh()
    cards = panel.findChildren(PastMeetingItem)

    assert [card.meeting_id for card in cards] == ["m_done"]
    assert cards[0].title_label.text() == "Planning review"
    assert "40 min" in cards[0].detail_label.text()
    cards[0].open_button.click()
    assert selected == ["m_done"]

    panel.deleteLater()
    app.processEvents()


def test_panel_shows_empty_state():
    app = QApplication.instance() or QApplication([])
    panel = PastMeetingsPanel(meeting_provider=lambda: [])

    panel.refresh()

    empty_label = panel.findChild(QLabel, "pastMeetingsEmpty")
    assert empty_label is not None
    assert empty_label.text() == "No past meetings yet"

    panel.deleteLater()
    app.processEvents()
