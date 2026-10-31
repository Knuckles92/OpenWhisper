"""The non-modal Stats window: how much you dictate, how fast, when and where."""

from __future__ import annotations

import importlib
import itertools
import logging
import threading
from datetime import date
from typing import Optional

from PyQt6 import sip
from PyQt6.QtCore import QLocale, QObject, Qt, QTimer, pyqtSignal
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
    BADGE_TONES,
    ActivityMap,
    AppBadge,
    Bar,
    BarChart,
    ShareBar,
    words_text,
)
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)

#: Width of the window, at 100% font, from which cards share a row and
#: sections sit side by side.
_WIDE_WIDTH = 720
#: Width from which the hero keeps time saved beside the words.
_HERO_ROW_WIDTH = 520

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


def _hour(hour: int) -> str:
    return f"{(hour % 12) or 12} {'AM' if hour % 24 < 12 else 'PM'}"


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


class StatCard(QFrame):
    """One number with a tinted icon, a caption above and a detail below."""

    def __init__(self, caption: str, icon: str, tone: str, parent=None):
        super().__init__(parent)
        self.setObjectName("statsCard")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(140)
        column = QVBoxLayout(self)
        column.setContentsMargins(14, 12, 14, 13)
        column.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(_glyph(icon, tone, 26, 15))
        self.caption_label = ElidingLabel(caption)
        self.caption_label.setObjectName("statsCardCaption")
        top.addWidget(self.caption_label, 1)
        column.addLayout(top)
        column.addSpacing(8)

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


