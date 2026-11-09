"""Layout 5, "Studio": the controls down the left, the walkthrough on the right, always.

Two panes that never fold or swap. The left column is one tidy form: Start,
the shortcuts, then what happens while recording, who writes it up, and what
is copied. The right side is a stage with one slot: the storyboard, a calm
"Recording" card while one is being made, or what to do and the last few
walkthroughs when none is open. Changing a setting never moves the stage, and
opening a walkthrough never moves the settings.
"""
from __future__ import annotations

from PyQt6.QtCore import QPoint, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.walkthrough import library
from services.walkthrough.storage import captures_root
from ui_qt.utils.palette import token_color
from ui_qt.widgets import past_walkthroughs, walkthrough_review
from ui_qt.widgets.eliding_label import ElidingLabel
from ui_qt.widgets.walkthrough_layouts import _kit
from ui_qt.widgets.walkthrough_layouts._kit import (
    LayoutTab,
    Popover,
    bound_switch,
    card,
    label,
    link,
    plain,
    rule,
    step_badge,
)

#: The control column, and the fields down its right edge.
COLUMN_WIDTH = 320
FIELD_WIDTH = 164
ROW_HEIGHT = 36
#: Under Start: two shortcut rows, or the Cancel button.
KEYS_SLOT_HEIGHT = 68
#: The stage asks for this whatever it shows, so the window never resizes between states.
STAGE_SIZE = QSize(640, 640)
STAGE_MIN_HEIGHT = 560
#: How many past walkthroughs the empty stage offers.
RECENT_SHOWN = 3
THUMB_SIZE = QSize(76, 48)


