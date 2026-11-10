"""The non-modal Stats window: what you dictate, and what else you do with the app."""

from __future__ import annotations

import importlib
import itertools
import logging
import threading
from datetime import date
from typing import Optional

from PyQt6 import sip
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QBoxLayout,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.icons import tabler_icon
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets.buttons import Button, compact_primary_button, neutral_button
from ui_qt.widgets.eliding_label import ElidingLabel
from ui_qt.widgets.segmented_bar import SegmentedBar
from ui_qt.widgets.stats_charts import (
    WEEKDAYS,
    ActivityMap,
    Bar,
    BarChart,
    HourGrid,
    ShareBar,
    SplitBar,
    hour_text,
    words_text,
)
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)

#: Width of the window, at 100% font, from which the four numbers share a row
#: and sections sit side by side.
_WIDE_WIDTH = 720
#: Width from which the trend sits beside the words instead of under them.
_HERO_ROW_WIDTH = 520

#: Names for the breakdown keys the stats rows carry; "" is a row without one.
_CATEGORY_TEXT = {"email": "Email", "work": "Work", "personal": "Personal",
                  "other": "Other apps", "": "Unrecognized apps"}
_CLEANUP_TEXT = {"light": "Light", "medium": "Medium", "high": "High", "": "Off"}

#: Period keys with their switch label, hero caption and "the span before".
_PERIOD_TEXT = {
    "week": ("Week", "Last 7 days", "the 7 days before"),
    "month": ("Month", "Last 30 days", "the 30 days before"),
    "all": ("All time", "All time", ""),
}


def _stats():
    return importlib.import_module("services.dictation_stats")


def _days(count: int) -> str:
    return f"{count} day" + ("" if count == 1 else "s")


def _short_day(day: date) -> str:
    return f"{day:%b} {day.day}"


def _long_day(day: date) -> str:
    return f"{day:%b} {day.day}, {day.year}"


def _count(count: int, one: str, many: str = "") -> str:
    return f"{count:,} {one if count == 1 else many or one + 's'}"


def _language_text(code: str) -> str:
    if not code:
        return "Not set"
    try:
        return importlib.import_module("services.dictation_language").label(code)
    except Exception:
        return code


def _feature_text(feature) -> tuple[str, str]:
    """A feature row's name and its line of detail for a span with something in it."""
    key, count = feature.key, feature.count
    length = _duration(feature.seconds / 60) if feature.seconds >= 1 else ""
    if key == "meetings":
        parts = [_count(count, "meeting"), f"{length} captured" if length else ""]
        if count > 1 and length:
            parts.append(f"avg {_duration(feature.seconds / 60 / count)}")
        return "Meetings", " · ".join(part for part in parts if part)
    if key == "files":
        parts = [_count(count, "file"), f"{length} of audio" if length else "",
                 words_text(feature.words) if feature.words else ""]
        return "Audio files", " · ".join(part for part in parts if part)
    if key == "walkthroughs":
        parts = [_count(count, "walkthrough"),
                 _count(feature.shots, "screenshot") if feature.shots else ""]
        return "Walkthroughs", " · ".join(part for part in parts if part)
    if key == "actions":
        parts = [f"{count:,} sent"] + [f"{name} {times:,}" for name, times in feature.top]
        return "Actions", " · ".join(parts)
    if key == "commands":
        parts = [f"{count:,} run", f"{words_text(feature.words)} rewritten" if feature.words else ""]
        return "Commands", " · ".join(part for part in parts if part)
    if key == "snippets":
        parts = [f"{count:,} used",
                 f"wrote {words_text(feature.words)} for you" if feature.words else ""]
        return "Snippets", " · ".join(part for part in parts if part)
    return "Dictionary", f"{_count(count, 'correction')} applied"


#: Where clicking a feature's row leads, for its tooltip and screen readers.
_OPENS_TEXT = {
    "meetings": "Open Past Meetings",
    "files": "Open History on the Upload File tab",
    "walkthroughs": "Open your walkthroughs",
    "actions": "Open Actions in Settings",
    "commands": "Open Commands in Settings",
    "snippets": "Open Snippets in Settings",
    "dictionary": "Open the dictionary in Settings",
}

