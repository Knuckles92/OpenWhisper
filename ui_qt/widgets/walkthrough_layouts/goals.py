"""Layout 3, "What are you making?": start from the outcome, not from settings.

The page leads with a grid of goals: one tile per write-up style, and one for
keeping it as spoken. Picking a tile is the whole setup for most walkthroughs;
who writes it, what's copied and how it's captured sit in one quiet strip
under the grid, and the prompt opens inside that strip. Start is last, with a
line saying what it will make. With a storyboard open, the tiles fold to a
row of pills so the walkthrough gets the room.
"""
from __future__ import annotations

from PyQt6.QtCore import QEvent, QSize, Qt
from PyQt6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui_qt.utils.icons import tabler_pixmap
from ui_qt.widgets.buttons import start_row
from ui_qt.widgets.walkthrough_layouts import _kit
from ui_qt.widgets.walkthrough_layouts._kit import (
    Chip,
    FlowLayout,
    LayoutTab,
    bound_switch,
    card,
    ghost,
    label,
    link,
    plain,
    rule,
)

#: Tile keys that are not a style id (a hex id or a slug).
SPOKEN = ":spoken"
OWN = ":own"
COLUMNS = 3
#: How much of a saved style's prompt its tile shows.
DESCRIPTION_CHARS = 60

#: What each built-in goal gives you, and its icon.
_BUILT_IN = {
    "walkthrough": ("notes-blue.svg", "A tidy document of what you showed and said."),
    "step-by-step": ("book-blue.svg", "Numbered steps someone else can follow."),
    "bug-report": ("info-warning.svg", "What broke and how to reproduce it."),
    "design-feedback": ("palette-purple.svg", "One section per change you'd make, and why."),
    "agent-task": ("terminal-green.svg", "A brief a coding agent can act on."),
    SPOKEN: ("microphone-blue.svg", "No AI. Your words and screenshots, untouched."),
    OWN: ("wand-purple.svg", "The prompt you wrote yourself."),
}
_SAVED_ICON = "stack-purple.svg"