class StatsDialog(QDialog):
    """Words, time saved, pace, streak, when and where you dictate, over a chosen span."""

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
        intro = WrappedLabel("How much you dictate, how fast, when and where. Kept on this computer.")
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
        self.reset_button.setToolTip("Start your counts over; History stays as it is")
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
            "Dictate something and your words, pace and favorite apps show up here."
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

        column.addWidget(self._build_hero())

        self.pace_card = StatCard("Speaking pace", "microphone-blue.svg", "blue")
        self.dictations_card = StatCard("Dictations", "notes-blue.svg", "blue")
        self.cleanup_card = StatCard("Cleaned up by AI", "wand-purple.svg", "purple")
        self.streak_card = StatCard("Day streak", "bolt-green.svg", "green")
        self.cards = [self.pace_card, self.dictations_card, self.cleanup_card, self.streak_card]
        self._card_grid = QGridLayout()
        self._card_grid.setContentsMargins(0, 0, 0, 0)
        self._card_grid.setHorizontalSpacing(10)
        self._card_grid.setVerticalSpacing(10)
        column.addLayout(self._card_grid)

        column.addWidget(self._build_chart())
        self._pair_grid = QGridLayout()
        self._pair_grid.setContentsMargins(0, 0, 0, 0)
        self._pair_grid.setSpacing(14)
        self._pairs = [self._build_rhythm(), self._build_apps()]
        column.addLayout(self._pair_grid)
        column.addWidget(self._build_activity())
        column.addStretch()
        self._set_wide(False)
        return page

    def _build_hero(self) -> QFrame:
        hero = QFrame()
        hero.setObjectName("statsHero")
        hero.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        column = QVBoxLayout(hero)
        column.setContentsMargins(20, 18, 20, 16)
        column.setSpacing(6)

        self._hero_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._hero_row.setSpacing(14)
        words = QVBoxLayout()
        words.setSpacing(4)
        self.hero_caption = _label("", "statsHeroCaption")
        words.addWidget(self.hero_caption)
        self.hero_value = _label("", "statsHeroValue")
        words.addWidget(self.hero_value)
        chip_row = QHBoxLayout()
        chip_row.setContentsMargins(0, 2, 0, 0)
        self.trend_chip = _label("", "statsTrend", eliding=False)
        chip_row.addWidget(self.trend_chip)
        chip_row.addStretch()
        words.addLayout(chip_row)
        self._hero_row.addLayout(words, 3)

        saved = QVBoxLayout()
        saved.setSpacing(4)
        saved.addWidget(_label("Time saved", "statsHeroCaption"))
        self.saved_value = _label("", "statsHeroSaved")
        saved.addWidget(self.saved_value)
        self.saved_detail = _label("", "statsHeroDetail")
        saved.addWidget(self.saved_detail)
        saved.addStretch()
        self._hero_row.addLayout(saved, 2)
        column.addLayout(self._hero_row)
        column.addSpacing(10)

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
        return hero

    def _build_chart(self) -> QFrame:
        frame, column, heading = _section("Words per day")
        self.chart_title = heading.itemAt(0).widget()
        self.chart_average = _label("", "statsMuted", eliding=False)
        heading.addWidget(self.chart_average)
        self.best_label = _label("", "statsMuted")
        self.best_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        heading.addWidget(self.best_label, 1)
        self.bars = BarChart(110, tone="accent")
        column.addWidget(self.bars)
        return frame

    def _build_rhythm(self) -> QFrame:
        frame, column, _heading = _section("When you dictate")
        self.hour_label = _label("", "statsInsight")
        column.addWidget(self.hour_label)
        self.hour_chart = BarChart(54, tone="warning")
        column.addWidget(self.hour_chart)
        column.addSpacing(4)
        self.weekday_label = _label("", "statsInsight")
        column.addWidget(self.weekday_label)
        self.weekday_chart = BarChart(54, tone="purple")
        column.addWidget(self.weekday_chart)
        column.addStretch()
        return frame

    def _build_apps(self) -> QFrame:
        frame, column, _heading = _section("Top apps")
        self.apps_grid = QGridLayout()
        self.apps_grid.setContentsMargins(0, 0, 0, 0)
        self.apps_grid.setHorizontalSpacing(10)
        self.apps_grid.setVerticalSpacing(10)
        self.apps_grid.setColumnStretch(2, 1)
        column.addLayout(self.apps_grid)
        self.no_apps_label = WrappedLabel("Apps show up here as you dictate into them.")
        self.no_apps_label.setObjectName("statsMuted")
        column.addWidget(self.no_apps_label)
        column.addStretch()
        return frame

    def _build_activity(self) -> QFrame:
        frame, column, heading = _section("Activity")
        self.active_days_label = _label("", "statsMuted")
        self.active_days_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        heading.addWidget(self.active_days_label, 1)
        self.activity = ActivityMap()
        column.addWidget(self.activity)
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
        self._show_chart(period)
        self._show_rhythm(period)
        self._show_apps(period)
        self._show_activity(summary)

    def _show_hero(self, summary, period) -> None:
        self.hero_caption.setText(_PERIOD_TEXT[period.key][1])
        self.hero_value.setText(words_text(period.words))
        chip, tone = _trend(period, summary.first_day)
        self.trend_chip.setText(chip)
        self.trend_chip.setVisible(bool(chip))
        set_style_property(self.trend_chip, "tone", tone or "flat")
        typing = _stats().TYPING_WPM
        if period.minutes_saved:
            self.saved_value.setText(_duration(period.minutes_saved))
            self.saved_detail.setText(f"vs typing at {typing} wpm")
        else:
            self.saved_value.setText("—")
            self.saved_detail.setText("Shows after a longer dictation")
        self.saved_value.setToolTip(
            f"Typing these words at {typing} words a minute, less the time you spent saying them"
        )
        total = summary.words_all_time
        goal = _stats().next_milestone(total)
        self.milestone_label.setText(f"Next milestone: {goal:,} words")
        self.milestone_left.setText(f"{goal - total:,} to go")
        self.milestone_bar.set_fraction(total / goal)
        self.milestone_bar.setToolTip(f"{total:,} of {goal:,} words, all time")

    def _show_cards(self, summary, period) -> None:
        typing = _stats().TYPING_WPM
        if period.average_wpm:
            faster = period.average_wpm / typing
            self.pace_card.set_values(
                f"{round(period.average_wpm):,} wpm",
                f"{faster:.1f}× faster than typing" if faster >= 1.1 else "Your speaking pace",
                "Words per minute of recording, pauses included",
            )
        else:
            self.pace_card.set_values("—", "Shows after a longer dictation")
        parts = []
        if period.dictations:
            parts.append(f"{round(period.words / period.dictations):,} words each")
        if period.rewrites:
            parts.append(f"{period.rewrites:,} command" + ("" if period.rewrites == 1 else "s"))
        self.dictations_card.set_values(
            f"{period.dictations:,}", " · ".join(parts) or "None in this span",
            "Dictations, and the commands that rewrote selected text",
        )
        self.cleanup_card.set_values(
            words_text(period.words_cleaned_up), "Words AI cleanup changed",
            "Words AI cleanup replaced, added or removed",
        )
        self.streak_card.set_values(
            _days(summary.day_streak), _streak_detail(summary),
            f"Days in a row with a dictation; your longest is {_days(summary.longest_streak)}",
        )

    def _show_chart(self, period) -> None:
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

    def _show_rhythm(self, period) -> None:
        hours = period.hours
        self.hour_chart.set_bars(
            [Bar(f"{_hour(h)} – {_hour(h + 1)}", words, _hour(h) if h % 6 == 0 else "")
             for h, words in enumerate(hours)],
            "Words by hour of the day",
        )
        start = QLocale().firstDayOfWeek().value - 1
        order = [(start + offset) % 7 for offset in range(7)]
        names = [date(2026, 10, 5 + weekday) for weekday in order]  # Oct 5, 2026 was a Monday.
        self.weekday_chart.set_bars(
            [Bar(f"{day:%A}", period.weekdays[weekday], f"{day:%a}")
             for weekday, day in zip(order, names)],
            "Words by day of the week",
        )
        if not period.words:
            self.hour_label.setText("Nothing dictated in this span yet")
            self.weekday_label.setText("")
            return
        peak_hour = max(range(24), key=lambda h: hours[h])
        self.hour_label.setText(f"Most active around {_hour(peak_hour)}")
        peak_day = max(range(7), key=lambda d: period.weekdays[d])
        self.weekday_label.setText(f"Busiest on {date(2026, 10, 5 + peak_day):%A}s")

    def _show_apps(self, period) -> None:
        while self.apps_grid.count():
            item = self.apps_grid.takeAt(0)
            if item.widget() is not None:
                # Hidden now: switching periods draws the new rows before this is deleted.
                item.widget().hide()
                item.widget().deleteLater()
        apps = period.top_apps
        self.app_rows = list(apps)
        peak = max((words for _name, words in apps), default=0)
        for row, (name, words) in enumerate(apps):
            tone = BADGE_TONES[row % len(BADGE_TONES)]
            self.apps_grid.addWidget(AppBadge(name, tone), row, 0)
            label = _label(name, "statsAppName")
            label.setMinimumWidth(50)
            self.apps_grid.addWidget(label, row, 1)
            self.apps_grid.addWidget(ShareBar(words / peak if peak else 0.0, tone=tone), row, 2,
                                     Qt.AlignmentFlag.AlignVCenter)
            count = _label(f"{words:,}", "statsAppWords", eliding=False)
            count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.apps_grid.addWidget(count, row, 3)
            share = round(words * 100 / period.words) if period.words else 0
            percent = _label(f"{share}%", "statsAppShare", eliding=False)
            percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            percent.setToolTip(f"{share}% of the words you dictated in this span")
            self.apps_grid.addWidget(percent, row, 4)
        self.apps_grid.setColumnMinimumWidth(1, round(70 * current_ui_font_scale()))
        self.apps_grid.setColumnMinimumWidth(4, self.fontMetrics().horizontalAdvance("100%"))
        self.no_apps_label.setVisible(not apps)

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
            "Reset your stats?\n\nYour word counts, pace and streak start over. "
            "History isn't changed.",
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
    if dialog.isVisible():
        dialog.reload()
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