#: What one column of a feature's trend counts, for hover and screen readers.
_TREND_TEXT = {
    "meetings": lambda n: _count(n, "meeting"),
    "files": lambda n: _count(n, "file"),
    "walkthroughs": lambda n: _count(n, "walkthrough"),
    "actions": lambda n: f"{n:,} sent",
    "commands": lambda n: f"{n:,} run",
    "snippets": lambda n: f"{n:,} used",
    "dictionary": lambda n: _count(n, "correction"),
}


def _duration(minutes: float) -> str:
    if 0 < minutes < 1:
        return "Under a minute"
    whole = round(minutes)
    if whole < 60:
        return f"{whole} min"
    hours, rest = divmod(whole, 60)
    if hours >= 100 or not rest:
        return f"{hours:,} h"
    return f"{hours} h {rest} min"


def _trend(period, first_day: Optional[date]) -> tuple[str, str]:
    """The hero chip's text and tone: this span against the one before it."""
    if period.days is None:
        return (f"Since {_long_day(first_day)}", "flat") if first_day else ("", "")
    previous = period.previous_words
    if not previous:
        return "", ""
    change = round((period.words - previous) * 100 / previous)
    before = _PERIOD_TEXT[period.key][2]
    if change > 0:
        return f"↑ {change}% vs {before}", "up"
    if change < 0:
        return f"↓ {-change}% vs {before}", "down"
    return f"Same as {before}", "flat"


def _streak_detail(summary) -> str:
    if summary.dictated_today:
        detail = "You dictated today" if summary.day_streak == 1 else "Keep it going"
    elif summary.day_streak:
        detail = "Dictate today to keep it"
    else:
        detail = "Dictate today to start one"
    if summary.longest_streak > summary.day_streak:
        return f"{detail} · best {summary.longest_streak}"
    return detail


def _day_bars(period) -> list[Bar]:
    """A column a day; every 7th from today labelled, so 30 days stay legible."""
    last = len(period.bars) - 1
    bars = []
    for index, (day, words) in enumerate(period.bars):
        name = "Today" if index == last else f"{day:%a}, {_short_day(day)}"
        if period.days <= 7:
            axis = "Today" if index == last else f"{day:%a}"
        else:
            axis = ("Today" if index == last else _short_day(day)) if (last - index) % 7 == 0 else ""
        bars.append(Bar(name, words, axis))
    return bars


def _month_bars(period) -> list[Bar]:
    return [Bar(f"{month:%B %Y}", words, f"{month:%b}") for month, words in period.bars]


class _Relay(QObject):
    """Carries worker results to the windows; module-lived, so never deleted under a worker."""

    loaded = pyqtSignal(int, object, str)
    #: A dictation was counted; emitted on the history-save worker.
    recorded = pyqtSignal()


_relay_instance: Optional[_Relay] = None
_tokens = itertools.count(1)


def _relay() -> _Relay:
    global _relay_instance
    if _relay_instance is None:
        _relay_instance = _Relay()
        _stats().add_listener(_relay_instance.recorded.emit)
    return _relay_instance


class StatCard(QWidget):
    """One number with a caption above and a detail below."""

    def __init__(self, caption: str, parent=None):
        super().__init__(parent)
        self.setObjectName("statsStat")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(110)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(1)
        self.caption_label = ElidingLabel(caption)
        self.caption_label.setObjectName("statsCardCaption")
        column.addWidget(self.caption_label)
        self.value_label = ElidingLabel("")
        self.value_label.setObjectName("statsCardValue")
        column.addWidget(self.value_label)
        self.detail_label = ElidingLabel("")
        self.detail_label.setObjectName("statsCardDetail")
        column.addWidget(self.detail_label)
        column.addStretch()

    def set_values(self, value: str, detail: str, tooltip: str = "") -> None:
        self.value_label.setText(value)
        self.detail_label.setText(detail)
        self.setToolTip(tooltip)


