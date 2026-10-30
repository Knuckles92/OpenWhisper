"""The side panel's Walkthroughs page, shown while the Walkthrough tab is open.

Each card is one capture folder: when it was made, its title (the write-up's
heading, else its first words), its first screenshot, and what it holds.
Clicking one opens it in Saved Walkthroughs. Folders are read and the
screenshots decoded on a worker thread, which hands its results back
through this module's relay.
"""
from __future__ import annotations

import itertools
import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from PyQt6.QtCore import QObject, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QImage, QImageReader, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.walkthrough import library
from services.walkthrough.document import clock
from services.walkthrough.storage import captures_root
from services.walkthrough.write_up import MARKDOWN_NAME, PDF_NAME
from ui_qt.widgets.walkthrough_review import _day, _shots, _started, _time

logger = logging.getLogger(__name__)

#: The most cards the page builds; Saved Walkthroughs lists every one.
MAX_SHOWN = 60
#: The screenshot strip on each card, in logical pixels.
THUMB_SIZE = QSize(296, 100)
#: Titles made from spoken words stop around here, at a word break.
TITLE_CHARS = 90

_tokens = itertools.count(1)
_HEADING = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Summary:
    """What a card shows about one walkthrough folder."""

    folder: Path
    when: datetime | None
    title: str
    meta: str
    pill: str
    pill_tone: str
    #: Everything a search matches against, lowercased.
    words: str
    thumb: QImage | None


class _Relay(QObject):
    """Carries a worker's summaries back to the UI thread: (token, summaries)."""

    loaded = pyqtSignal(int, object)


_relay_instance: _Relay | None = None


def _relay() -> _Relay:
    # Made on first use, on the UI thread: a module-level QObject would live
    # on whichever thread imported this module.
    global _relay_instance
    if _relay_instance is None:
        _relay_instance = _Relay()
    return _relay_instance


