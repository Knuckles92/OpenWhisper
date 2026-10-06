"""The non-modal Stats window: how much you dictate, and where."""

from __future__ import annotations

import importlib
import logging
import threading
from datetime import date
from typing import Optional

from PyQt6 import sip
from PyQt6.QtCore import QObject, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QPainter, QPainterPath
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from services.desktop_session import use_omarchy_ui
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.icons import tabler_icon
from ui_qt.utils.palette import token_color
from ui_qt.widgets.buttons import Button, compact_primary_button, neutral_button
from ui_qt.widgets.eliding_label import ElidingLabel
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)

#: Width of the card grid, at 100% font, from which the four cards share a row.
_ONE_ROW_WIDTH = 720


def _stats():
    return importlib.import_module("services.dictation_stats")


def _words(count: int) -> str:
    return f"{count:,} word" + ("" if count == 1 else "s")


def _days(count: int) -> str:
    return f"{count} day" + ("" if count == 1 else "s")


def _short_day(day: date) -> str:
    return f"{day:%b} {day.day}"


class _Delivery(QObject):
    """Carries a worker's result to the window; outlives it if it closes first."""

    loaded = pyqtSignal(int, object, str)


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
        glyph = QLabel()
        glyph.setObjectName("statsCardIcon")
        glyph.setProperty("tone", tone)
        glyph.setFixedSize(26, 26)
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        glyph.setPixmap(tabler_icon(icon).pixmap(15, 15))
        top.addWidget(glyph)
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