class _Field(QPushButton):
    """A drop-down value at a fixed width: the text elides, the chevron sits at the right.

    Drawn to match the capture delay's own chip, which shares the column.
    """

    def __init__(self, menu):
        super().__init__()
        self.setObjectName("studioField")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(FIELD_WIDTH, 34)
        self._menu = menu
        self._value = ""
        self.clicked.connect(self._open)

    def set_value(self, text: str) -> None:
        self._value = text
        # 12 padding each side, and room for the chevron.
        self.setText(self.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideRight, FIELD_WIDTH - 24 - 18))
        self.setToolTip(text if self.text() != text else "")
        self.setAccessibleName(text)

    def _open(self) -> None:
        menu = self._menu()
        menu.setMinimumWidth(max(menu.sizeHint().width(), self.width()))
        menu.exec(self.mapToGlobal(QPoint(0, self.height() + 4)))

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(1.0 if self.isEnabled() else 0.4)
        painter.setPen(QPen(token_color("text-secondary"), 1.4, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        x, y = self.width() - 17, self.height() / 2
        painter.drawLine(int(x - 3), int(y - 2), int(x), int(y + 1))
        painter.drawLine(int(x), int(y + 1), int(x + 3), int(y - 2))


class _Stage(QWidget):
    """The right side's one slot: whichever pane shows fills it, and its size is its own.

    It has no layout on purpose. A layout would pass each pane's size hint
    (and height-for-width) up to the window, which would then resize as the
    stage changed from the empty state to a recording to a storyboard.
    """

    def __init__(self):
        super().__init__()
        self.setObjectName("wlPlain")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(STAGE_MIN_HEIGHT)
        self._panes: list[QWidget] = []

    def add(self, pane: QWidget) -> QWidget:
        pane.setParent(self)
        pane.hide()
        self._panes.append(pane)
        return pane

    def show_only(self, pane: QWidget) -> None:
        pane.setGeometry(self.rect())
        for each in self._panes:
            each.setVisible(each is pane)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for pane in self._panes:
            pane.setGeometry(self.rect())

    def sizeHint(self) -> QSize:
        return STAGE_SIZE

    def minimumSizeHint(self) -> QSize:
        return QSize(420, STAGE_MIN_HEIGHT)


class _Thumb(QLabel):
    """A walkthrough's first screenshot, small, with rounded corners."""

    def __init__(self, image: QImage | None):
        super().__init__()
        self.setObjectName("studioThumb")
        self.setFixedSize(THUMB_SIZE)
        if image is None:
            return
        ratio = self.devicePixelRatioF()
        target = THUMB_SIZE * ratio
        # The card's strip is wide and short; keep its middle at this shape.
        cover = image.scaledToHeight(target.height(), Qt.TransformationMode.SmoothTransformation)
        x = max(0, (cover.width() - target.width()) // 2)
        cover = cover.copy(x, 0, target.width(), target.height())
        rounded = QPixmap(cover.size())
        rounded.fill(Qt.GlobalColor.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(rounded.rect()), 6 * ratio, 6 * ratio)
        painter.setClipPath(clip)
        # Into the whole pixmap: the strip carries a pixel ratio of its own.
        painter.drawImage(QRectF(rounded.rect()), cover)
        painter.end()
        rounded.setDevicePixelRatio(ratio)
        self.setPixmap(rounded)


class _RecentRow(QFrame):
    """One past walkthrough on the empty stage: click, Enter or Space opens it."""

    clicked = pyqtSignal(object)

    def __init__(self, summary, today):
        super().__init__()
        self.folder = summary.folder
        self.setObjectName("studioRecent")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 8, 12, 8)
        row.setSpacing(12)
        row.addWidget(_Thumb(summary.thumb))
        words = QVBoxLayout()
        words.setSpacing(2)
        self.title_label = ElidingLabel(summary.title)
        self.title_label.setObjectName("wlTitle2")
        words.addWidget(self.title_label)
        when = summary.when
        meta = summary.meta if when is None else (
            f"{walkthrough_review._day(when, today)} · {walkthrough_review._time(when)}"
            f" · {summary.meta}")
        words.addWidget(label(meta, "wlHint"))
        row.addLayout(words, 1)
        if summary.pill:
            pill = QLabel(summary.pill)
            pill.setObjectName("studioPill")
            row.addWidget(pill, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setAccessibleName(f"Open {summary.title}. {meta}")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.clicked.emit(self.folder)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(self.folder)
            return
        super().keyPressEvent(event)


class StudioTab(LayoutTab):
    WIDTH = 1040
    WIDTH_WITH_STORYBOARD = 1040
    STYLE = """
        QLabel#studioRowName {
            color: @text;
            font-size: 13px;
            background: transparent;
        }
        QLabel#studioRowName:disabled {
            color: @text-muted;
        }
        QPushButton#studioField {
            color: @text;
            background-color: @surface;
            border: 1px solid @border;
            border-radius: 10px;
            padding: 0 30px 0 12px;
            font-size: 12px;
            font-weight: 500;
            min-height: 0;
            text-align: left;
        }
        QPushButton#studioField:hover {
            background-color: @surface-hover;
        }
        QPushButton#studioField:focus {
            border-color: @accent-tint-border;
        }
        QPushButton#studioField:disabled {
            color: @text-muted;
            border-color: @border-subtle;
        }
        QPushButton#wlLink:disabled {
            color: @text-muted;
        }
        QFrame#studioRecent {
            background-color: transparent;
            border: 1px solid transparent;
            border-radius: 10px;
        }
        QFrame#studioRecent:hover, QFrame#studioRecent:focus {
            background-color: rgba(@overlay-rgb, 0.06);
            border-color: @border;
        }
        QLabel#studioThumb {
            background-color: @slate-field;
            border-radius: 6px;
        }
        QLabel#studioPill {
            color: @accent-soft;
            background-color: rgba(@accent-rgb, 0.12);
            border-radius: 8px;
            padding: 1px 8px;
            font-size: 11px;
            font-weight: 600;
        }
        QLabel#studioDot {
            background-color: @danger;
            border-radius: 6px;
            min-width: 12px; max-width: 12px;
            min-height: 12px; max-height: 12px;
        }
        QLabel#studioKeys {
            color: @text;
            font-size: 14px;
            background: transparent;
        }
    """

    def build(self, page: QVBoxLayout) -> None:
        self._stage_state = None
        panes = QHBoxLayout()
        panes.setContentsMargins(0, 0, 0, 0)
        panes.setSpacing(16)
        panes.addWidget(self._controls())
        panes.addWidget(self._build_stage(), 1)
        page.addLayout(panes, 1)

        # The prompt, its styles and the save/delete links open from the column.
        self.prompt_popover = Popover(self, width=520)
        self.prompt_popover.body.addWidget(label("What the agent is asked for", "wlTitle2"))
        self.prompt_popover.body.addWidget(self.prompt_area)

    # ---- the control column ----

    def _controls(self) -> QWidget:
        column = card()
        column.setFixedWidth(COLUMN_WIDTH)
        body = QVBoxLayout(column)
        body.setContentsMargins(16, 16, 16, 12)
        body.setSpacing(2)

        body.addWidget(self.walkthrough_button)
        # One slot under Start holds the shortcuts, or Cancel while a
        # walkthrough records (the stage shows the shortcuts then), so the
        # rows below never shift. Cancel goes under Finish, not beside it:
        # "Finish Walkthrough" needs the column's width.
        slot = plain()
        slot.setFixedHeight(KEYS_SLOT_HEIGHT)
        under = QVBoxLayout(slot)
        under.setContentsMargins(0, 8, 0, 0)
        under.setSpacing(0)
        under.addWidget(self.cancel_button)
        self.keys_box = plain()
        keys = QVBoxLayout(self.keys_box)
        keys.setContentsMargins(0, 0, 0, 0)
        keys.setSpacing(4)
        keys.addWidget(self._key_row("Start / finish", self.start_key, self.walkthrough_badge))
        keys.addWidget(self._key_row("Screenshot", self.snap_key))
        under.addWidget(self.keys_box)
        under.addStretch(1)
        body.addWidget(slot)

        self._section(body, "CAPTURE")
        if _kit.AUTO_MODE_HERE:
            self.auto_switch = bound_switch(self.auto_check, "Auto Mode")
            body.addWidget(self._row("Auto Mode", self.auto_switch, self.auto_check.toolTip()))
            self.capture_delay.setFixedWidth(FIELD_WIDTH)
            body.addWidget(self._row("After each click", self.capture_delay,
                                     self.capture_delay.toolTip()))
        self.minimize_switch = bound_switch(self.minimize_check, "Minimize to tray when it starts")
        body.addWidget(self._row("Minimize to tray", self.minimize_switch,
                                 self.minimize_check.toolTip()))

        self._section(body, "WRITE-UP")
        body.addWidget(self._row("AI write-up", self.write_up_switch,
                                 "An agent lays out a clean document from it. "
                                 "The as-spoken PDF is kept too."))
        self.agent_field = _Field(lambda: self.agent_menu(with_off=False))
        self.style_field = _Field(lambda: self.style_menu(edit=self.edit_prompt))
        self.prompt_link = link("Edit the prompt…", self.edit_prompt,
                                "See and change exactly what the agent is asked for")
        link_row = plain()
        links = QHBoxLayout(link_row)
        links.setContentsMargins(0, 2, 0, 2)
        links.addStretch(1)
        links.addWidget(self.prompt_link)
        # Without a write-up these have nobody to act on; dimmed, not hidden,
        # so the column keeps its shape.
        self._write_up_rows = [
            self._row("Written by", self.agent_field),
            self._row("Style", self.style_field),
            self._row("Follow my notes", self.notes_switch, self.notes_label.toolTip()),
            link_row,
        ]
        for row in self._write_up_rows:
            body.addWidget(row)

        self._section(body, "COPY")
        self.copy_field = _Field(self.copy_menu)
        body.addWidget(self._row("On finish, copy", self.copy_field))

        body.addStretch(1)
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 8, 0, 0)
        foot.addWidget(self.settings_link)
        foot.addStretch(1)
        body.addLayout(foot)
        return column

    @staticmethod
    def _section(body: QVBoxLayout, title: str) -> None:
        body.addSpacing(10)
        body.addWidget(rule())
        body.addSpacing(10)
        body.addWidget(label(title, "wlOverline"))
        body.addSpacing(2)

    @staticmethod
    def _row(text: str, control: QWidget, tip: str = "") -> QWidget:
        """A form row: what it is on the left, its control at the right edge."""
        row = plain()
        row.setFixedHeight(ROW_HEIGHT)
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(8)
        name = label(text, "studioRowName")
        if tip:
            name.setToolTip(tip)
        line.addWidget(name)
        line.addStretch(1)
        line.addWidget(control)
        return row

    @staticmethod
    def _key_row(text: str, chip: QWidget, badge: QWidget | None = None) -> QWidget:
        row = plain()
        row.setFixedHeight(28)
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(6)
        line.addWidget(label(text, "wlHint"))
        if badge is not None:
            line.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        line.addStretch(1)
        line.addWidget(chip)
        return row

    # ---- the stage ----

    def _build_stage(self) -> QWidget:
        self.stage = _Stage()
        self.stage.add(self.review)
        self.recording_pane = self.stage.add(self._recording_card())
        self.empty_pane = self.stage.add(self._empty_card())
        return self.stage

    def _recording_card(self) -> QWidget:
        pane = card("sunken")
        outer = QVBoxLayout(pane)
        outer.setContentsMargins(32, 32, 32, 32)
        outer.addStretch(2)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addStretch(1)
        dot = QLabel()
        dot.setObjectName("studioDot")
        head.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(label("Recording your walkthrough", "wlHeading"))
        head.addStretch(1)
        outer.addLayout(head)
        outer.addSpacing(10)
        self.recording_keys = label("", "studioKeys")
        self.recording_keys.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.recording_keys)
        outer.addSpacing(4)
        after = label("It opens here as a storyboard when you finish.", "wlHint")
        after.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(after)
        outer.addStretch(3)
        return pane

    def _empty_card(self) -> QWidget:
        pane = card("sunken")
        outer = QHBoxLayout(pane)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.addStretch(1)
        column = plain()
        column.setFixedWidth(460)
        body = QVBoxLayout(column)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addStretch(2)
        body.addWidget(label("No walkthrough open", "wlHeading"))
        body.addSpacing(4)
        body.addWidget(label("Start one on the left, or open a recent one below.", "wlHint"))
        body.addSpacing(22)
        for number, (title, text) in enumerate((
            ("Talk it through", "Press Start and explain what's on screen."),
            ("Snap as you go", "Each screenshot lands beside what you were saying."),
            ("Paste the result", "One document, already on your clipboard."),
        ), 1):
            step = QHBoxLayout()
            step.setSpacing(12)
            step.addWidget(step_badge(number, quiet=True), 0, Qt.AlignmentFlag.AlignTop)
            words = QVBoxLayout()
            words.setSpacing(1)
            words.addWidget(label(title, "wlTitle2"))
            words.addWidget(label(text, "wlHint"))
            step.addLayout(words, 1)
            body.addLayout(step)
            body.addSpacing(14)

        self.recent_box = plain()
        recent = QVBoxLayout(self.recent_box)
        recent.setContentsMargins(0, 10, 0, 0)
        recent.setSpacing(4)
        recent.addWidget(rule())
        recent.addSpacing(12)
        recent.addWidget(label("RECENT", "wlOverline"))
        recent.addSpacing(2)
        self.recent_rows = QVBoxLayout()
        self.recent_rows.setSpacing(2)
        recent.addLayout(self.recent_rows)
        body.addWidget(self.recent_box)
        body.addStretch(3)
        outer.addWidget(column)
        outer.addStretch(1)
        return pane

    def _load_recent(self) -> None:
        """Read the newest few walkthroughs for the empty stage (a handful of small files)."""
        while self.recent_rows.count():
            item = self.recent_rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        # Through the module: the name is replaced when the date is pinned.
        today = walkthrough_review.date.today()
        folders = library.entries(captures_root())[:RECENT_SHOWN]
        for folder in folders:
            row = _RecentRow(
                past_walkthroughs.summarize(folder, today, self.devicePixelRatioF()), today)
            row.clicked.connect(lambda chosen: self.show_walkthrough(chosen, ask=True))
            self.recent_rows.addWidget(row)
        self.recent_box.setVisible(bool(folders))

    # ---- behaviour ----

    def fills_height(self) -> bool:
        """The stage takes whatever height the window has, in every state."""
        return True

    def edit_prompt(self) -> None:
        if not self.write_up_on:
            self.set_write_up(True)
        self.prompt_popover.open_under(self.prompt_link)

    def refresh(self) -> None:
        on = self.write_up_on
        self.keys_box.setVisible(not self.walkthrough_active)
        # Both shortcuts at one width, so the pair reads as a column.
        self.start_key.setMinimumWidth(0)
        self.snap_key.setMinimumWidth(0)
        widest = max(self.start_key.sizeHint().width(), self.snap_key.sizeHint().width())
        for chip in (self.start_key, self.snap_key):
            chip.setMinimumWidth(widest)
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.agent_field.set_value(self.agent_name)
        style = self.style_name
        self.style_field.set_value(style[:1].upper() + style[1:])
        self.copy_field.set_value(self.copy_label)
        for row in self._write_up_rows:
            row.setEnabled(on)
        self.recording_keys.setText(
            f"Finish with <b>{self.start_key.text()}</b>  ·  "
            f"Screenshot with <b>{self.snap_key.text()}</b>")

        # A recording takes the stage even over an open storyboard, which
        # comes back if it is cancelled.
        state = ("recording" if self.walkthrough_active
                 else "review" if self.review.folder is not None else "empty")
        if state != self._stage_state:
            self._stage_state = state
            if state == "empty":
                self._load_recent()
        # Every time: the base class shows the storyboard itself when one opens.
        self.stage.show_only({"recording": self.recording_pane, "review": self.review,
                              "empty": self.empty_pane}[state])
