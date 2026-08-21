"""Qt tests for the Meeting Mode Past Meetings sidebar content."""
import json
import os
from datetime import datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from meeting.state.schema import FinalizationState, MeetingState
from ui_qt.widgets.past_meetings_panel import (
    PastMeetingItem,
    PastMeetingsPanel,
    _format_duration,
)


def _meeting(
    meeting_id: str,
    status: str = "ended",
    title: str = "Review",
    *,
    finalization=None,
    cloud_enabled=True,
):
    started = datetime(2025, 1, 2, 9, 30)
    row = {
        "id": meeting_id,
        "title": title,
        "status": status,
        "started_at": started.isoformat(),
        "ended_at": (started + timedelta(minutes=42)).isoformat(),
        "paused_total_s": 120,
        "cloud_enabled": cloud_enabled,
    }
    if finalization is not None:
        state = MeetingState(
            meeting_id=meeting_id,
            status=status,
            cloud_enabled=cloud_enabled,
            title=title,
            finalization=finalization,
        )
        row["state_json"] = json.dumps(state.to_dict())
    return row


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


def test_panel_shows_insights_pills():
    app = QApplication.instance() or QApplication([])
    panel = PastMeetingsPanel(
        meeting_provider=lambda: [
            _meeting(
                "m_saved",
                title="Deferred review",
                finalization=FinalizationState(
                    status="failed",
                    message="interrupted",
                    card_deferred=True,
                ),
            ),
            _meeting(
                "m_ready",
                title="Ready review",
                finalization=FinalizationState(status="completed", message="done"),
            ),
        ]
    )

    panel.refresh()
    cards = panel.findChildren(PastMeetingItem)

    assert [card.meeting_id for card in cards] == ["m_saved", "m_ready"]
    assert cards[0].insights_pill.text() == "Saved for later"
    assert cards[1].insights_pill.text() == "Ready"
    assert "40 min" in cards[0].detail_label.text()
    assert "Ended" not in cards[0].detail_label.text()

    panel.deleteLater()
    app.processEvents()


def test_failed_and_empty_meetings_are_labeled_honestly():
    app = QApplication.instance() or QApplication([])
    failed = _meeting("m_failed", status="failed", title="")
    failed["content_summary"] = {
        "is_empty": True,
        "has_audio": False,
        "has_transcript": False,
    }
    panel = PastMeetingsPanel(meeting_provider=lambda: [failed])

    panel.refresh()
    card = panel.findChild(PastMeetingItem)

    assert card is not None
    assert card.title_label.text() == "Failed meeting"
    assert card.content_label.text() == "Meeting failed to start"
    assert "Failed" in card.detail_label.text()
    assert card.insights_pill.text() == "Failed start"

    panel.deleteLater()
    app.processEvents()


def test_subminute_duration_uses_seconds_instead_of_zero_minutes():
    meeting = {
        "started_at": "2026-08-20T16:55:00Z",
        "ended_at": "2026-08-20T16:55:40Z",
        "paused_total_s": 0,
    }

    assert _format_duration(meeting) == "40 sec"