def _title(folder: Path, document) -> str:
    try:
        heading = _HEADING.search((folder / MARKDOWN_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        heading = None
    if heading:
        return heading.group(1)
    said = " ".join(" ".join(s.text.split()) for s in document.sections if s.text.strip())
    if len(said) <= TITLE_CHARS:
        return said or "No words"
    return said[:TITLE_CHARS].rsplit(" ", 1)[0] + "…"


def _thumb(path: Path, ratio: float) -> QImage | None:
    """The screenshot filling the strip, cropped from its top, corners rounded."""
    reader = QImageReader(str(path))
    size = reader.size()
    if not size.isValid():
        return None
    target = THUMB_SIZE * ratio
    cover = size.scaled(target, Qt.AspectRatioMode.KeepAspectRatioByExpanding)
    reader.setScaledSize(cover)
    image = reader.read()
    if image.isNull():
        return None
    # Centred across, from the top down: a screenshot's heading is up there.
    x = max(0, (image.width() - target.width()) // 2)
    image = image.copy(QRect(x, 0, target.width(), target.height()))
    rounded = QImage(image.size(), QImage.Format.Format_ARGB32_Premultiplied)
    rounded.fill(Qt.GlobalColor.transparent)
    painter = QPainter(rounded)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(rounded.rect()), 8 * ratio, 8 * ratio)
    painter.setClipPath(clip)
    painter.drawImage(0, 0, image)
    painter.end()
    rounded.setDevicePixelRatio(ratio)
    return rounded


def summarize(folder: Path, today: date, ratio: float = 1.0) -> Summary:
    """Read one capture folder for its card; any thread."""
    draft = (folder / library.DRAFT_NAME).exists()
    try:
        document = library.load_document(folder)
    except (OSError, ValueError, KeyError, TypeError):
        document = None
    when = _started(folder, document)
    if document is None:
        return Summary(
            folder, when,
            title="Not finished" if draft else "Can't be opened",
            meta=_day(when, today) if when else folder.name,
            pill="Interrupted" if draft else "", pill_tone="warning",
            words=folder.name.lower(), thumb=None,
        )
    shots = [section.shot for section in document.sections if section.shot]
    thumb = None
    if shots:
        try:
            thumb = _thumb(shots[0], ratio)
        except Exception:
            logger.debug("No thumbnail for %s", folder, exc_info=True)
    title = _title(folder, document)
    write_up = (folder / PDF_NAME).is_file()
    return Summary(
        folder, when, title,
        meta=f"{_shots(len(shots))} · {clock(document.duration_s)}",
        pill="AI write-up" if write_up else "", pill_tone="",
        words=" ".join([title, *(s.text for s in document.sections)]).lower(),
        thumb=thumb,
    )


class WalkthroughCard(QFrame):
    """One walkthrough: click, Enter or Space opens it."""

    clicked = pyqtSignal(object)

    def __init__(self, summary: Summary, today: date, parent=None):
        super().__init__(parent)
        self.summary = summary
        self.setObjectName("walkthroughCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        column = QVBoxLayout(self)
        column.setContentsMargins(14, 12, 14, 12)
        column.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(6)
        when = summary.when
        self.when_label = QLabel(f"{_day(when, today)} · {_time(when)}" if when else "")
        self.when_label.setObjectName("walkthroughCardWhen")
        top.addWidget(self.when_label)
        top.addStretch(1)
        if summary.pill:
            pill = QLabel(summary.pill)
            pill.setObjectName("walkthroughCardPill")
            if summary.pill_tone:
                pill.setProperty("tone", summary.pill_tone)
            top.addWidget(pill)
        column.addLayout(top)

        self.title_label = QLabel(summary.title)
        self.title_label.setObjectName("walkthroughCardTitle")
        self.title_label.setWordWrap(True)
        column.addWidget(self.title_label)

        if summary.thumb is not None:
            shot = QLabel()
            shot.setObjectName("walkthroughCardShot")
            shot.setPixmap(QPixmap.fromImage(summary.thumb))
            shot.setFixedHeight(THUMB_SIZE.height())
            shot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            shot.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            column.addWidget(shot)

        meta = QLabel(summary.meta)
        meta.setObjectName("walkthroughCardMeta")
        column.addWidget(meta)
        self.setAccessibleName(f"{summary.title}. {self.when_label.text()}, {summary.meta}")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.clicked.emit(self.summary.folder)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(self.summary.folder)
            return
        super().keyPressEvent(event)


class PastWalkthroughsPanel(QWidget):
    """Past walkthroughs, newest first, for the side panel."""

    #: Open Saved Walkthroughs: a capture folder to select, or None for the newest.
    walkthrough_selected = pyqtSignal(object)

    def __init__(self, parent=None, *, root: Path | None = None, today=date.today):
        super().__init__(parent)
        self.root = Path(root) if root is not None else captures_root()
        self._today = today
        self._token = 0
        #: How many folders the last read found; None until one has finished.
        self._total: int | None = None
        self.cards: list[WalkthroughCard] = []
        self.setObjectName("walkthroughsContent")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        _relay().loaded.connect(self._on_loaded)
        self._setup_ui()
        self._apply_style()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("Walkthroughs")
        title.setObjectName("sidebarHeader")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        head.addWidget(title)
        head.addStretch(1)
        self.library_button = QPushButton("Open library")
        self.library_button.setObjectName("walkthroughsLibraryLink")
        self.library_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.library_button.setToolTip("Review, fix, copy and delete walkthroughs")
        self.library_button.clicked.connect(lambda: self.walkthrough_selected.emit(None))
        head.addWidget(self.library_button)
        layout.addLayout(head)

        self.search_input = QLineEdit()
        self.search_input.setObjectName("historySearchInput")
        self.search_input.setPlaceholderText("Search walkthroughs...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setFixedHeight(32)
        self.search_input.textChanged.connect(self._filter)
        layout.addWidget(self.search_input)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("historyScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        column = QVBoxLayout(body)
        column.setContentsMargins(0, 0, 6, 0)
        column.setSpacing(12)
        column.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.count_header = QLabel("WALKTHROUGHS")
        self.count_header.setObjectName("sectionHeader")
        self.count_header.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        column.addWidget(self.count_header)
        self.card_layout = QVBoxLayout()
        self.card_layout.setSpacing(12)
        column.addLayout(self.card_layout)
        self.empty_label = QLabel("Loading walkthroughs…")
        self.empty_label.setObjectName("walkthroughsEmpty")
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.empty_label)
        self.scroll_area.setWidget(body)
        layout.addWidget(self.scroll_area, 1)

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QWidget#walkthroughsContent {
                background-color: @bg;
                border-left: 1px solid rgba(@overlay-rgb, 0.08);
            }
            QPushButton#walkthroughsLibraryLink {
                color: @accent;
                background: transparent;
                border: none;
                padding: 4px 2px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton#walkthroughsLibraryLink:hover,
            QPushButton#walkthroughsLibraryLink:focus {
                color: @accent-hover;
                text-decoration: underline;
            }
            QFrame#walkthroughCard {
                background-color: rgba(@surface-rgb, 0.5);
                border-radius: 12px;
                border: 1px solid rgba(@overlay-rgb, 0.05);
            }
            QFrame#walkthroughCard:hover, QFrame#walkthroughCard:focus {
                background-color: rgba(@surface-hover-rgb, 0.6);
                border: 1px solid rgba(@accent-rgb, 0.35);
            }
            QLabel#walkthroughCardWhen {
                color: @text-secondary-strong;
                background: transparent;
                font-size: 12px;
            }
            QLabel#walkthroughCardTitle {
                color: @text-heading;
                background: transparent;
                font-size: 14px;
                font-weight: 600;
            }
            QLabel#walkthroughCardShot {
                background: transparent;
            }
            QLabel#walkthroughCardMeta {
                color: @text-tertiary;
                background: transparent;
                font-size: 11px;
            }
            QLabel#walkthroughCardPill {
                background-color: rgba(@accent-rgb, 0.14);
                color: @accent-soft;
                border: 1px solid rgba(@accent-rgb, 0.25);
                border-radius: 6px;
                padding: 0px 8px;
                font-size: 10px;
                font-weight: 600;
            }
            QLabel#walkthroughCardPill[tone="warning"] {
                background-color: rgba(@warning-rgb, 0.12);
                color: @warning-text;
                border: 1px solid rgba(@warning-rgb, 0.28);
            }
            QLabel#walkthroughsEmpty {
                color: @text-muted;
                background: transparent;
                font-size: 12px;
                padding: 8px 0px;
            }
        """)

    # ---- loading ----

    def refresh(self) -> None:
        """Read the capture folders again, off the UI thread."""
        self._token = token = next(_tokens)
        root, today = self.root, self._today()
        ratio = self.devicePixelRatioF() or 1.0
        loaded = _relay().loaded

        def load() -> None:
            try:
                folders = library.entries(root)
            except OSError:
                folders = []
            summaries = []
            for folder in folders[:MAX_SHOWN]:
                try:
                    summaries.append(summarize(folder, today, ratio))
                except Exception:
                    logger.warning("Could not read walkthrough %s", folder, exc_info=True)
            loaded.emit(token, (summaries, len(folders)))

        threading.Thread(target=load, name="past-walkthroughs-load", daemon=True).start()

    def _on_loaded(self, token: int, result) -> None:
        if token != self._token:
            return
        summaries, total = result
        today = self._today()
        for card in self.cards:
            card.hide()
            card.setParent(None)
            card.deleteLater()
        self.cards = []
        for summary in summaries:
            card = WalkthroughCard(summary, today)
            card.clicked.connect(self.walkthrough_selected)
            self.card_layout.addWidget(card)
            self.cards.append(card)
        self._total = total
        self._filter()

    def _filter(self, *_args) -> None:
        """Show the cards whose title or words hold every word searched for."""
        terms = self.search_input.text().lower().split()
        shown = 0
        for card in self.cards:
            match = all(term in card.summary.words for term in terms)
            card.setVisible(match)
            shown += match
        total = self._total
        if total is None:
            return
        more = "+" if total > len(self.cards) else ""
        self.count_header.setText(
            f"WALKTHROUGHS ({shown}{more})" if shown else "WALKTHROUGHS")
        if not self.cards:
            message = ("Nothing yet. Your walkthroughs show up here, newest first, "
                       "with their first screenshot.")
        elif not shown:
            message = "No walkthrough says that"
        elif more and not terms:
            message = f"Showing the newest {len(self.cards)}. Open library for all {total}."
        else:
            message = ""
        self.empty_label.setText(message)
        self.empty_label.setVisible(bool(message))
