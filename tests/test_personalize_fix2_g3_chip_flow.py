"""History card chips are measured once per change, not on every layout query."""
import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QLabel, QScrollArea, QVBoxLayout, QWidget

from services.models import TranscriptionHistory
from ui_qt.widgets import eliding_label
from ui_qt.widgets.eliding_label import ElidingLabel
from ui_qt.widgets.history_sidebar import HistoryItemWidget, _ChipFlow


@pytest.fixture
def measured(monkeypatch):
    """Counts ElidingLabel.sizeHint calls, the cost the flow should not repeat."""
    calls = []
    original = eliding_label.ElidingLabel.sizeHint

    def counting(self):
        calls.append(self)
        return original(self)

    monkeypatch.setattr(eliding_label.ElidingLabel, "sizeHint", counting)
    return calls


def _chip(text):
    chip = ElidingLabel(text)
    chip.setFont(QFont("Segoe UI", 9))
    chip.setFixedHeight(20)
    return chip


def _flow(*texts):
    return _ChipFlow([_chip(text) for text in texts])


def _hosted(widget, width):
    """``widget`` at ``width``, given exactly the height it asks for."""
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget)
    layout.addStretch(1)
    host.resize(width, 400)
    host.show()
    _settle()
    # The parentless host owns ``widget``; keep it alive as long as the widget.
    widget.test_host = host
    return host


def _settle():
    for _ in range(3):
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest.value)


def test_layout_queries_reuse_the_measurement(measured):
    flow = _flow("Polish", "✦ Medium · gpt-4o-mini", "Slack")
    wide = flow.heightForWidth(2000)
    narrow = flow.heightForWidth(120)
    assert narrow > wide
    measured.clear()
    for _ in range(5):
        assert flow.heightForWidth(2000) == wide
        assert flow.heightForWidth(120) == narrow
        flow.sizeHint()
        flow.minimumSizeHint()
    assert measured == []


def test_a_chip_that_grows_is_measured_again(measured):
    flow = _flow("Polish", "Slack")
    _hosted(flow, 200)
    line = flow.minimumSizeHint().height()
    assert flow.height() == line

    flow._chips[1].setText("Google Docs — Q3 planning notes and the long tail")
    _settle()
    assert flow.heightForWidth(200) == 2 * line + _ChipFlow.SPACING
    assert flow.height() == 2 * line + _ChipFlow.SPACING


def test_a_chip_that_grows_within_its_line_gets_the_room():
    flow = _flow("Polish", "Slack")
    _hosted(flow, 400)
    height = flow.height()

    chip = flow._chips[1]
    chip.setText("Slack huddle")
    _settle()
    assert flow.height() == height
    assert chip.width() == chip.sizeHint().width()
    assert QLabel.text(chip) == "Slack huddle"


def test_a_chip_changed_while_hidden_is_measured_again_when_shown():
    flow = _flow("Polish", "Slack")
    host = _hosted(flow, 200)
    line = flow.height()

    host.hide()
    flow._chips[1].setFont(QFont("Segoe UI", 30))
    host.show()
    _settle()
    assert flow.heightForWidth(200) > line
    assert flow.height() > line


def test_the_arrangement_matches_the_chips_size_hints():
    entry = TranscriptionHistory.create(
        text="Polished.", model="base", source_name="Polish", entry_kind="transform",
        raw_text="draft", cleanup_model="gpt-4o-mini", cleanup_level="medium",
        app_name="Google Docs — Q3 planning notes",
    )
    card = HistoryItemWidget(entry)
    host = _hosted(card, 240)
    for width in (240, 420, 900, 300):
        host.resize(width, 400)
        _settle()
        flow = card.chips
        x = y = 0
        line = max(chip.sizeHint().height() for chip in flow._chips)
        for chip in flow._chips:
            chip_width = min(chip.sizeHint().width(), flow.width())
            if x and x + chip_width > flow.width():
                x, y = 0, y + line + _ChipFlow.SPACING
            assert chip.geometry().getRect() == (x, y, chip_width, line)
            x += chip_width + _ChipFlow.SPACING
        assert flow.height() == y + line


def test_adding_a_card_to_a_long_list_does_not_remeasure_the_others(measured):
    """What every finished dictation does to an open History list."""
    def card(index):
        return HistoryItemWidget(TranscriptionHistory.create(
            text=f"Note {index}.", model="base", raw_text="note", cleanup_model="gpt-4o-mini",
            cleanup_level="medium", app_name="Slack",
        ))

    content = QWidget()
    column = QVBoxLayout(content)
    for index in range(30):
        column.addWidget(card(index))
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(content)
    area.resize(380, 900)
    area.show()
    _settle()

    measured.clear()
    column.insertWidget(0, card(30))
    _settle()
    # The new card's two chips, a few times over; not 30 cards' worth.
    assert len(measured) <= 12
