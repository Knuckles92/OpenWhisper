"""The Walkthrough tab: start one, choose what happens to it, and work with the last one.

Everything a walkthrough uses that people change between walkthroughs sits
here: whether an agent writes it up, which one, what it is asked for, whether
it follows notes spoken to it, what lands on the clipboard, and whether the
window goes to the tray as one starts. The rest
(shortcuts, how the document looks) stays in Settings → Walkthroughs, one
link away.

Below that, the newest walkthrough shows as a storyboard to fix, reorder and
copy again (``walkthrough_review.py``); one picked in the side panel, which
lists past walkthroughs while this tab is open, shows there instead. Beside the
Start button, a summary says what happens when a walkthrough finishes. With a
walkthrough to show, the choices fold away behind the summary's Change, and the
window widens for the storyboard, giving the width back on the next tab. See
docs/walkthrough.md.
"""
from __future__ import annotations

from PyQt6.QtCore import QEvent, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.installed_agents import AGENT_SPECS
from services.settings import (
    SettingsKey,
    WalkthroughCopyFormat,
    resolve_walkthrough_copy_format,
    resolve_walkthrough_intro_seen,
    resolve_walkthrough_minimize_to_tray,
    resolve_walkthrough_write_up,
    resolve_walkthrough_write_up_agent,
    resolve_walkthrough_write_up_instructions,
    resolve_walkthrough_write_up_notes,
    settings_manager,
)
from services.walkthrough import library
from services.walkthrough.storage import captures_root
from services.walkthrough.styles import (
    DEFAULT_STYLE,
    delete_style,
    load_styles,
    save_style,
    style_for,
)
from services.walkthrough.write_up import AGENTS, DEFAULT_INSTRUCTIONS
from ui_qt.dialogs.settings_destinations import WALKTHROUGHS
from ui_qt.widgets.agent_picker import FOUND, AgentMark, describe_agent
from ui_qt.widgets.buttons import SuccessButton, WarningButton, start_row
from ui_qt.widgets.feature_badge import FeatureBadge
from ui_qt.widgets.no_wheel import NoWheelComboBox
from ui_qt.widgets.segmented_bar import SegmentedBar
from ui_qt.widgets.settings_switch import SettingsSwitch
from ui_qt.widgets.walkthrough_review import WalkthroughReview
from ui_qt.widgets.wrapped_label import WrappedLabel

#: The style menu's item while the prompt is the user's own words.
OWN_PROMPT = "Your own"
#: The style menu's last item, which makes a style from scratch.
NEW_STYLE = "New style…"
#: Its item data: not a style id (hex or a slug) and not "", the user's own.
_NEW_STYLE_DATA = ":new"
#: The storyboard never shrinks below this, so a couple of rows always show.
REVIEW_MIN_HEIGHT = 380
#: How wide the page asks the window to be while a storyboard shows: room
#: for each screenshot at a size you can read beside a comfortable line of words.
STORYBOARD_WIDTH = 900
#: How long typing in the prompt pauses before it is saved.
PROMPT_SAVE_DELAY_MS = 400

#: Copy choices in the order the bar shows them, short enough that the four
#: share one row at the window's minimum width.
COPY_CHOICES = (
    (WalkthroughCopyFormat.PDF, "PDF"),
    (WalkthroughCopyFormat.BOTH, "PDF + Markdown"),
    (WalkthroughCopyFormat.AGENT, "Markdown"),
    (WalkthroughCopyFormat.WORD, "Word"),
)

