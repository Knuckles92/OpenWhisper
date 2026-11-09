"""Layout 4, "Home and detail": the tab is a place with two pages, never both jobs at once.

Home is where a walkthrough starts and where the ones already made are kept:
Start with one line saying what it will do, then every walkthrough as a card
with its first screenshot. Opening a card (or finishing a walkthrough) turns
the page to Detail: the storyboard alone, under a slim Start row and a link
back. The choices live in one Options popover, so neither page ever has to
fold settings away to make room.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QImageReader, QPainter, QPainterPath, QTextLayout, QTextOption
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.walkthrough import library
from services.walkthrough.storage import captures_root
from ui_qt.utils.palette import token_color
from ui_qt.widgets import walkthrough_review
from ui_qt.widgets.past_walkthroughs import summarize
from ui_qt.widgets.walkthrough_layouts._kit import (
    LayoutTab,
    Popover,
    card,
    ghost,
    label,
    link,
    plain,
    restyle,
    rule,
    step_badge,
)
from ui_qt.widgets.walkthrough_review import _day, _time

#: The most cards Home builds; the library lists every walkthrough.
MAX_CARDS = 9
#: Cards per row under the newest one.
COLUMNS = 3
#: Screenshots are decoded no wider than this (pixels), enough for the newest card at 2x.
COVER_DECODE_WIDTH = 760
#: The newest walkthrough's screenshot, beside its words.
FEATURED_COVER = QSize(288, 180)
#: The Start column: wide enough for Finish beside Cancel, and both shortcuts under it.
START_WIDTH = 320


def _first_shot(folder: Path) -> Path | None:
    try:
        document = library.load_document(folder)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return next((section.shot for section in document.sections if section.shot), None)


def _said(folder: Path, limit: int = 240) -> str:
    """How the walkthrough begins, as spoken, for the newest card."""
    try:
        document = library.load_document(folder)
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    words = " ".join(" ".join(s.text.split()) for s in document.sections if s.text.strip())
    return words if len(words) <= limit else words[:limit].rsplit(" ", 1)[0] + "…"


def _decode(path: Path | None) -> QImage | None:
    """The screenshot, scaled down while it is read."""
    if path is None:
        return None
    reader = QImageReader(str(path))
    size = reader.size()
    if not size.isValid():
        return None
    if size.width() > COVER_DECODE_WIDTH:
        reader.setScaledSize(size.scaled(COVER_DECODE_WIDTH, 100000,
                                         Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    return None if image.isNull() else image


class _Cover(QWidget):
    """A screenshot filling its box: cropped from the top, where a screen's heading is.

    ``fixed`` is a set size; without one it is as wide as its card and 16:10.
    ``corners`` are the radii (top-left, top-right, bottom-right, bottom-left)
    that follow the card's own rounded edge.
    """

    def __init__(self, image: QImage | None, *, fixed: QSize | None = None,
                 corners: tuple[float, float, float, float] = (11, 11, 0, 0)):
        super().__init__()
        self._image = image
        self._corners = corners
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        if fixed is not None:
            self.setFixedSize(fixed)
        else:
            policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            policy.setHeightForWidth(True)
            self.setSizePolicy(policy)

    def hasHeightForWidth(self) -> bool:
        return self.maximumHeight() != self.minimumHeight()

    def heightForWidth(self, width: int) -> int:
        return round(width * 10 / 16)

    def sizeHint(self) -> QSize:
        return QSize(256, 160)

    def minimumSizeHint(self) -> QSize:
        return QSize(120, 75)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        box = QRectF(self.rect())
        tl, tr, br, bl = self._corners
        clip = QPainterPath()
        clip.moveTo(box.left() + tl, box.top())
        clip.lineTo(box.right() - tr, box.top())
        clip.quadTo(box.right(), box.top(), box.right(), box.top() + tr)
        clip.lineTo(box.right(), box.bottom() - br)
        clip.quadTo(box.right(), box.bottom(), box.right() - br, box.bottom())
        clip.lineTo(box.left() + bl, box.bottom())
        clip.quadTo(box.left(), box.bottom(), box.left(), box.bottom() - bl)
        clip.lineTo(box.left(), box.top() + tl)
        clip.quadTo(box.left(), box.top(), box.left() + tl, box.top())
        painter.setClipPath(clip)
        if self._image is None:
            painter.fillRect(box, token_color("surface-sunken"))
            painter.setPen(token_color("text-muted"))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, "No screenshot")
            return
        # Cover the box: as wide as it, or as tall, whichever leaves nothing bare.
        scale = max(box.width() / self._image.width(), box.height() / self._image.height())
        wide, tall = box.width() / scale, box.height() / scale
        source = QRectF((self._image.width() - wide) / 2, 0, wide, tall)
        painter.drawImage(box, self._image, source)


class _Clamped(QLabel):
    """Wrapped text that stops after ``lines`` lines with an ellipsis.

    ``always`` keeps it that tall however short the text is, so the cards of
    a row line up; otherwise it is as tall as the lines it shows.
    """

    def __init__(self, text: str, name: str, lines: int = 2, *, always: bool = True):
        super().__init__(text)
        self.setObjectName(name)
        self._lines = lines
        self._always = always
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def _broken(self, width: int) -> list[tuple[int, int]]:
        """(start, length) of each line the text takes ``width`` wide, at most ``lines``."""
        layout = QTextLayout(self.text(), self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WordWrap)
        layout.setTextOption(option)
        layout.beginLayout()
        lines = []
        while len(lines) < self._lines:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(max(1, width))
            lines.append((line.textStart(), line.textLength()))
        layout.endLayout()
        return lines

    def _fit(self) -> None:
        self.ensurePolished()
        lines = self._lines if self._always else max(1, len(self._broken(self.width())))
        height = self.fontMetrics().lineSpacing() * lines
        if height != self.maximumHeight() or height != self.minimumHeight():
            self.setFixedHeight(height)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Its height follows the lines its width breaks the text into.
        self._fit()

    def sizeHint(self) -> QSize:
        self.ensurePolished()
        lines = self._lines if self._always else 1
        return QSize(160, self.fontMetrics().lineSpacing() * lines)

    def minimumSizeHint(self) -> QSize:
        return QSize(40, self.sizeHint().height())

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        metrics = self.fontMetrics()
        text = self.text()
        lines = self._broken(self.width())
        for index, (start, length) in enumerate(lines):
            shown = text[start:start + length]
            if index == len(lines) - 1 and start + length < len(text):
                # The last line that fits takes the rest of the text, cut short.
                shown = metrics.elidedText(text[start:], Qt.TextElideMode.ElideRight, self.width())
            painter.drawText(0, index * metrics.lineSpacing() + metrics.ascent(), shown)


def _quiet(text: str, name: str) -> QLabel:
    """A label on a card: clicks go through it to the card."""
    widget = label(text, name)
    widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    return widget


class WalkthroughShelfCard(QFrame):
    """One walkthrough on Home: click, Enter or Space opens it."""

    clicked = pyqtSignal(object)

    def __init__(self, folder: Path, summary, today, *, featured: bool = False):
        super().__init__()
        self.folder = folder
        self.setObjectName("glCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        when = summary.when
        stamp = f"{_day(when, today)} · {_time(when)}" if when else ""
        image = _decode(_first_shot(folder))
        if featured:
            self._featured(summary, stamp, image)
        else:
            self._regular(summary, stamp, image)
        self.setAccessibleName(f"{summary.title}. {stamp}, {summary.meta}")

    def _regular(self, summary, stamp: str, image) -> None:
        column = QVBoxLayout(self)
        # Inside the 1px border, so the screenshot meets the card's edge.
        column.setContentsMargins(1, 1, 1, 0)
        column.setSpacing(0)
        column.addWidget(_Cover(image))
        words = QVBoxLayout()
        words.setContentsMargins(13, 10, 13, 12)
        words.setSpacing(4)
        words.addWidget(_Clamped(summary.title, "glTitle"))
        foot = QHBoxLayout()
        foot.setSpacing(6)
        foot.addWidget(_quiet(stamp, "glMeta"))
        foot.addStretch(1)
        if summary.pill:
            foot.addWidget(self._pill(summary))
        words.addLayout(foot)
        words.addWidget(_quiet(summary.meta, "glMeta"))
        column.addLayout(words)

    def _featured(self, summary, stamp: str, image) -> None:
        row = QHBoxLayout(self)
        row.setContentsMargins(1, 1, 18, 1)
        row.setSpacing(18)
        row.addWidget(_Cover(image, fixed=FEATURED_COVER, corners=(11, 0, 0, 11)))
        words = QVBoxLayout()
        words.setContentsMargins(0, 14, 0, 14)
        words.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(_quiet("Latest", "glTag"))
        top.addWidget(_quiet(stamp, "glMeta"))
        top.addStretch(1)
        if summary.pill:
            top.addWidget(self._pill(summary))
        words.addLayout(top)
        words.addWidget(_Clamped(summary.title, "glTitleBig", always=False))
        said = _said(self.folder)
        if said and said != summary.title:
            words.addWidget(_Clamped(said, "glSaid", lines=3, always=False))
        words.addStretch(1)
        foot = QHBoxLayout()
        foot.addWidget(_quiet(summary.meta, "glMeta"))
        foot.addStretch(1)
        foot.addWidget(_quiet("Open  →", "glOpen"))
        words.addLayout(foot)
        row.addLayout(words, 1)

    @staticmethod
    def _pill(summary) -> QLabel:
        pill = _quiet(summary.pill, "glPill")
        if summary.pill_tone:
            pill.setProperty("tone", summary.pill_tone)
        return pill

    def mousePressEvent(self, event) -> None:
        # Taken here so the release comes back to the card.
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.clicked.emit(self.folder)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(self.folder)
            return
        super().keyPressEvent(event)


class GalleryTab(LayoutTab):
    WIDTH = 900
    WIDTH_WITH_STORYBOARD = 900
    #: The walkthroughs changed (or were never read): Home reads them again when it shows.
    _stale = True

    STYLE = """
        QFrame#glCard {
            background-color: @surface;
            border: 1px solid @border;
            border-radius: 12px;
        }
        QFrame#glCard:hover {
            background-color: @surface-hover;
            border-color: @border-hover;
        }
        QFrame#glCard:focus {
            border-color: @accent;
        }
        QLabel#glTitle {
            color: @text;
            font-size: 13px;
            font-weight: 600;
            background: transparent;
        }
        QLabel#glTitleBig {
            color: @text-heading;
            font-size: 17px;
            font-weight: 700;
            background: transparent;
        }
        QLabel#glSaid {
            color: @text-secondary;
            font-size: 13px;
            background: transparent;
        }
        QLabel#glMeta {
            color: @text-secondary;
            font-size: 12px;
            background: transparent;
        }
        QLabel#glOpen {
            color: @accent;
            font-size: 12px;
            font-weight: 600;
            background: transparent;
        }
        QLabel#glPill {
            color: @accent-soft;
            background-color: rgba(@accent-rgb, 0.14);
            border-radius: 8px;
            padding: 1px 8px;
            font-size: 11px;
            font-weight: 600;
        }
        QLabel#glPill[tone="warning"] {
            color: @warning-text;
            background-color: rgba(@warning-rgb, 0.16);
        }
        QLabel#glTag {
            color: @on-accent;
            background-color: @accent;
            border-radius: 8px;
            padding: 1px 8px;
            font-size: 11px;
            font-weight: 700;
        }
        QLabel#glCount {
            color: @text-secondary;
            background-color: rgba(@overlay-rgb, 0.07);
            border-radius: 7px;
            padding: 1px 8px;
            font-size: 12px;
            font-weight: 600;
        }
        QLabel#glRecipe {
            color: @text;
            font-size: 14px;
            font-weight: 600;
            background: transparent;
        }
        QLabel#glRecipe[live="true"] {
            color: @warning-text;
        }
        QLabel#wlOverline[live="true"] {
            color: @warning-text;
        }
    """

    def build(self, page: QVBoxLayout) -> None:
        page.addWidget(self._start_card())
        self.options = self._options_popover()

        # ---- Home: the walkthroughs already made ----
        self.home = plain()
        home = QVBoxLayout(self.home)
        home.setContentsMargins(0, 4, 0, 0)
        home.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(label("Your walkthroughs", "wlHeading"))
        self.count_label = label("", "glCount")
        head.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        self.more_label = label("", "wlHint")
        head.addWidget(self.more_label, 0, Qt.AlignmentFlag.AlignBottom)
        home.addLayout(head)
        self.shelf = plain()
        self.grid = QGridLayout(self.shelf)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(14)
        for column in range(COLUMNS):
            self.grid.setColumnStretch(column, 1)
        home.addWidget(self.shelf)
        self.empty = self._empty_state()
        home.addWidget(self.empty)
        page.addWidget(self.home)

        # ---- Detail: one walkthrough, and the way back ----
        self.back_row = plain()
        back = QHBoxLayout(self.back_row)
        back.setContentsMargins(2, 0, 2, 0)
        self.back_link = link("←  All walkthroughs", self.close_walkthrough,
                              "Back to every walkthrough you've made")
        back.addWidget(self.back_link)
        back.addStretch(1)
        page.addWidget(self.back_row)
        page.addWidget(self.review, 1)
        page.addStretch()

        self.cards: list[WalkthroughShelfCard] = []
        self._shown: tuple = ()
        self.walkthroughs_changed.connect(self._mark_stale)

    def _start_card(self) -> QFrame:
        hero = card()
        self.hero_row = QHBoxLayout(hero)
        self.hero_row.setSpacing(20)

        start = plain()
        start.setFixedWidth(START_WIDTH)
        column = QVBoxLayout(start)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addWidget(self.walkthrough_button, 3)
        buttons.addWidget(self.cancel_button, 2)
        column.addLayout(buttons)
        # Home only: on Detail the row stays one line tall.
        self.key_row = plain()
        keys = QHBoxLayout(self.key_row)
        keys.setContentsMargins(0, 0, 0, 0)
        keys.setSpacing(6)
        keys.addStretch(1)
        keys.addWidget(self.walkthrough_badge)
        keys.addWidget(label("Start", "wlHint"))
        keys.addWidget(self.start_key)
        keys.addSpacing(8)
        keys.addWidget(label("Screenshot", "wlHint"))
        keys.addWidget(self.snap_key)
        keys.addStretch(1)
        column.addWidget(self.key_row)
        self.hero_row.addWidget(start, 0, Qt.AlignmentFlag.AlignVCenter)

        self.hero_rule = rule(vertical=True)
        self.hero_row.addWidget(self.hero_rule)

        words = QVBoxLayout()
        words.setSpacing(4)
        words.addStretch(1)
        self.next_overline = label("NEXT WALKTHROUGH", "wlOverline")
        words.addWidget(self.next_overline)
        self.recipe_label = label("", "glRecipe", wrap=True)
        words.addWidget(self.recipe_label)
        self.recipe_note = label("", "wlHint", wrap=True)
        words.addWidget(self.recipe_note)
        words.addStretch(1)
        self.hero_row.addLayout(words, 1)

        self.options_button = ghost("Options", self._open_options)
        self.options_button.setToolTip("Change how it's captured, written up and copied")
        self.hero_row.addWidget(self.options_button, 0, Qt.AlignmentFlag.AlignVCenter)
        return hero

    def _options_popover(self) -> Popover:
        """Every choice for the next walkthrough, in the order it happens."""
        popover = Popover(self, width=600)
        body = popover.body
        body.setSpacing(9)
        body.addWidget(label("Options for the next walkthrough", "wlTitle2"))

        body.addWidget(label("CAPTURE", "wlOverline"))
        capture = QHBoxLayout()
        capture.setSpacing(16)
        capture.addWidget(self.auto_check)
        capture.addWidget(self.capture_delay)
        capture.addStretch(1)
        body.addLayout(capture)
        body.addWidget(self.minimize_check, 0, Qt.AlignmentFlag.AlignLeft)
        body.addWidget(rule())

        body.addWidget(label("COPY WHEN IT FINISHES", "wlOverline"))
        body.addWidget(self.copy_bar)
        body.addWidget(rule())

        head = QHBoxLayout()
        head.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label("AI WRITE-UP", "wlOverline"))
        titles.addWidget(self.write_up_text)
        head.addLayout(titles, 1)
        head.addWidget(self.write_up_switch, 0, Qt.AlignmentFlag.AlignVCenter)
        body.addLayout(head)
        agents = QHBoxLayout()
        agents.setSpacing(10)
        for choice in self.agent_choices.values():
            agents.addWidget(choice)
        body.addLayout(agents)
        # The whole popover has to fit a small window: the prompt scrolls sooner here.
        self.prompt_edit.setMaximumHeight(104)
        body.addWidget(self.prompt_area)

        foot = QHBoxLayout()
        foot.addWidget(self.settings_link)
        foot.addStretch(1)
        foot.addWidget(ghost("Done", popover.hide))
        body.addLayout(foot)
        # Settings opens its own window: the popover would sit on top of it.
        self.settings_link.clicked.connect(popover.hide)
        return popover

    def _empty_state(self) -> QFrame:
        empty = card("sunken")
        column = QVBoxLayout(empty)
        column.setContentsMargins(22, 20, 22, 22)
        column.setSpacing(14)
        column.addWidget(label("Nothing here yet", "wlTitle2"))
        column.addWidget(label(
            "A walkthrough is you talking through what's on screen while you take "
            "screenshots. Each one you make is kept here.", "wlHint", wrap=True))
        row = QHBoxLayout()
        row.setSpacing(18)
        for number, (title, text) in enumerate((
            ("Talk it through", "Press Start and explain what's on screen."),
            ("Snap as you go", "Each screenshot lands beside what you were saying."),
            ("Paste the result", "One document, already on your clipboard."),
        ), 1):
            cell = QHBoxLayout()
            cell.setSpacing(10)
            cell.addWidget(step_badge(number, quiet=True), 0, Qt.AlignmentFlag.AlignTop)
            words = QVBoxLayout()
            words.setSpacing(1)
            words.addWidget(label(title, "wlTitle2"))
            words.addWidget(label(text, "wlHint", wrap=True))
            cell.addLayout(words, 1)
            row.addLayout(cell, 1)
        column.addLayout(row)
        return empty

    # ---- Home's cards ----

    def _mark_stale(self) -> None:
        self._stale = True
        self._refresh()

    def _show(self, folder, *, ask: bool) -> bool:
        # A walkthrough that opened or closed may be new, renamed or gone.
        self._stale = True
        return super()._show(folder, ask=ask)

    def _reload(self) -> None:
        """Read the capture folders again, rebuilding the cards only if they changed."""
        self._stale = False
        folders = library.entries(captures_root())
        today = walkthrough_review.date.today()
        ratio = self.devicePixelRatioF()
        summaries = [(folder, summarize(folder, today, ratio)) for folder in folders[:MAX_CARDS]]
        shown = tuple((folder.name, summary.title, summary.meta, summary.pill)
                      for folder, summary in summaries) + (len(folders), today)
        if shown == self._shown:
            return
        self._shown = shown
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.cards = []
        for index, (folder, summary) in enumerate(summaries):
            tile = WalkthroughShelfCard(folder, summary, today, featured=index == 0)
            tile.clicked.connect(lambda chosen: self.show_walkthrough(chosen, ask=True))
            if index == 0:
                # The newest gets a row to itself: its screenshot beside how it begins.
                self.grid.addWidget(tile, 0, 0, 1, COLUMNS)
            else:
                self.grid.addWidget(tile, 1 + (index - 1) // COLUMNS, (index - 1) % COLUMNS)
            self.cards.append(tile)
        self.count_label.setText(str(len(folders)))
        self.count_label.setVisible(bool(folders))
        hidden = len(folders) - len(summaries)
        self.more_label.setText(f"Showing the newest {len(summaries)} of {len(folders)}"
                                if hidden else "")
        self.shelf.setVisible(bool(folders))
        self.empty.setVisible(not folders)

    # ---- state ----

    def _open_options(self) -> None:
        self.options.open_under(self.options_button, align_right=True)

    def refresh(self) -> None:
        detail = self.has_walkthrough
        live = self.walkthrough_active
        self.home.setVisible(not detail)
        self.back_row.setVisible(detail)
        # Detail keeps Start in reach on one slim row; Home gives it room.
        self.key_row.setVisible(not detail)
        self.next_overline.setVisible(not detail)
        margin = (16, 10, 16, 10) if detail else (20, 18, 20, 18)
        self.hero_row.setContentsMargins(*margin)

        if live:
            key = self.start_key.text()
            self.next_overline.setText("RECORDING")
            self.recipe_label.setText("Talk it through and take screenshots as you go.")
            self.recipe_note.setText(
                f"Finish here or with {key}." if key and key != "Not set" else "Finish here.")
            self.recipe_note.show()
        else:
            parts = self.recipe()
            self.next_overline.setText("NEXT WALKTHROUGH")
            # How it is made on one line, what comes of it on the next.
            self.recipe_label.setText("  →  ".join(parts[:2]))
            # Short enough for one line, so Detail's row stays slim.
            self.recipe_note.setText(
                f"{parts[2]} · this window goes to the tray" if self.minimize_on
                else f"{parts[2]} when it finishes")
            self.recipe_note.show()
        for widget in (self.next_overline, self.recipe_label):
            if widget.property("live") != live:
                widget.setProperty("live", live)
                restyle(widget)
        self.options_button.setEnabled(not live and not self.is_recording)
        self.back_link.setEnabled(not self.review._busy)

        if not detail and self._stale:
            self._reload()