def _glyph(icon: str, tone: str, size: int, icon_size: int) -> QLabel:
    glyph = QLabel()
    glyph.setObjectName("statsCardIcon")
    glyph.setProperty("tone", tone)
    glyph.setFixedSize(size, size)
    glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
    glyph.setPixmap(tabler_icon(icon).pixmap(icon_size, icon_size))
    return glyph


def _label(text: str, name: str, *, eliding: bool = True) -> QLabel:
    label = ElidingLabel(text) if eliding else QLabel(text)
    label.setObjectName(name)
    return label


def _section(title: str) -> tuple[QFrame, QVBoxLayout, QHBoxLayout]:
    """A rounded section with a heading row; returns it, its column and that row."""
    frame = QFrame()
    frame.setObjectName("statsSection")
    frame.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    column = QVBoxLayout(frame)
    column.setContentsMargins(16, 14, 16, 14)
    column.setSpacing(10)
    heading = QHBoxLayout()
    heading.setSpacing(8)
    heading.addWidget(_label(title, "statsSectionTitle", eliding=False))
    column.addLayout(heading)
    return frame, column, heading


class FeatureRow(QFrame):
    """A feature's name and detail; a click, Space or Enter opens where it lives."""

    clicked = pyqtSignal()

    def __init__(self, key: str, name: str, detail: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.setObjectName("statsFeatureRow")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(_OPENS_TEXT[key])
        self.setAccessibleName(f"{name}: {detail}")
        self.setAccessibleDescription(_OPENS_TEXT[key])
        column = QVBoxLayout(self)
        column.setContentsMargins(8, 4, 8, 4)
        column.setSpacing(1)
        line = WrappedLabel(detail)
        line.setObjectName("statsMuted")
        for label in (_label(name, "statsAppName"), line):
            # A click on the text is a click on the row.
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            column.addWidget(label)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clicked.emit()
            return
        super().keyPressEvent(event)


class _Breakdown:
    """One line of the breakdown section: a caption, its shares in words, and a bar."""

    def __init__(self, caption: str, column: QVBoxLayout):
        self.caption = caption
        self.widget = QWidget()
        self.widget.setObjectName("statsContent")
        lines = QVBoxLayout(self.widget)
        lines.setContentsMargins(0, 0, 0, 0)
        lines.setSpacing(4)
        lines.addWidget(_label(caption, "statsInsight"))
        self.legend = WrappedLabel("")
        self.legend.setObjectName("statsMuted")
        lines.addWidget(self.legend)
        self.bar = SplitBar()
        lines.addWidget(self.bar)
        column.addWidget(self.widget)

    def show(self, shares, name_of, *, least: int = 1) -> bool:
        """Draw ``shares``; hidden, and False, with fewer than ``least`` of them."""
        total = sum(words for _key, words in shares)
        shown = bool(total) and len(shares) >= least
        self.widget.setVisible(shown)
        if not shown:
            self.bar.set_parts([], self.caption)
            self.legend.setText("")
            return False
        parts = [(name_of(key), words, bool(key)) for key, words in shares]
        self.bar.set_parts(parts, self.caption)
        self.legend.setText(" · ".join(
            f"{label} {round(words * 100 / total)}%" for label, words, _named in parts
        ))
        return True


class StatsDialog(QDialog):
    """Words dictated over a chosen span, then a row for each other thing the app did."""

    #: A feature row was activated; carries its FEATURES key.
    feature_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("statsDialog")
        self.setWindowTitle("Stats")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setMinimumSize(360, 360)
        self.resize(round(640 * current_ui_font_scale()), round(720 * current_ui_font_scale()))
        self._token = 0
        self._wide: Optional[bool] = None
        self.period_key = "week"
        self.summary = None
        self.app_rows: list[tuple[str, int]] = []
        #: ``(key, name, detail)`` of the feature rows on show.
        self.feature_rows: list[tuple[str, str, str]] = []
        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.setInterval(400)
        self._refresh.timeout.connect(self.reload)
        relay = _relay()
        relay.loaded.connect(self._apply)
        relay.recorded.connect(self._on_recorded)
        self._build()

    # ---- layout ----

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName("statsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("statsBody")
        column = QVBoxLayout(body)
        column.setContentsMargins(24, 22, 24, 16)
        column.setSpacing(14)

        title = QLabel("Stats")
        title.setObjectName("statsTitle")
        column.addWidget(title)
        intro = WrappedLabel("What you do with OpenWhisper, counted and kept on this computer.")
        intro.setObjectName("statsIntro")
        column.addWidget(intro)

        self.pages = QStackedWidget()
        self.pages.setObjectName("statsPages")
        self.loading_label = QLabel("Counting your words…")
        self.loading_label.setObjectName("statsMuted")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pages.addWidget(self.loading_label)
        self.pages.addWidget(self._build_empty())
        self.pages.addWidget(self._build_content())
        # A stack is as tall as its tallest page, so the short loading and
        # empty pages sat centred in the content page's height, below the fold.
        self.pages.currentChanged.connect(self._fit_pages_to_current)
        self._fit_pages_to_current(self.pages.currentIndex())
        column.addWidget(self.pages, 1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        footer = QWidget()
        footer.setObjectName("statsFooter")
        row = QHBoxLayout(footer)
        row.setContentsMargins(24, 10, 24, 16)
        row.setSpacing(8)
        self.reset_button = neutral_button(Button("Reset stats…"))
        self.reset_button.setToolTip("Start your counts over; History and meetings stay as they are")
        self.reset_button.clicked.connect(self.confirm_reset)
        self.reset_button.setEnabled(False)
        row.addWidget(self.reset_button)
        row.addStretch()
        close = compact_primary_button(Button("Close"))
        close.clicked.connect(self.close)
        row.addWidget(close)
        outer.addWidget(footer)
        # Without a default, QDialog makes the first button in the focus chain,
        # Reset, the one that Enter clicks. Set once the button is in the dialog.
        close.setDefault(True)

    def _fit_pages_to_current(self, index: int) -> None:
        """Size the stack to the page on show; the hidden pages ask for no room."""
        for page_index in range(self.pages.count()):
            page = self.pages.widget(page_index)
            policy = (
                QSizePolicy.Policy.Preferred if page_index == index
                else QSizePolicy.Policy.Ignored
            )
            page.setSizePolicy(policy, policy)
            page.adjustSize()
        self.pages.adjustSize()

    def _build_empty(self) -> QWidget:
        page = QFrame()
        page.setObjectName("statsEmpty")
        column = QVBoxLayout(page)
        column.setContentsMargins(24, 36, 24, 36)
        column.setSpacing(8)
        column.addStretch()
        column.addWidget(_glyph("microphone-blue.svg", "blue", 40, 22), 0, Qt.AlignmentFlag.AlignHCenter)
        heading = QLabel("Nothing to count yet")
        heading.setObjectName("statsEmptyTitle")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(heading)
        self.empty_text = WrappedLabel(
            "Dictate something, record a meeting or transcribe a file, and it shows up here."
        )
        self.empty_text.setObjectName("statsMuted")
        self.empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.empty_text)
        column.addStretch()
        return page

    def _build_content(self) -> QWidget:
        page = QWidget()
        page.setObjectName("statsContent")
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)

        self.period_bar = SegmentedBar(
            [(_PERIOD_TEXT[key][0], "") for key, _days in _stats().PERIODS], compact=True
        )
        self.period_bar.setAccessibleName("Stats period")
        self.period_bar.setMaximumWidth(round(330 * current_ui_font_scale()))
        for button in self.period_bar.buttons:
            # Enter still closes the window when a period has focus.
            button.setAutoDefault(False)
        self.period_bar.activated.connect(self._choose_period)
        column.addWidget(self.period_bar)

        column.addWidget(self._build_headline())
        self.features_section = self._build_features()
        column.addWidget(self.features_section)
        self._pair_grid = QGridLayout()
        self._pair_grid.setContentsMargins(0, 0, 0, 0)
        self._pair_grid.setSpacing(14)
        self._pairs = [self._build_rhythm(), self._build_apps()]
        column.addLayout(self._pair_grid)
        self.breakdown_section = self._build_breakdown()
        column.addWidget(self.breakdown_section)
        column.addWidget(self._build_activity())
        column.addStretch()
        self._set_wide(False)
        return page

    def _build_headline(self) -> QFrame:
        """The span's words with their trend, the chart of them, and four numbers."""
        frame = QFrame()
        frame.setObjectName("statsSection")
        frame.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        column = QVBoxLayout(frame)
        column.setContentsMargins(16, 14, 16, 14)
        column.setSpacing(10)

        self._hero_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._hero_row.setSpacing(8)
        words = QVBoxLayout()
        words.setSpacing(2)
        self.hero_caption = _label("", "statsHeroCaption")
        words.addWidget(self.hero_caption)
        self.hero_value = _label("", "statsHeroValue")
        words.addWidget(self.hero_value)
        self._hero_row.addLayout(words, 1)
        chip_row = QHBoxLayout()
        chip_row.setContentsMargins(0, 0, 0, 4)
        self.trend_chip = _label("", "statsTrend", eliding=False)
        chip_row.addWidget(self.trend_chip, 0, Qt.AlignmentFlag.AlignBottom)
        chip_row.addStretch()
        self._hero_row.addLayout(chip_row)
        column.addLayout(self._hero_row)

        heading = QHBoxLayout()
        heading.setSpacing(8)
        self.chart_title = _label("Words per day", "statsInsight", eliding=False)
        heading.addWidget(self.chart_title)
        self.chart_average = _label("", "statsMuted", eliding=False)
        heading.addWidget(self.chart_average)
        self.best_label = _label("", "statsMuted")
        self.best_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        heading.addWidget(self.best_label, 1)
        column.addLayout(heading)
        self.bars = BarChart(96, tone="accent")
        column.addWidget(self.bars)

        rule = QFrame()
        rule.setObjectName("statsRule")
        rule.setFixedHeight(1)
        column.addWidget(rule)
        self.saved_card = StatCard("Time saved")
        self.saved_value = self.saved_card.value_label
        self.saved_detail = self.saved_card.detail_label
        self.pace_card = StatCard("Speaking pace")
        self.dictations_card = StatCard("Dictations")
        self.streak_card = StatCard("Day streak")
        self.cards = [self.saved_card, self.pace_card, self.dictations_card, self.streak_card]
        self._card_grid = QGridLayout()
        self._card_grid.setContentsMargins(0, 2, 0, 0)
        self._card_grid.setHorizontalSpacing(14)
        self._card_grid.setVerticalSpacing(12)
        column.addLayout(self._card_grid)
        return frame

    def _build_features(self) -> QFrame:
        frame, column, heading = _section("Across OpenWhisper")
        # The rows pad their text by 8 for the hover wash, so the section
        # gives up 8 on the left and the heading takes it back: text lines up.
        margins = column.contentsMargins()
        column.setContentsMargins(margins.left() - 8, margins.top(), margins.right(), margins.bottom())
        heading.setContentsMargins(8, 0, 0, 0)
        self.features_grid = QGridLayout()
        self.features_grid.setContentsMargins(0, 0, 0, 0)
        self.features_grid.setHorizontalSpacing(12)
        self.features_grid.setVerticalSpacing(2)
        self.features_grid.setColumnStretch(0, 1)
        column.addLayout(self.features_grid)
        return frame

    def _build_rhythm(self) -> QFrame:
        frame, column, _heading = _section("When you dictate")
        self.hour_label = _label("", "statsInsight")
        column.addWidget(self.hour_label)
        self.rhythm = HourGrid()
        column.addWidget(self.rhythm)
        column.addStretch()
        return frame

    def _build_apps(self) -> QFrame:
        frame, column, _heading = _section("Top apps")
        self.apps_grid = QGridLayout()
        self.apps_grid.setContentsMargins(0, 0, 0, 0)
        self.apps_grid.setHorizontalSpacing(10)
        self.apps_grid.setVerticalSpacing(10)
        self.apps_grid.setColumnStretch(1, 1)
        column.addLayout(self.apps_grid)
        self.no_apps_label = WrappedLabel("Apps show up here as you dictate into them.")
        self.no_apps_label.setObjectName("statsMuted")
        column.addWidget(self.no_apps_label)
        column.addStretch()
        return frame

    def _build_breakdown(self) -> QFrame:
        frame, column, heading = _section("How your words break down")
        self.cleanup_label = _label("", "statsMuted")
        self.cleanup_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.cleanup_label.setToolTip("Words AI cleanup replaced, added or removed")
        heading.addWidget(self.cleanup_label, 1)
        self.category_breakdown = _Breakdown("Kind of app", column)
        self.cleanup_breakdown = _Breakdown("AI cleanup", column)
        self.language_breakdown = _Breakdown("Language", column)
        return frame

    def _build_activity(self) -> QFrame:
        frame, column, heading = _section("Activity")
        self.active_days_label = _label("", "statsMuted")
        self.active_days_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        heading.addWidget(self.active_days_label, 1)
        self.activity = ActivityMap()
        column.addWidget(self.activity)
        milestone = QHBoxLayout()
        milestone.setSpacing(8)
        self.milestone_label = _label("", "statsHeroDetail")
        milestone.addWidget(self.milestone_label, 1)
        self.milestone_left = _label("", "statsHeroDetail", eliding=False)
        self.milestone_left.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        milestone.addWidget(self.milestone_left)
        column.addLayout(milestone)
        self.milestone_bar = ShareBar(0.0, tone="accent", height=6)
        column.addWidget(self.milestone_bar)
        return frame

    def _set_wide(self, wide: bool) -> None:
        if wide == self._wide:
            return
        self._wide = wide
        columns = 4 if wide else 2
        for card in self.cards:
            self._card_grid.removeWidget(card)
        for index, card in enumerate(self.cards):
            self._card_grid.addWidget(card, *divmod(index, columns))
        for col in range(4):
            self._card_grid.setColumnStretch(col, 1 if col < columns else 0)
        for section in self._pairs:
            self._pair_grid.removeWidget(section)
        for index, section in enumerate(self._pairs):
            self._pair_grid.addWidget(section, *((0, index) if wide else (index, 0)))
        for col in range(2):
            self._pair_grid.setColumnStretch(col, 1 if wide or col == 0 else 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        scale = current_ui_font_scale()
        self._set_wide(self.width() >= _WIDE_WIDTH * scale)
        self._hero_row.setDirection(
            QBoxLayout.Direction.LeftToRight if self.width() >= _HERO_ROW_WIDTH * scale
            else QBoxLayout.Direction.TopToBottom
        )

    # ---- data ----

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not event.spontaneous():
            self.reload()

    def _on_recorded(self) -> None:
        if self.isVisible():
            self._refresh.start()

    def reload(self) -> None:
        """Count again on a worker; an older count that finishes later is dropped."""
        self._token = next(_tokens)
        if self.summary is None:
            self.pages.setCurrentIndex(0)
        _run(lambda: _stats().load_summary(), self._token)

    def _apply(self, token: int, summary, error: str) -> None:
        if token != self._token:
            return
        if error:
            self.loading_label.setText("Stats couldn't be counted right now.")
            self.pages.setCurrentIndex(0)
            self.reset_button.setEnabled(bool(self.summary and self.summary.entries))
            return
        self.summary = summary
        self.reset_button.setEnabled(bool(summary.entries))
        if not summary.entries:
            self.pages.setCurrentIndex(1)
            return
        self._show(summary)
        self.pages.setCurrentIndex(2)

    def _choose_period(self, index: int) -> None:
        self.period_key = _stats().PERIODS[index][0]
        if self.summary is not None and self.summary.entries:
            self._show(self.summary)

    def _show(self, summary) -> None:
        period = summary.period(self.period_key)
        self._show_hero(summary, period)
        self._show_cards(summary, period)
        bars = self._show_chart(period)
        self._show_features(summary, period, bars)
        self._show_rhythm(period)
        self._show_apps(period)
        self._show_breakdown(period)
        self._show_activity(summary)

    def _show_hero(self, summary, period) -> None:
        self.hero_caption.setText(f"Dictated · {_PERIOD_TEXT[period.key][1].lower()}")
        self.hero_value.setText(words_text(period.words))
        chip, tone = _trend(period, summary.first_day)
        self.trend_chip.setText(chip)
        self.trend_chip.setVisible(bool(chip))
        set_style_property(self.trend_chip, "tone", tone or "flat")
        total = summary.words_all_time
        goal = _stats().next_milestone(total)
        self.milestone_label.setText(f"Next milestone: {goal:,} words")
        self.milestone_left.setText(f"{goal - total:,} to go")
        self.milestone_bar.set_fraction(total / goal)
        self.milestone_bar.setToolTip(f"{total:,} of {goal:,} words dictated, all time")

    def _show_cards(self, summary, period) -> None:
        typing = _stats().TYPING_WPM
        saved_tip = f"Typing these words at {typing} words a minute, less the time you spent saying them"
        if period.minutes_saved:
            self.saved_card.set_values(
                _duration(period.minutes_saved), f"vs typing at {typing} wpm", saved_tip
            )
        else:
            self.saved_card.set_values("—", "Shows after a longer dictation", saved_tip)
        if period.average_wpm:
            faster = period.average_wpm / typing
            self.pace_card.set_values(
                f"{round(period.average_wpm):,} wpm",
                f"{faster:.1f}× faster than typing" if faster >= 1.1 else "Your speaking pace",
                "Words per minute of recording, pauses included",
            )
        else:
            self.pace_card.set_values("—", "Shows after a longer dictation")
        self.dictations_card.set_values(
            f"{period.dictations:,}",
            f"{round(period.words / period.dictations):,} words each" if period.dictations
            else "None in this span",
        )
        self.streak_card.set_values(
            _days(summary.day_streak), _streak_detail(summary),
            f"Days in a row with a dictation; your longest is {_days(summary.longest_streak)}",
        )

    def _show_chart(self, period) -> list[Bar]:
        if period.days is None:
            unit, bars = "month", _month_bars(period)
            name = f"Words per month, last {len(bars)} months"
        else:
            unit, bars = "day", _day_bars(period)
            name = f"Words per day, last {_days(len(bars))}"
        self.chart_title.setText(f"Words per {unit}")
        average = sum(bar.words for bar in bars) / len(bars) if bars else 0.0
        self.chart_average.setText(f"· avg {round(average):,}" if average else "")
        self.bars.set_bars(bars, name, highlight=len(bars) - 1, average=average)
        best = max(bars, key=lambda bar: bar.words, default=None)
        self.best_label.setText(
            f"Best {unit}: {best.name} · {words_text(best.words)}" if best and best.words
            else "No words yet"
        )
        return bars

    def _show_features(self, summary, period, bars: list[Bar]) -> None:
        """A row per feature ever used: its count for the span, a detail and a trend."""
        _clear(self.features_grid)
        self.feature_rows = []
        ever = summary.period("all")
        for key in _stats().FEATURES:
            if not ever.feature(key).count:
                continue
            feature = period.feature(key)
            name, detail = _feature_text(feature)
            if not feature.count:
                detail = "None in this span"
            row = len(self.feature_rows)
            opener = FeatureRow(key, name, detail)
            opener.clicked.connect(lambda key=key: self.feature_requested.emit(key))
            self.features_grid.addWidget(opener, row, 0)
            trend = BarChart(22, tone="accent", value_text=_TREND_TEXT[key])
            trend.setFixedWidth(round(72 * current_ui_font_scale()))
            trend.set_bars(
                [Bar(bar.name, count) for bar, count in zip(bars, feature.trend)],
                f"{name} over this span",
            )
            self.features_grid.addWidget(trend, row, 1, Qt.AlignmentFlag.AlignVCenter)
            self.feature_rows.append((key, name, detail))
        self.features_section.setVisible(bool(self.feature_rows))

    def _show_rhythm(self, period) -> None:
        self.rhythm.set_grid(period.grid)
        peak = self.rhythm.peak()
        if peak is None:
            self.hour_label.setText("Nothing dictated in this span yet")
            return
        weekday, hour = peak
        self.hour_label.setText(f"Most on {WEEKDAYS[weekday]:%A}s around {hour_text(hour)}")

    def _show_apps(self, period) -> None:
        _clear(self.apps_grid)
        apps = period.top_apps
        self.app_rows = list(apps)
        peak = max((words for _name, words in apps), default=0)
        for row, (name, words) in enumerate(apps):
            label = _label(name, "statsAppName")
            label.setMinimumWidth(50)
            self.apps_grid.addWidget(label, row, 0)
            self.apps_grid.addWidget(ShareBar(words / peak if peak else 0.0, tone="accent"), row, 1,
                                     Qt.AlignmentFlag.AlignVCenter)
            count = _label(f"{words:,}", "statsAppWords", eliding=False)
            count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.apps_grid.addWidget(count, row, 2)
            share = round(words * 100 / period.words) if period.words else 0
            percent = _label(f"{share}%", "statsAppShare", eliding=False)
            percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            percent.setToolTip(f"{share}% of the words you dictated in this span")
            self.apps_grid.addWidget(percent, row, 3)
        self.apps_grid.setColumnMinimumWidth(0, round(70 * current_ui_font_scale()))
        self.apps_grid.setColumnMinimumWidth(3, self.fontMetrics().horizontalAdvance("100%"))
        self.no_apps_label.setVisible(not apps)

    def _show_breakdown(self, period) -> None:
        shown = [
            self.category_breakdown.show(
                period.categories, lambda key: _CATEGORY_TEXT.get(key, key.title())),
            self.cleanup_breakdown.show(
                period.cleanup_levels, lambda key: _CLEANUP_TEXT.get(key, key.title())),
            # One language says nothing a share bar could show.
            self.language_breakdown.show(period.languages, _language_text, least=2),
        ]
        changed = period.words_cleaned_up
        self.cleanup_label.setText(f"AI cleanup changed {words_text(changed)}" if changed else "")
        self.breakdown_section.setVisible(any(shown) or bool(changed))

    def _show_activity(self, summary) -> None:
        self.activity.set_days(summary.activity)
        active = sum(1 for _day, words in summary.activity if words)
        self.active_days_label.setText(
            f"{active} active {'day' if active == 1 else 'days'} this past year"
        )

    # ---- reset ----

    def confirm_reset(self) -> None:
        reply = QMessageBox.question(
            self,
            "Reset stats",
            "Reset your stats?\n\nEvery count here starts over. "
            "History and your meetings aren't changed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._token = next(_tokens)
        self.reset_button.setEnabled(False)

        def work():
            _stats().reset()
            return _stats().load_summary()

        _run(work, self._token)


def _clear(grid: QGridLayout) -> None:
    while grid.count():
        item = grid.takeAt(0)
        if item.widget() is not None:
            # Hidden now: switching periods draws the new rows before this is deleted.
            item.widget().hide()
            item.widget().deleteLater()


def _run(work, token: int) -> None:
    loaded = _relay().loaded

    def run() -> None:
        try:
            result, error = work(), ""
        except Exception as exc:
            logger.exception("Could not load dictation stats")
            result, error = None, str(exc) or type(exc).__name__
        loaded.emit(token, result, error)

    threading.Thread(target=run, name="stats-load", daemon=True).start()


#: The one Stats window, kept while it is closed so reopening is instant.
_window: Optional[StatsDialog] = None


def show_stats(ui) -> None:
    """Open the Stats window, or raise it when it is already open."""
    global _window
    dialog = _window
    if dialog is None or sip.isdeleted(dialog):
        dialog = _window = StatsDialog(ui.main_window)
        opener = getattr(ui, "open_stats_feature", None)
        if callable(opener):
            dialog.feature_requested.connect(opener)
    if dialog.isVisible():
        dialog.reload()
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