_STYLE = """
    QFrame#walkthroughHero {
        background-color: @surface;
        border: 1px solid @border;
        border-radius: 14px;
    }
    QLabel#walkthroughKeyName {
        color: @text-secondary;
        font-size: 12px;
        background: transparent;
    }
    QLabel#walkthroughKey {
        color: @text;
        background-color: @slate-field;
        border: 1px solid @slate-border;
        border-bottom-width: 2px;
        border-radius: 6px;
        padding: 1px 7px;
        font-size: 12px;
        font-weight: 600;
    }
    QLabel#walkthroughKey[unset="true"] {
        color: @text-muted;
        font-weight: 400;
    }
    QCheckBox#walkthroughMinimizeCheck {
        color: @text-secondary;
        font-size: 12px;
        spacing: 7px;
        background: transparent;
    }
    QCheckBox#walkthroughMinimizeCheck::indicator {
        width: 15px;
        height: 15px;
        border-radius: 4px;
        border: 1px solid @border-strong;
        background-color: @bg;
    }
    QCheckBox#walkthroughMinimizeCheck::indicator:hover {
        border-color: @accent;
    }
    QCheckBox#walkthroughMinimizeCheck::indicator:checked {
        background-color: @accent;
        border-color: @accent;
        image: url(ui_qt/assets/check.svg);
    }
    QWidget#walkthroughCopyArea {
        background: transparent;
    }
    QWidget#walkthroughSplit,
    QWidget#walkthroughKeys,
    QWidget#walkthroughKeyPair,
    QWidget#walkthroughStartColumn,
    QWidget#walkthroughSummary,
    QWidget#walkthroughSetupPanel,
    QFrame#walkthroughSetupSection {
        background: transparent;
        border: none;
    }
    QFrame#walkthroughRule {
        background-color: @border;
        border: none;
    }
    QLabel#walkthroughOverline {
        color: @text-secondary;
        font-size: 11px;
        font-weight: 700;
        background: transparent;
    }
    QLabel#walkthroughSummaryLine {
        color: @text;
        font-size: 13px;
        background: transparent;
    }
    QLabel#walkthroughSummaryLine[muted="true"] {
        color: @text-secondary;
    }
    QLabel#walkthroughSectionTitle {
        color: @text;
        font-size: 14px;
        font-weight: 700;
        background: transparent;
    }
    QPushButton#walkthroughAgent {
        background-color: @slate-raised;
        border: 1px solid @slate-border;
        border-radius: 12px;
        padding: 0;
        text-align: left;
    }
    QPushButton#walkthroughAgent:hover {
        border-color: @slate-border-hover;
    }
    QPushButton#walkthroughAgent:checked {
        background-color: @accent-tint;
        border: 2px solid @accent;
    }
    QPushButton#walkthroughAgent:focus {
        border-color: @accent;
    }
    QPushButton#walkthroughAgent:disabled {
        background-color: @slate-field;
        border-color: @slate-border-subtle;
    }
    QLabel#walkthroughAgentName {
        color: @text;
        font-size: 13px;
        font-weight: 700;
        background: transparent;
    }
    QLabel#walkthroughAgentStatus {
        color: @text-muted;
        font-size: 11px;
        background: transparent;
    }
    QLabel#walkthroughAgentStatus[tone="found"] {
        color: @success-text;
    }
    QLabel#walkthroughAgentStatus[tone="warning"] {
        color: @warning-text;
    }
    QWidget#walkthroughPromptArea {
        background: transparent;
    }
    QFrame#walkthroughPromptRule {
        background-color: @border;
        border: none;
        max-height: 1px;
    }
    QLabel#walkthroughPromptTitle {
        color: @text;
        font-size: 13px;
        font-weight: 700;
        background: transparent;
    }
    QComboBox#walkthroughPreset {
        padding: 4px 10px;
        font-size: 12px;
        min-width: 150px;
    }
    QPushButton#walkthroughStyleLink {
        color: @accent;
        background-color: transparent;
        border: none;
        font-size: 12px;
        font-weight: 600;
        padding: 2px 0;
        min-height: 0;
    }
    QPushButton#walkthroughStyleLink:hover,
    QPushButton#walkthroughStyleLink:focus {
        color: @accent-soft;
        text-decoration: underline;
    }
    QPlainTextEdit#walkthroughPrompt {
        color: @text;
        background-color: @slate-field;
        border: 1px solid @slate-border;
        border-radius: 10px;
        padding: 6px 8px;
        font-size: 13px;
    }
    QPlainTextEdit#walkthroughPrompt:focus {
        border-color: @accent;
    }
"""


def _key_chip(text: str) -> QLabel:
    chip = QLabel(text)
    chip.setObjectName("walkthroughKey")
    return chip


