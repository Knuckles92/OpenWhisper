"""Qt tests for the Meeting Mode Past Meetings sidebar content."""
import json
import os
import threading
import time
from datetime import datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

from meeting.state.schema import FinalizationState, MeetingState, TopicState
from ui_qt.widgets.past_meetings_panel import (
    PastMeetingItem,
    PastMeetingsPanel,
    _format_duration,
)


def _list_cards(panel):
    cards = []
    layout = panel.meetings_list_layout
    for index in range(layout.count()):
        widget = layout.itemAt(index).widget()
        if isinstance(widget, PastMeetingItem):
            cards.append(widget)
    return cards


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
    assert cards[0].findChild(QPushButton, "pastMeetingOpenButton") is None
    QTest.mouseClick(cards[0], Qt.MouseButton.LeftButton)
    assert selected == ["m_done"]
    assert cards[0].property("selected") is True

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


def test_untitled_row_shows_topic_as_title():
    app = QApplication.instance() or QApplication([])
    row = _meeting("m_topic", title="")
    state = MeetingState(
        meeting_id="m_topic",
        title="",
        topic=TopicState(current="Quarterly roadmap"),
    )
    row["state_json"] = json.dumps(state.to_dict())
    panel = PastMeetingsPanel(meeting_provider=lambda: [row])

    panel.refresh()
    card = panel.findChild(PastMeetingItem)

    assert card is not None
    assert card.title_label.text() == "Quarterly roadmap"

    panel.deleteLater()
    app.processEvents()


def test_panel_search_filters_and_empty_copy():
    app = QApplication.instance() or QApplication([])
    planning = _meeting("m_plan", title="Planning review")
    planning["content_summary"] = {
        "has_transcript": True,
        "preview_text": "We should ship the sidebar polish next.",
    }
    standup = _meeting("m_stand", title="Standup")
    standup["content_summary"] = {
        "has_transcript": False,
        "preview_text": "",
    }
    panel = PastMeetingsPanel(meeting_provider=lambda: [planning, standup])
    panel.refresh()

    header = panel.findChild(QLabel, "sectionHeader")
    assert header is not None
    assert header.text() == "PAST MEETINGS (2)"

    copied = []
    panel.copy_transcript_requested.connect(copied.append)
    cards = panel.findChildren(PastMeetingItem)
    assert cards[0].preview_label.text().startswith("We should ship")
    cards[0].copy_transcript_requested.emit(cards[0].meeting_id)
    assert copied == ["m_plan"]

    panel.search_input.blockSignals(True)
    panel.search_input.setText("planning")
    panel.search_input.blockSignals(False)
    panel._rebuild_list()
    assert [card.meeting_id for card in _list_cards(panel)] == ["m_plan"]

    panel.search_input.blockSignals(True)
    panel.search_input.setText("no-such-meeting")
    panel.search_input.blockSignals(False)
    panel._rebuild_list()
    assert _list_cards(panel) == []
    empty = panel.findChild(QLabel, "pastMeetingsEmpty")
    assert empty is not None
    assert empty.text() == "No matching meetings"

    panel.deleteLater()
    app.processEvents()


def test_repository_refresh_runs_off_ui_thread_and_requests_one_page():
    app = QApplication.instance() or QApplication([])
    main_thread = threading.get_ident()
    called = threading.Event()

    class Repo:
        def list_past_meeting_summaries(self, *, limit, query):
            self.args = (limit, query)
            self.thread_id = threading.get_ident()
            called.set()
            row = _meeting("m_async", title="Async review")
            row["content_summary"] = {
                "has_audio": False,
                "has_transcript": False,
                "is_empty": True,
                "preview_text": "",
            }
            return [row]

    repo = Repo()
    panel = PastMeetingsPanel(repository=repo)
    panel.refresh()
    assert called.wait(1.0)
    deadline = time.monotonic() + 1.0
    while not _list_cards(panel) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)

    assert repo.args == (panel.MAX_MEETINGS + 1, "")
    assert repo.thread_id != main_thread
    assert [card.meeting_id for card in _list_cards(panel)] == ["m_async"]

    panel.deleteLater()
    app.processEvents()


def test_subminute_duration_uses_seconds_instead_of_zero_minutes():
    meeting = {
        "started_at": "2026-08-20T16:55:00Z",
        "ended_at": "2026-08-20T16:55:40Z",
        "paused_total_s": 0,
    }

    assert _format_duration(meeting) == "40 sec"