def _shortened(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= DESCRIPTION_CHARS:
        return text
    return text[:DESCRIPTION_CHARS].rsplit(" ", 1)[0] + "…"


class GoalTile(QPushButton):
    """One thing a walkthrough can become: its icon, name and a line about it."""

    def __init__(self, key: str, name: str, icon: str, text: str):
        super().__init__()
        self.key = key
        self.setObjectName("wlGoal")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(name)
        self.setAccessibleDescription(text)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(12)
        mark = QLabel()
        mark.setObjectName("wlGoalIcon")
        mark.setPixmap(tabler_pixmap(icon, 22))
        mark.setFixedSize(36, 36)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
        words = QVBoxLayout()
        words.setSpacing(2)
        title = QLabel(name)
        title.setObjectName("wlGoalName")
        words.addWidget(title)
        about = QLabel(text)
        about.setObjectName("wlGoalText")
        about.setWordWrap(True)
        about.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        words.addWidget(about, 1)
        row.addLayout(words, 1)
        for child in (mark, title, about):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def hasHeightForWidth(self) -> bool:
        # The grid gives every tile one height (GoalsTab._even_tiles), not each its own.
        return False

    def needed_height(self, width: int) -> int:
        """How tall the tile must be to show all of its description ``width`` wide."""
        return self.layout().totalHeightForWidth(width)

    def sizeHint(self) -> QSize:
        return QSize(200, 80)


class GoalsTab(LayoutTab):
    WIDTH = 760
    STYLE = """
        QPushButton#wlGoal {
            background-color: @surface;
            border: 1px solid @border;
            border-radius: 12px;
            padding: 0;
            text-align: left;
        }
        QPushButton#wlGoal:hover {
            border-color: @border-hover;
        }
        QPushButton#wlGoal:focus {
            border-color: @accent;
        }
        QPushButton#wlGoal:checked {
            background-color: @accent-tint;
            border: 2px solid @accent;
        }
        QLabel#wlGoalIcon {
            background-color: rgba(@overlay-rgb, 0.06);
            border-radius: 10px;
        }
        QLabel#wlGoalName {
            color: @text;
            font-size: 13px;
            font-weight: 700;
            background: transparent;
        }
        QLabel#wlGoalText {
            color: @text-secondary;
            font-size: 12px;
            background: transparent;
        }
        QLabel#wlStripName {
            color: @text-secondary;
            font-size: 12px;
            background: transparent;
        }
    """

    def build(self, page: QVBoxLayout) -> None:
        #: (id, name) of the tiles that are built, so they are rebuilt only when it changes.
        self._goals_shown = None
        self.tiles: list[GoalTile] = []
        self.pills: list[QPushButton] = []

        # ---- what it will be ----
        self.heading = plain()
        head = QVBoxLayout(self.heading)
        head.setContentsMargins(2, 4, 2, 0)
        head.setSpacing(2)
        head.addWidget(label("What are you making?", "wlHeading"))
        head.addWidget(label(
            "Pick how it should come out. You talk and take screenshots the same way for each.",
            "wlHint", wrap=True))
        page.addWidget(self.heading)

        self.grid_box = plain()
        self.grid = QGridLayout(self.grid_box)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(10)
        for column in range(COLUMNS):
            self.grid.setColumnStretch(column, 1)
        page.addWidget(self.grid_box)

        self.grid_box.installEventFilter(self)

        # The same goals as one line of names, for when a storyboard needs the
        # room; Options, set apart at the end, opens the strip the fold hides.
        self.pill_box = plain()
        pills = QHBoxLayout(self.pill_box)
        pills.setContentsMargins(0, 0, 0, 0)
        pills.setSpacing(12)
        names = plain()
        self.pill_flow = FlowLayout(names, h_gap=8, v_gap=8)
        pills.addWidget(names, 1)
        self.options_button = link("Options  ▾", tip="Who writes it up, what's copied "
                                                     "and how it's captured")
        self.options_button.setCheckable(True)
        self.options_button.toggled.connect(lambda _on: self._refresh())
        # Level with the first line of names, however many lines they wrap to.
        aside = QVBoxLayout()
        aside.setContentsMargins(0, 5, 0, 0)
        aside.addWidget(self.options_button)
        aside.addStretch(1)
        pills.addLayout(aside)
        page.addWidget(self.pill_box)

        page.addWidget(self._details())

        # ---- go ----
        buttons = plain()
        row = QHBoxLayout(buttons)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addWidget(self.walkthrough_button, 1)
        row.addWidget(self.cancel_button, 1)
        start = start_row(buttons)
        start.setContentsMargins(0, 4, 0, 0)
        page.addLayout(start)
        self.caption = label("", "wlHint")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        page.addWidget(self.caption)

        keys = QHBoxLayout()
        keys.setSpacing(6)
        keys.addStretch(1)
        keys.addWidget(self.walkthrough_badge)
        keys.addWidget(label("Start / finish", "wlHint"))
        keys.addWidget(self.start_key)
        keys.addSpacing(12)
        keys.addWidget(label("Screenshot", "wlHint"))
        keys.addWidget(self.snap_key)
        keys.addSpacing(18)
        keys.addWidget(self.settings_link)
        keys.addStretch(1)
        page.addLayout(keys)

        page.addWidget(self.review, 1)
        page.addStretch()

    def _details(self) -> QWidget:
        """Everything a goal doesn't settle: one strip for after, one for while recording."""
        self.details = card("sunken")
        body = QVBoxLayout(self.details)
        body.setContentsMargins(14, 10, 14, 10)
        body.setSpacing(8)

        after = plain()
        flow = FlowLayout(after, h_gap=8, v_gap=8)
        self.agent_name_label = self._strip_name("Written by")
        flow.addWidget(self.agent_name_label)
        self.agent_chip = Chip(tone="field", menu=lambda: self.agent_menu(with_off=False))
        flow.addWidget(self.agent_chip)
        self.copy_name_label = self._strip_name("Copies", lead=14)
        flow.addWidget(self.copy_name_label)
        self.copy_chip = Chip(tone="field", menu=self.copy_menu)
        flow.addWidget(self.copy_chip)
        self._customize_gap = self._strip_name("", lead=6)
        flow.addWidget(self._customize_gap)
        self.customize_button = ghost("Customize prompt", checkable=True)
        self.customize_button.setToolTip("See and change exactly what the agent is asked for")
        self.customize_button.toggled.connect(lambda _on: self._refresh())
        flow.addWidget(self.customize_button)
        body.addWidget(after)

        # The base class shows and hides the prompt with the write-up; this
        # box adds whether Customize is open.
        self.prompt_box = plain()
        prompt = QVBoxLayout(self.prompt_box)
        prompt.setContentsMargins(0, 0, 0, 4)
        prompt.addWidget(self.prompt_area)
        # Shorter than on the shipped tab: here it opens between the goals and Start.
        self.prompt_edit.setMaximumHeight(120)
        body.addWidget(self.prompt_box)

        body.addWidget(rule())
        capture = plain()
        flow = FlowLayout(capture, h_gap=8, v_gap=8)
        if _kit.AUTO_MODE_HERE:
            self.auto_switch = bound_switch(self.auto_check, "Auto Mode")
            flow.addWidget(self.auto_switch)
            name = self._strip_name("Auto Mode")
            name.setToolTip(self.auto_check.toolTip())
            flow.addWidget(name)
            flow.addWidget(self._strip_name("", lead=8))
            flow.addWidget(self.capture_delay)
            flow.addWidget(self._strip_name("", lead=8))
        self.minimize_switch = bound_switch(self.minimize_check, "Minimize to tray when it starts")
        flow.addWidget(self.minimize_switch)
        name = self._strip_name("Minimize to tray when it starts")
        name.setToolTip(self.minimize_check.toolTip())
        flow.addWidget(name)
        body.addWidget(capture)
        return self.details

    @staticmethod
    def _strip_name(text: str, *, lead: int = 0) -> QLabel:
        """A quiet name before a control; ``lead`` pixels of air set it off from the one before."""
        name = QLabel(text)
        name.setObjectName("wlStripName")
        if lead:
            name.setContentsMargins(lead, 0, 0, 0)
        return name

    # ---- the goals ----

    def _goals(self) -> list[tuple[str, str]]:
        """(key, name) of every tile, in order."""
        goals = [(style.id, style.name) for style in self.styles]
        if self._own_prompt:
            goals.append((OWN, "My own prompt"))
        goals.append((SPOKEN, "Just as spoken"))
        return goals

    def _chosen(self) -> str:
        if not self.write_up_on:
            return SPOKEN
        style = self.style
        return style.id if style else OWN

    def _choose(self, key: str) -> None:
        if key == SPOKEN:
            self.set_write_up(False)
        else:
            self.choose_style("" if key == OWN else key)
            self.set_write_up(True)
        # A tile clicked while already chosen unchecks itself: check it again.
        self._check_goals()

    def _build_goals(self, goals: list[tuple[str, str]]) -> None:
        for old in (*self.tiles, *self.pills):
            old.setParent(None)
            old.deleteLater()
        self.tiles, self.pills = [], []
        about = {style.id: _shortened(style.instructions) for style in self.styles}
        for index, (key, name) in enumerate(goals):
            icon, text = _BUILT_IN.get(key, (_SAVED_ICON, about.get(key, "")))
            tile = GoalTile(key, name, icon, text)
            tile.clicked.connect(lambda _checked=False, k=key: self._choose(k))
            self.grid.addWidget(tile, index // COLUMNS, index % COLUMNS)
            self.tiles.append(tile)
            pill = ghost(name)
            pill.setCheckable(True)
            pill.setToolTip(text)
            pill.clicked.connect(lambda _checked=False, k=key: self._choose(k))
            self.pill_flow.addWidget(pill)
            self.pills.append(pill)
        self._even_tiles()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.grid_box and event.type() == QEvent.Type.Resize:
            self._even_tiles()
        return super().eventFilter(watched, event)

    def _even_tiles(self) -> None:
        """Make every tile as tall as the one with the most to say at this width."""
        if not self.tiles:
            return
        gaps = self.grid.horizontalSpacing() * (COLUMNS - 1)
        width = max(1, (self.grid_box.width() - gaps) // COLUMNS)
        height = max(tile.needed_height(width) for tile in self.tiles)
        for tile in self.tiles:
            if tile.minimumHeight() != height:
                tile.setFixedHeight(height)

    def _check_goals(self) -> None:
        chosen = self._chosen()
        for (key, _name), tile, pill in zip(self._goals_shown, self.tiles, self.pills, strict=True):
            tile.setChecked(key == chosen)
            pill.setChecked(key == chosen)

    def refresh(self) -> None:
        goals = self._goals()
        if goals != self._goals_shown:
            self._goals_shown = goals
            self._build_goals(goals)
        self._check_goals()

        on = self.write_up_on
        folded = self.has_walkthrough
        self.heading.setVisible(not folded)
        self.grid_box.setVisible(not folded)
        self.pill_box.setVisible(folded)
        options = self.options_button.isChecked()
        self.options_button.setText("Options  ▴" if options else "Options  ▾")
        self.details.setVisible(not folded or options)

        self.agent_chip.set_value(self.agent_name)
        self.copy_chip.set_value(self.copy_label)
        # As spoken, nobody writes it up and there is no prompt to change.
        for widget in (self.agent_name_label, self.agent_chip, self._customize_gap,
                       self.customize_button):
            widget.setVisible(on)
        self.copy_name_label.setContentsMargins(14 if on else 0, 0, 0, 0)
        self.prompt_box.setVisible(on and self.customize_button.isChecked())

        made = dict(goals)[self._chosen()] if on else "As spoken"
        parts = [made]
        if on:
            parts.append(f"written by {self.agent_name}")
        parts.append(f"copies {self.copy_label}")
        if self.walkthrough_active:
            parts.insert(0, "Recording")
        self.caption.setText(" · ".join(parts))