class AgentChoice(QPushButton):
    """One agent that can write a walkthrough up: its mark, name and whether it's here."""

    def __init__(self, agent: str, parent=None):
        super().__init__(parent)
        spec = AGENT_SPECS[agent]
        self.agent = agent
        self.setObjectName("walkthroughAgent")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"Write up with {spec.name}")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)
        self.mark = AgentMark(spec.monogram, spec.accent, parent=self)
        row.addWidget(self.mark, alignment=Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(1)
        self.name_label = QLabel(spec.name)
        self.name_label.setObjectName("walkthroughAgentName")
        text.addWidget(self.name_label)
        self.status_label = QLabel("Checking…")
        self.status_label.setObjectName("walkthroughAgentStatus")
        text.addWidget(self.status_label)
        row.addLayout(text, 1)
        for child in (self.mark, self.name_label, self.status_label):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def sizeHint(self) -> QSize:
        return QSize(150, self.layout().sizeHint().height())

    def show_found(self, agent) -> None:
        """Paint what the last scan said about this agent (None: not installed)."""
        state = describe_agent(self.agent, agent)
        self.status_label.setText(state.pill)
        self.status_label.setProperty("tone", state.tone)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.mark.set_warmth(1.0 if state.tone == FOUND else 0.35)
        self.mark.set_missing(0.0 if agent is not None else 1.0)
        self.setToolTip(state.detail or state.pill)


def _rule(vertical: bool = False) -> QFrame:
    rule = QFrame()
    rule.setObjectName("walkthroughRule")
    if vertical:
        rule.setFixedWidth(1)
    else:
        rule.setFixedHeight(1)
    return rule


class _Flip(QWidget):
    """Widgets side by side and centred while they fit, else one a line.

    Like ``Reflow``, its own minimum width is ignored: the width comes from
    the column, so a long shortcut can't hold the page wider than its window.
    """

    def __init__(self, items, gap: int, parent=None):
        super().__init__(parent)
        self.setObjectName("walkthroughKeys")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._items = list(items)
        self._gap = gap
        self._box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._stacked = None
        self._place(False)

    @property
    def stacked(self) -> bool:
        return bool(self._stacked)

    def _place(self, stacked: bool) -> None:
        if stacked == self._stacked:
            return
        self._stacked = stacked
        while self._box.count():
            self._box.takeAt(0)
        if stacked:
            self._box.setDirection(QBoxLayout.Direction.TopToBottom)
            self._box.setSpacing(6)
            for item in self._items:
                self._box.addWidget(item, 0, Qt.AlignmentFlag.AlignHCenter)
        else:
            self._box.setDirection(QBoxLayout.Direction.LeftToRight)
            self._box.setSpacing(self._gap)
            self._box.addStretch(1)
            for item in self._items:
                self._box.addWidget(item)
            self._box.addStretch(1)
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        needed = (sum(item.sizeHint().width() for item in self._items)
                  + self._gap * (len(self._items) - 1))
        self._place(self.width() < needed)


class StartSplit(QWidget):
    """Start on the left and what happens when it finishes on the right.

    Side by side while both fit at their narrowest, the summary under Start
    once they don't (a narrow window or a large font), so neither forces the
    page wider than its window. Its own minimum width is ignored, as
    ``Reflow``'s is: the width comes from the card.
    """

    def __init__(self, start: QWidget, summary: QWidget, parent=None):
        super().__init__(parent)
        self.setObjectName("walkthroughSplit")
        # Its height is its content's: the start column's centring stretch
        # would otherwise take the page's spare height.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._start = start
        self._summary = summary
        self._across = _rule(vertical=True)
        self._under = _rule()
        self._box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(0)
        self._box.addWidget(start, 3)
        self._box.addWidget(self._across)
        self._box.addWidget(self._under)
        self._box.addWidget(summary, 2)
        self._stacked = None
        self._place(False)

    @property
    def stacked(self) -> bool:
        return bool(self._stacked)

    def _place(self, stacked: bool) -> None:
        if stacked == self._stacked:
            return
        self._stacked = stacked
        self._box.setDirection(
            QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight)
        # The 3:2 split is across; stacked, each takes its own height.
        self._box.setStretchFactor(self._start, 0 if stacked else 3)
        self._box.setStretchFactor(self._summary, 0 if stacked else 2)
        self._across.setVisible(not stacked)
        self._under.setVisible(stacked)
        self.updateGeometry()

    def refit(self) -> None:
        """Side by side or stacked for the width it has now (after its text changed)."""
        needed = (self._start.minimumSizeHint().width() + 1
                  + self._summary.minimumSizeHint().width())
        self._place(self.width() < needed)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refit()


class WalkthroughTab(QWidget):
    """Start or finish a walkthrough, and set up what follows it."""

    #: Start a walkthrough, or finish the one recording.
    walkthrough_requested = pyqtSignal()
    #: Throw the recording walkthrough away.
    cancel_requested = pyqtSignal()
    settings_requested = pyqtSignal(str)
    #: The storyboard saved, rewrote or deleted a walkthrough: lists of them are stale.
    walkthroughs_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("walkthroughTab")
        self.setStyleSheet(_STYLE)
        self.is_recording = False
        self.walkthrough_active = False
        self._scan_token = 0
        #: The user's own prompt, kept while a style is tried in its place.
        self._own_prompt = ""
        self._styles = load_styles(settings_manager.load_all_settings())
        self._prompt_timer = QTimer(self)
        self._prompt_timer.setSingleShot(True)
        self._prompt_timer.setInterval(PROMPT_SAVE_DELAY_MS)
        self._prompt_timer.timeout.connect(self._save_prompt)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("transcriptionTabScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(self.scroll_area)
        content = QWidget()
        content.setObjectName("walkthroughTabContent")
        self.scroll_area.setWidget(content)
        column = QVBoxLayout(content)
        column.setContentsMargins(24, 6, 24, 16)
        column.setSpacing(12)
        column.addWidget(self._hero())
        # Sets the newest walkthrough apart from the card that starts the next.
        self.review_rule = _rule()
        self.review_rule.hide()
        column.addWidget(self.review_rule)
        self.review = WalkthroughReview(closable=True)
        self.review.setMinimumHeight(REVIEW_MIN_HEIGHT)
        self.review.changed.connect(self._on_review_changed)
        self.review.close_requested.connect(self.close_walkthrough)
        self.review.hide()
        column.addWidget(self.review, 1)
        column.addStretch()

        self.load_settings()
        self._show_agents()
        self.show_walkthrough()

    # ---- building ----

    def _hero(self) -> QFrame:
        hero = QFrame()
        hero.setObjectName("walkthroughHero")
        body = QVBoxLayout(hero)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.split = StartSplit(self._start_column(), self._summary_column())
        body.addWidget(self.split)
        body.addWidget(self._setup_panel())
        self.set_hotkeys("", "")
        return hero

    def _start_column(self) -> QWidget:
        column = QWidget()
        column.setObjectName("walkthroughStartColumn")
        body = QVBoxLayout(column)
        body.setContentsMargins(16, 14, 16, 14)
        body.setSpacing(10)
        body.addStretch(1)

        # Start fills the shared Start width; while a walkthrough runs, Finish
        # and Cancel split it.
        button_box = QWidget()
        buttons = QHBoxLayout(button_box)
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(12)
        self.walkthrough_button = SuccessButton("Start Walkthrough")
        self.walkthrough_button.setProperty("buttonId", "walkthrough")
        self.walkthrough_button.clicked.connect(self.walkthrough_requested)
        buttons.addWidget(self.walkthrough_button, 1)
        self.cancel_button = WarningButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.cancel_button.hide()
        buttons.addWidget(self.cancel_button, 1)
        body.addLayout(start_row(button_box))

        # Leads the shortcut row rather than sitting on the Start button, so
        # the button matches the other tabs' Start buttons.
        self.walkthrough_badge = FeatureBadge(
            None,
            "Talk while you take screenshots, then get one document with your "
            "words and pictures in order, copied to paste into an agent or chat.",
            column,
        )
        self.walkthrough_badge.set_new(
            not resolve_walkthrough_intro_seen(settings_manager.load_all_settings()))
        self.start_key = _key_chip("")
        self.snap_key = _key_chip("")
        pairs = []
        for lead, name, chip in ((self.walkthrough_badge, "Start / finish", self.start_key),
                                 (None, "Screenshot", self.snap_key)):
            pair = QWidget()
            pair.setObjectName("walkthroughKeyPair")
            row = QHBoxLayout(pair)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            if lead is not None:
                row.addWidget(lead, 0, Qt.AlignmentFlag.AlignVCenter)
            label = QLabel(name)
            label.setObjectName("walkthroughKeyName")
            row.addWidget(label)
            row.addWidget(chip)
            pairs.append(pair)
        # One row under Start while it fits; in a narrow column, one pair a line.
        self.keys = _Flip(pairs, gap=16)
        body.addWidget(self.keys)
        body.addStretch(1)
        return column

    def _summary_column(self) -> QWidget:
        """What happens when a walkthrough finishes, and the way to change it."""
        column = QWidget()
        column.setObjectName("walkthroughSummary")
        body = QVBoxLayout(column)
        body.setContentsMargins(16, 12, 16, 12)
        body.setSpacing(6)
        heading = QLabel("WHEN IT FINISHES")
        heading.setObjectName("walkthroughOverline")
        font = heading.font()
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 108)
        heading.setFont(font)
        body.addWidget(heading)
        self.copy_summary = self._summary_line()
        body.addWidget(self.copy_summary)
        self.write_up_summary = self._summary_line()
        body.addWidget(self.write_up_summary)
        self.minimize_summary = self._summary_line()
        self.minimize_summary.setText("Goes to the tray as it starts")
        body.addWidget(self.minimize_summary)
        body.addStretch(1)

        links = QHBoxLayout()
        links.setSpacing(14)
        # Shown only with a walkthrough below: then the choices fold away.
        self.setup_toggle = QPushButton()
        self.setup_toggle.setObjectName("walkthroughSettingsLink")
        self.setup_toggle.setCheckable(True)
        self.setup_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setup_toggle.toggled.connect(self._show_setup)
        self.setup_toggle.hide()
        links.addWidget(self.setup_toggle)
        self.settings_link = QPushButton("Walkthrough settings  →")
        self.settings_link.setObjectName("walkthroughSettingsLink")
        self.settings_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_link.setToolTip("Shortcuts and how the document looks")
        self.settings_link.clicked.connect(lambda: self.settings_requested.emit(WALKTHROUGHS))
        links.addWidget(self.settings_link)
        links.addStretch(1)
        body.addLayout(links)
        return column

    @staticmethod
    def _summary_line() -> QLabel:
        line = QLabel()
        line.setObjectName("walkthroughSummaryLine")
        line.setTextFormat(Qt.TextFormat.RichText)
        # Wraps rather than widening the column: a long agent name shouldn't
        # push the summary under Start.
        line.setWordWrap(True)
        return line

    def _setup_panel(self) -> QWidget:
        """The choices themselves, open under the split until a walkthrough shows."""
        self.setup_panel = QWidget()
        self.setup_panel.setObjectName("walkthroughSetupPanel")
        panel = QVBoxLayout(self.setup_panel)
        panel.setContentsMargins(0, 0, 0, 0)
        panel.setSpacing(0)
        panel.addWidget(_rule())
        inner = QVBoxLayout()
        inner.setContentsMargins(16, 12, 16, 14)
        inner.setSpacing(12)
        panel.addLayout(inner)

        # With or without a write-up, this is what lands on the clipboard. The
        # label sits above so the four choices keep one row at the minimum width.
        self.copy_area = QWidget()
        self.copy_area.setObjectName("walkthroughCopyArea")
        copy = QVBoxLayout(self.copy_area)
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(6)
        copy_label = QLabel("Copy when it finishes")
        copy_label.setObjectName("walkthroughKeyName")
        copy.addWidget(copy_label)
        self.copy_bar = SegmentedBar([(label, "") for _value, label in COPY_CHOICES], compact=True)
        self.copy_bar.setAccessibleName("What's copied when a walkthrough finishes")
        self.copy_bar.setToolTip(
            "Markdown is text with a file link to each screenshot, for pasting into a "
            "terminal agent. PDF + Markdown copies both at once. Word copies a .docx "
            "you can edit.")
        self.copy_bar.activated.connect(self._on_copy_chosen)
        copy.addWidget(self.copy_bar)
        copy.addSpacing(4)

        self.minimize_check = QCheckBox("Minimize to tray when it starts")
        self.minimize_check.setObjectName("walkthroughMinimizeCheck")
        self.minimize_check.setToolTip(
            "Gets this window out of the way of what you're showing. "
            "Open it again from the tray icon.")
        self.minimize_check.toggled.connect(self._on_minimize_toggled)
        copy.addWidget(self.minimize_check)
        inner.addWidget(self.copy_area)
        inner.addWidget(_rule())
        inner.addWidget(self._write_up_card())
        return self.setup_panel

    def _write_up_card(self) -> QFrame:
        card = self.write_up_card = QFrame()
        card.setObjectName("walkthroughSetupSection")
        body = QVBoxLayout(card)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        title = QLabel("AI write-up")
        title.setObjectName("walkthroughSectionTitle")
        titles.addWidget(title)
        self.write_up_text = WrappedLabel("")
        self.write_up_text.setObjectName("infoLabel")
        titles.addWidget(self.write_up_text)
        head.addLayout(titles, 1)
        self.write_up_switch = SettingsSwitch()
        self.write_up_switch.setAccessibleName("Write walkthroughs up with an AI agent")
        self.write_up_switch.toggled.connect(self._on_write_up_toggled)
        head.addWidget(self.write_up_switch, alignment=Qt.AlignmentFlag.AlignTop)
        body.addLayout(head)

        agents = QHBoxLayout()
        agents.setSpacing(10)
        self.agent_choices = {}
        for agent in AGENTS:
            choice = AgentChoice(agent)
            choice.clicked.connect(lambda _checked=False, a=agent: self._choose_agent(a))
            agents.addWidget(choice)
            self.agent_choices[agent] = choice
        body.addLayout(agents)
        body.addWidget(self._prompt_area())
        return card

    def _prompt_area(self) -> QWidget:
        """What the agent is asked for, and whether it listens to spoken notes.

        Shown only while the write-up is on: with it off there is no agent to ask.
        """
        self.prompt_area = QWidget()
        self.prompt_area.setObjectName("walkthroughPromptArea")
        area = QVBoxLayout(self.prompt_area)
        area.setContentsMargins(0, 2, 0, 0)
        area.setSpacing(8)
        rule = QFrame()
        rule.setObjectName("walkthroughPromptRule")
        rule.setFixedHeight(1)
        area.addWidget(rule)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("Prompt")
        title.setObjectName("walkthroughPromptTitle")
        head.addWidget(title)
        head.addStretch(1)
        start = QLabel("Start from")
        start.setObjectName("walkthroughKeyName")
        head.addWidget(start)
        self.preset_combo = NoWheelComboBox()
        self.preset_combo.setObjectName("walkthroughPreset")
        self.preset_combo.setAccessibleName("Write-up style")
        self.preset_combo.activated.connect(self._on_preset_chosen)
        head.addWidget(self.preset_combo)
        area.addLayout(head)

        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setObjectName("walkthroughPrompt")
        self.prompt_edit.setAccessibleName("Write-up prompt")
        self.prompt_edit.setPlaceholderText(DEFAULT_INSTRUCTIONS)
        self.prompt_edit.setTabChangesFocus(True)
        self.prompt_edit.setMinimumHeight(96)
        self.prompt_edit.setMaximumHeight(190)
        self.prompt_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.prompt_edit.textChanged.connect(self._on_prompt_edited)
        area.addWidget(self.prompt_edit)

        options = QHBoxLayout()
        options.setSpacing(8)
        self.notes_switch = SettingsSwitch()
        self.notes_switch.setAccessibleName("Follow notes left for the agent")
        self.notes_switch.toggled.connect(
            lambda on: settings_manager.save_setting(SettingsKey.WALKTHROUGH_WRITE_UP_NOTES, on))
        options.addWidget(self.notes_switch)
        self.notes_label = QLabel("Follow notes I leave for the agent")
        self.notes_label.setObjectName("walkthroughKeyName")
        self.notes_label.setToolTip(
            "Say \"note for the agent: skip the setup part\" while you talk, and the "
            "agent follows it instead of writing it into the document.")
        options.addWidget(self.notes_label)
        options.addStretch(1)
        self.save_style_button = self._style_link(
            "Save as style…", "Keep these words as a style to pick here and in Redo as",
            self._save_as_style)
        options.addWidget(self.save_style_button)
        self.delete_style_button = self._style_link(
            "Delete style", "Remove this style from the list; the words stay in the box",
            self._delete_style)
        options.addWidget(self.delete_style_button)
        area.addLayout(options)
        return self.prompt_area

    def _style_link(self, text: str, tip: str, slot) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("walkthroughStyleLink")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tip)
        button.clicked.connect(slot)
        return button

    # ---- state ----

    def load_settings(self) -> None:
        """Show the saved write-up and copy choices without saving them back.

        Typing not yet saved is saved first: it is newer than anything here.
        """
        if self._prompt_timer.isActive():
            self._save_prompt()
        settings = settings_manager.load_all_settings()
        self._styles = load_styles(settings)
        quiet = (self.write_up_switch, self.notes_switch, self.prompt_edit, self.minimize_check)
        for widget in quiet:
            widget.blockSignals(True)
        self.write_up_switch.setChecked(resolve_walkthrough_write_up(settings))
        self.notes_switch.setChecked(resolve_walkthrough_write_up_notes(settings))
        self.minimize_check.setChecked(resolve_walkthrough_minimize_to_tray(settings))
        prompt = resolve_walkthrough_write_up_instructions(settings) or DEFAULT_INSTRUCTIONS
        if self.prompt_edit.toPlainText().strip() != prompt:
            # Only when it differs: setting it moves the cursor to the start.
            self.prompt_edit.setPlainText(prompt)
        for widget in quiet:
            widget.blockSignals(False)
        self._show_preset()
        self._agent = resolve_walkthrough_write_up_agent(settings)
        copy = resolve_walkthrough_copy_format(settings)
        self.copy_bar.setCurrentIndex(
            next(i for i, (value, _label) in enumerate(COPY_CHOICES) if value == copy))
        self._sync_write_up()

    def _on_write_up_toggled(self, on: bool) -> None:
        settings_manager.save_setting(SettingsKey.WALKTHROUGH_WRITE_UP, on)
        self._sync_write_up()

    def _choose_agent(self, agent: str) -> None:
        self._agent = agent
        settings_manager.save_setting(SettingsKey.WALKTHROUGH_WRITE_UP_AGENT, agent)
        if not self.write_up_switch.isChecked():
            # Picking an agent is asking for a write-up.
            self.write_up_switch.setChecked(True)
        self._sync_write_up()

    def _on_minimize_toggled(self, on: bool) -> None:
        settings_manager.save_setting(SettingsKey.WALKTHROUGH_MINIMIZE_TO_TRAY, on)
        self._sync_setup_summary()

    def _on_copy_chosen(self, index: int) -> None:
        settings_manager.save_setting(SettingsKey.WALKTHROUGH_COPY_FORMAT, COPY_CHOICES[index][0])
        self._sync_setup_summary()

    def _sync_write_up(self) -> None:
        on = self.write_up_switch.isChecked()
        name = AGENT_SPECS[self._agent].name
        self.write_up_text.setText(
            f"{name} lays out a clean document from it. The as-spoken PDF is kept too."
            if on else "Off: you get it as spoken. Pick an agent to turn it on.")
        for agent, choice in self.agent_choices.items():
            choice.setChecked(on and agent == self._agent)
        self.prompt_area.setVisible(on)
        self._sync_setup_summary()

    # ---- the prompt ----

    def _on_prompt_edited(self) -> None:
        self._prompt_timer.start()
        self._show_preset()

    def _save_prompt(self) -> None:
        """Save the prompt; the default's own words save as "", like an empty box."""
        self._prompt_timer.stop()
        text = self.prompt_edit.toPlainText().strip()
        settings_manager.save_setting(
            SettingsKey.WALKTHROUGH_WRITE_UP_INSTRUCTIONS,
            "" if text == DEFAULT_INSTRUCTIONS else text)

    def _show_preset(self) -> None:
        """Point the menu at the style the prompt is, or at the user's own words.

        The menu is rebuilt each time: styles are saved and deleted here and
        from the storyboard's Redo with a new prompt.
        """
        text = self.prompt_edit.toPlainText().strip()
        style = style_for(text, self._styles)
        if style is None:
            self._own_prompt = text
        combo = self.preset_combo
        combo.clear()
        for each in self._styles:
            combo.addItem(each.name, each.id)
        if self._own_prompt:
            combo.addItem(OWN_PROMPT, "")
        combo.insertSeparator(combo.count())
        combo.addItem(NEW_STYLE, _NEW_STYLE_DATA)
        combo.setCurrentIndex(combo.findData(style.id if style else ""))
        self.save_style_button.setVisible(style is None and bool(text))
        self.delete_style_button.setVisible(style is not None and style != DEFAULT_STYLE)

    def _on_preset_chosen(self, index: int) -> None:
        # Past the styles: the user's own words, then New style….
        style_id = self.preset_combo.itemData(index)
        if style_id == _NEW_STYLE_DATA:
            self._new_style()
            return
        words = next((style.instructions for style in self._styles if style.id == style_id),
                     self._own_prompt)
        if words != self.prompt_edit.toPlainText().strip():
            self.prompt_edit.setPlainText(words)
        self._save_prompt()

    def _new_style(self) -> None:
        """Make a style from scratch and use it; cancelled, the menu goes back."""
        from ui_qt.dialogs.new_style_dialog import NewStyleDialog

        dialog = NewStyleDialog(self)
        if dialog.exec() != NewStyleDialog.DialogCode.Accepted:
            self._show_preset()
            return
        self._styles = load_styles(settings_manager.load_all_settings())
        # Its words fill the box, which points the menu at it, as picking a style does.
        self.prompt_edit.setPlainText(dialog.saved.instructions)
        self._save_prompt()

    def _save_as_style(self) -> None:
        text = self.prompt_edit.toPlainText().strip()
        name, ok = QInputDialog.getText(
            self, "Save as style", "Name the style. A name already saved is replaced.")
        if not ok or not name.strip():
            return
        try:
            settings_manager.mutate_settings(lambda settings: save_style(settings, name, text))
        except ValueError as exc:
            QMessageBox.warning(self, "Save as style", str(exc))
            return
        self._styles = load_styles(settings_manager.load_all_settings())
        self._show_preset()

    def _delete_style(self) -> None:
        style = style_for(self.prompt_edit.toPlainText(), self._styles)
        if style is None or style == DEFAULT_STYLE or QMessageBox.question(
            self, "Delete style", f"Delete the style \"{style.name}\"? Its words stay in the box.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        settings_manager.mutate_settings(lambda settings: delete_style(settings, style.id))
        self._styles = load_styles(settings_manager.load_all_settings())
        self._show_preset()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        # Settings → Walkthroughs edits the same choices; show them on return.
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.load_settings()

    def _show_agents(self) -> None:
        """Label each agent with the last scan; scan once if none has run."""
        from ui_qt.widgets import action_relay, agent_picker

        found = agent_picker.cached_agents()
        if found is None:
            if not self._scan_token:
                action_relay.relay().scan_done.connect(self._on_scan_done)
                self._scan_token = token = action_relay.next_token()
                action_relay.start_agent_scan(token)
            return
        for agent, choice in self.agent_choices.items():
            choice.show_found(found.get(agent))

    def _on_scan_done(self, token: int) -> None:
        if token == self._scan_token:
            self._show_agents()

    def set_hotkeys(self, walkthrough_key: str, screenshot_key: str) -> None:
        for chip, key in ((self.start_key, walkthrough_key), (self.snap_key, screenshot_key)):
            chip.setText(key or "Not set")
            chip.setProperty("unset", not key)
            chip.style().unpolish(chip)
            chip.style().polish(chip)
        self.walkthrough_button.set_hotkey(walkthrough_key)

    def set_recording_state(self, is_recording: bool, walkthrough_active: bool) -> None:
        """A walkthrough records: Finish and Cancel. A dictation records: wait for it."""
        self.is_recording = is_recording
        self.walkthrough_active = walkthrough_active
        if walkthrough_active and self.walkthrough_badge.text() == FeatureBadge.NEW:
            settings_manager.save_setting(SettingsKey.WALKTHROUGH_INTRO_SEEN, True)
            self.walkthrough_badge.set_new(False)
        self.walkthrough_button.setText(
            "Finish Walkthrough" if walkthrough_active else "Start Walkthrough")
        self.walkthrough_button.set_active(walkthrough_active or not is_recording)
        self.cancel_button.setVisible(walkthrough_active)

    # ---- the storyboard ----

    def show_walkthrough(self, folder=None, *, ask: bool = False) -> bool:
        """Show ``folder``, or the newest walkthrough unless it was closed, as the storyboard.

        Unsaved edits stay unless ``ask`` is set and the user lets them go;
        False when they stayed, or while the storyboard is saving.
        """
        if folder is None:
            newest = self._newest()
            closed = settings_manager.load_all_settings().get(SettingsKey.WALKTHROUGH_CLOSED)
            folder = newest if newest is not None and newest.name != closed else None
        return self._show(folder, ask=ask)

    def close_walkthrough(self) -> None:
        """Put the storyboard away until a newer walkthrough finishes."""
        newest = self._newest()
        if self._show(None, ask=True) and newest is not None:
            settings_manager.save_setting(SettingsKey.WALKTHROUGH_CLOSED, newest.name)

    @staticmethod
    def _newest():
        folders = library.entries(captures_root())
        return folders[0] if folders else None

    def _show(self, folder, *, ask: bool) -> bool:
        review = self.review
        if review._busy or (review._dirty and not (ask and review.discard_changes())):
            return False
        review.show_folder(folder)
        shown = folder is not None
        review.setVisible(shown)
        self.review_rule.setVisible(shown)
        self.setup_toggle.setVisible(shown)
        self._show_setup(self.setup_toggle.isChecked())
        return True

    def fills_height(self) -> bool:
        """With a storyboard, the page takes whatever height the window is given."""
        return not self.review.isHidden()

    def preferred_width(self) -> int:
        """With a storyboard, the window widens to show it (0: as wide as the window is)."""
        return STORYBOARD_WIDTH if self.fills_height() else 0

    def _show_setup(self, expanded: bool) -> None:
        # With nothing below to make room for, the choices are always open.
        shown = expanded or self.review.isHidden()
        self.setup_panel.setVisible(shown)
        self.setup_toggle.setText("Done  ▴" if expanded else "Change  ▾")
        self.setup_toggle.setToolTip("Fold the choices away" if expanded
                                     else "Change what's copied and how it's written up")

    def _on_review_changed(self, folder) -> None:
        self.show_walkthrough(folder)
        self.walkthroughs_changed.emit()

    def _sync_setup_summary(self) -> None:
        copy = COPY_CHOICES[self.copy_bar.currentIndex()][1]
        self.copy_summary.setText(f"Copies <b>{copy}</b>")
        on = self.write_up_switch.isChecked()
        self.write_up_summary.setText(
            f"AI write-up by <b>{AGENT_SPECS[self._agent].name}</b>" if on else "No AI write-up")
        self.write_up_summary.setProperty("muted", not on)
        self.write_up_summary.style().unpolish(self.write_up_summary)
        self.write_up_summary.style().polish(self.write_up_summary)
        # Unticked is the usual case: only say so when it will happen.
        self.minimize_summary.setVisible(self.minimize_check.isChecked())
        self.split.refit()