class DailyBars(QWidget):
    """Words per day as rounded bars, oldest on the left and today in full colour."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("statsDailyBars")
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(round(96 * current_ui_font_scale()))
        self._days: tuple[tuple[date, int], ...] = ()

    @property
    def days(self) -> tuple[tuple[date, int], ...]:
        return self._days

    def set_days(self, days) -> None:
        self._days = tuple(days)
        self.update()

    def _slots(self) -> list[QRectF]:
        count = len(self._days)
        if not count:
            return []
        gap = 6.0 if self.width() > 260 else 3.0
        width = max(2.0, (self.width() - gap * (count - 1)) / count)
        return [
            QRectF(index * (width + gap), 0, width, self.height()) for index in range(count)
        ]

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        peak = max((words for _day, words in self._days), default=0)
        radius = 1.0 if use_omarchy_ui() else 4.0
        last = len(self._days) - 1
        for index, (slot, (_day, words)) in enumerate(zip(self._slots(), self._days)):
            if words and peak:
                height = max(6.0, slot.height() * words / peak)
                colour = token_color("accent", None if index == last else 120)
            else:
                height = 4.0
                colour = token_color("overlay-rgb", 30)
            bar = QRectF(slot.left(), slot.bottom() - height, slot.width(), height)
            path = QPainterPath()
            path.addRoundedRect(bar, min(radius, bar.width() / 2), min(radius, height / 2))
            painter.fillPath(path, colour)
        painter.end()

    def mouseMoveEvent(self, event) -> None:
        position = event.position()
        for slot, (day, words) in zip(self._slots(), self._days):
            if slot.left() <= position.x() <= slot.right():
                label = "Today" if day == self._days[-1][0] else f"{day:%a}, {_short_day(day)}"
                QToolTip.showText(event.globalPosition().toPoint(), f"{label} · {_words(words)}", self)
                return
        QToolTip.hideText()


class _AppBar(QWidget):
    """A thin bar for an app's share of the busiest app's words."""

    def __init__(self, fraction: float, parent=None):
        super().__init__(parent)
        self.setFixedHeight(5)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.fraction = max(0.0, min(1.0, fraction))

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        radius = 1.0 if use_omarchy_ui() else rect.height() / 2
        track = QPainterPath()
        track.addRoundedRect(rect, radius, radius)
        painter.fillPath(track, token_color("overlay-rgb", 22))
        if self.fraction:
            filled = QRectF(rect.left(), rect.top(), max(rect.height(), rect.width() * self.fraction),
                            rect.height())
            path = QPainterPath()
            path.addRoundedRect(filled, radius, radius)
            painter.fillPath(path, token_color("success", 210))
        painter.end()


class StatsDialog(QDialog):
    """Words this week and all time, pace, AI edits, streak, apps and 14 days."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("statsDialog")
        self.setWindowTitle("Stats")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setMinimumSize(360, 360)
        self.resize(round(600 * current_ui_font_scale()), round(640 * current_ui_font_scale()))
        self._generation = 0
        self._columns = 0
        self.summary = None
        self.app_rows: list[tuple[str, int]] = []
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
        intro = WrappedLabel("How much you dictate, how fast, and where. Kept on this computer.")
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

    def _build_empty(self) -> QWidget:
        page = QFrame()
        page.setObjectName("statsEmpty")
        column = QVBoxLayout(page)
        column.setContentsMargins(24, 36, 24, 36)
        column.setSpacing(8)
        column.addStretch()
        glyph = QLabel()
        glyph.setObjectName("statsCardIcon")
        glyph.setProperty("tone", "blue")
        glyph.setFixedSize(40, 40)
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        glyph.setPixmap(tabler_icon("microphone-blue.svg").pixmap(22, 22))
        column.addWidget(glyph, 0, Qt.AlignmentFlag.AlignHCenter)
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

        self.week_card = StatCard("This week", "notes-blue.svg", "blue")
        self.pace_card = StatCard("Words per minute", "microphone-blue.svg", "blue")
        self.cleanup_card = StatCard("Cleaned up by AI", "wand-purple.svg", "purple")
        self.streak_card = StatCard("Day streak", "bolt-green.svg", "green")
        self.cards = [self.week_card, self.pace_card, self.cleanup_card, self.streak_card]
        self._card_grid = QGridLayout()
        self._card_grid.setContentsMargins(0, 0, 0, 0)
        self._card_grid.setHorizontalSpacing(10)
        self._card_grid.setVerticalSpacing(10)
        column.addLayout(self._card_grid)
        self._place_cards(2)

        days = QFrame()
        days.setObjectName("statsSection")
        days_column = QVBoxLayout(days)
        days_column.setContentsMargins(16, 14, 16, 12)
        days_column.setSpacing(10)
        heading = QHBoxLayout()
        heading.setSpacing(8)
        days_title = QLabel("Last 14 days")
        days_title.setObjectName("statsSectionTitle")
        heading.addWidget(days_title)
        self.best_day_label = ElidingLabel("")
        self.best_day_label.setObjectName("statsMuted")
        self.best_day_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        heading.addWidget(self.best_day_label, 1)
        days_column.addLayout(heading)
        self.bars = DailyBars()
        days_column.addWidget(self.bars)
        axis = QHBoxLayout()
        self.first_day_label = QLabel("")
        self.first_day_label.setObjectName("statsAxis")
        axis.addWidget(self.first_day_label)
        axis.addStretch()
        today = QLabel("Today")
        today.setObjectName("statsAxis")
        axis.addWidget(today)
        days_column.addLayout(axis)
        column.addWidget(days)

        apps = QFrame()
        apps.setObjectName("statsSection")
        apps_column = QVBoxLayout(apps)
        apps_column.setContentsMargins(16, 14, 16, 14)
        apps_column.setSpacing(10)
        apps_title = QLabel("Top apps")
        apps_title.setObjectName("statsSectionTitle")
        apps_column.addWidget(apps_title)
        self.apps_grid = QGridLayout()
        self.apps_grid.setContentsMargins(0, 0, 0, 0)
        self.apps_grid.setHorizontalSpacing(12)
        self.apps_grid.setVerticalSpacing(8)
        self.apps_grid.setColumnStretch(1, 1)
        apps_column.addLayout(self.apps_grid)
        self.no_apps_label = WrappedLabel(
            "Apps show up here as you dictate into them."
        )
        self.no_apps_label.setObjectName("statsMuted")
        apps_column.addWidget(self.no_apps_label)
        column.addWidget(apps)
        column.addStretch()
        return page

    def _place_cards(self, columns: int) -> None:
        if columns == self._columns:
            return
        self._columns = columns
        for card in self.cards:
            self._card_grid.removeWidget(card)
        for index, card in enumerate(self.cards):
            row, col = divmod(index, columns)
            self._card_grid.addWidget(card, row, col)
        for col in range(4):
            self._card_grid.setColumnStretch(col, 1 if col < columns else 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        wide = self.width() >= _ONE_ROW_WIDTH * current_ui_font_scale()
        self._place_cards(4 if wide else 2)

    # ---- data ----

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not event.spontaneous():
            self.reload()

    def reload(self) -> None:
        """Count again on a worker; an older count that finishes later is dropped."""
        self._generation += 1
        generation = self._generation
        if self.summary is None:
            self.pages.setCurrentIndex(0)
        delivery = _Delivery()
        delivery.loaded.connect(self._apply)
        _run(lambda: _stats().load_summary(), delivery, generation)

    def _apply(self, generation: int, summary, error: str) -> None:
        if generation != self._generation:
            return
        if error:
            self.loading_label.setText("Stats couldn't be counted right now.")
            self.pages.setCurrentIndex(0)
            return
        self.summary = summary
        self.reset_button.setEnabled(bool(summary.dictations))
        if not summary.dictations:
            self.pages.setCurrentIndex(1)
            return
        self._show(summary)
        self.pages.setCurrentIndex(2)

    def _show(self, summary) -> None:
        self.week_card.set_values(
            _words(summary.words_this_week),
            f"{summary.words_all_time:,} all time",
            "Words you dictated in the last 7 days",
        )
        if summary.average_wpm:
            self.pace_card.set_values(
                f"{round(summary.average_wpm):,} wpm", "Your speaking pace",
                "Words per minute of recording, pauses included",
            )
        else:
            self.pace_card.set_values("—", "Shows after a longer dictation")
        self.cleanup_card.set_values(
            _words(summary.words_cleaned_up), "Words AI cleanup changed",
            "Words AI cleanup replaced, added or removed",
        )
        if summary.dictated_today:
            streak_detail = "You dictated today" if summary.day_streak == 1 else "Keep it going"
        elif summary.day_streak:
            streak_detail = "Dictate today to keep it"
        else:
            streak_detail = "Dictate today to start one"
        self.streak_card.set_values(
            _days(summary.day_streak), streak_detail, "Days in a row with a dictation",
        )

        days = summary.daily_words
        self.bars.set_days(days)
        if days:
            self.first_day_label.setText(_short_day(days[0][0]))
            best_day, best = max(days, key=lambda item: (item[1], item[0]))
            self.best_day_label.setText(
                f"Best day: {_short_day(best_day)} · {_words(best)}" if best else "No words yet"
            )
        self._show_apps(summary.top_apps)

    def _show_apps(self, apps) -> None:
        while self.apps_grid.count():
            item = self.apps_grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.app_rows = list(apps)
        peak = max((words for _name, words in apps), default=0)
        for row, (name, words) in enumerate(apps):
            label = ElidingLabel(name)
            label.setObjectName("statsAppName")
            label.setMinimumWidth(60)
            self.apps_grid.addWidget(label, row, 0)
            self.apps_grid.addWidget(_AppBar(words / peak if peak else 0.0), row, 1,
                                     Qt.AlignmentFlag.AlignVCenter)
            count = QLabel(f"{words:,}")
            count.setObjectName("statsAppWords")
            count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.apps_grid.addWidget(count, row, 2)
        self.apps_grid.setColumnMinimumWidth(0, round(110 * current_ui_font_scale()))
        self.no_apps_label.setVisible(not apps)

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
        self._generation += 1
        generation = self._generation
        self.reset_button.setEnabled(False)
        delivery = _Delivery()
        delivery.loaded.connect(self._after_reset)

        def work():
            _stats().reset()
            return _stats().load_summary()

        _run(work, delivery, generation)

    def _after_reset(self, generation: int, summary, error: str) -> None:
        if error:
            self.reset_button.setEnabled(True)
        self._apply(generation, summary, error)


def _run(work, delivery: _Delivery, generation: int) -> None:
    def run() -> None:
        try:
            result, error = work(), ""
        except Exception as exc:
            logger.exception("Could not load dictation stats")
            result, error = None, str(exc) or type(exc).__name__
        try:
            delivery.loaded.emit(generation, result, error)
        except RuntimeError:
            pass  # The window closed for good while this ran.

    # The closure keeps ``delivery`` alive until it has emitted.
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
