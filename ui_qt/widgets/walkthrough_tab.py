"""The Walkthrough tab: start one, choose what happens to it, and work with the last one.

Everything a walkthrough uses that people change between walkthroughs sits
here: whether an agent writes it up, which one, what it is asked for, whether
it follows notes spoken to it, what lands on the clipboard, and whether the
window goes to the tray as one starts. The rest
(shortcuts, how the document looks) stays in Settings → Walkthroughs, one
link away.

Below that, the newest walkthrough shows as a storyboard to fix, reorder and
copy again (``walkthrough_review.py``); one picked in the side panel, which
lists past walkthroughs while this tab is open, shows there instead. With a
walkthrough to show, the choices fold away under Setup and the window widens
for the storyboard, giving the width back on the next tab. See
docs/walkthrough.md.
"""
from __future__ import annotations

from PyQt6.QtCore import QEvent, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
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
from services.walkthrough.write_up import (
    AGENTS,
    DEFAULT_INSTRUCTIONS,
    PRESETS,
    preset_name,
)
from ui_qt.dialogs.settings_destinations import WALKTHROUGHS
from ui_qt.widgets.agent_picker import FOUND, AgentMark, describe_agent
from ui_qt.widgets.buttons import PrimaryButton, WarningButton
from ui_qt.widgets.collapsible_header import CollapsibleSectionToggle
from ui_qt.widgets.feature_badge import FeatureBadge
from ui_qt.widgets.no_wheel import NoWheelComboBox
from ui_qt.widgets.segmented_bar import SegmentedBar
from ui_qt.widgets.settings_switch import SettingsSwitch
from ui_qt.widgets.walkthrough_review import WalkthroughReview
from ui_qt.widgets.wrapped_label import WrappedLabel

#: The preset menu's last item while the prompt is the user's own words.
OWN_PROMPT = "Your own"
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
    QWidget#walkthroughSetupRow, QWidget#walkthroughCopyArea,
    QToolButton#walkthroughSetupToggle {
        background: transparent;
    }
    QLabel#walkthroughSetupSummary {
        color: @text-muted;
        font-size: 12px;
        background: transparent;
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
        #: The user's own prompt, kept while a preset is tried in its place.
        self._own_prompt = ""
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
        column.addWidget(self._write_up_card())
        self.review = WalkthroughReview()
        self.review.setMinimumHeight(REVIEW_MIN_HEIGHT)
        self.review.changed.connect(self._on_review_changed)
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
        body.setContentsMargins(16, 12, 16, 14)
        body.setSpacing(10)

        keys = QHBoxLayout()
        keys.setSpacing(6)
        self.start_key = _key_chip("")
        self.snap_key = _key_chip("")
        for name, chip in (("Start / finish", self.start_key), ("Screenshot", self.snap_key)):
            label = QLabel(name)
            label.setObjectName("walkthroughKeyName")
            keys.addWidget(label)
            keys.addWidget(chip)
            keys.addSpacing(10)
        keys.addStretch(1)
        self.settings_link = QPushButton("Walkthrough settings  →")
        self.settings_link.setObjectName("walkthroughSettingsLink")
        self.settings_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_link.clicked.connect(lambda: self.settings_requested.emit(WALKTHROUGHS))
        keys.addWidget(self.settings_link)
        body.addLayout(keys)

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        self.walkthrough_button = PrimaryButton("Start Walkthrough")
        self.walkthrough_button.setProperty("buttonId", "walkthrough")
        self.walkthrough_button.clicked.connect(self.walkthrough_requested)
        self.walkthrough_badge = FeatureBadge(
            self.walkthrough_button,
            "Talk while you take screenshots, then get one document with your "
            "words and pictures in order, copied to paste into an agent or chat.",
        )
        self.walkthrough_badge.set_new(
            not resolve_walkthrough_intro_seen(settings_manager.load_all_settings()))
        self.walkthrough_button.reserve_beside_label(FeatureBadge.room_beside_label())
        buttons.addWidget(self.walkthrough_button, 1)
        self.cancel_button = WarningButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.cancel_button.hide()
        buttons.addWidget(self.cancel_button, 1)
        body.addLayout(buttons)

        # Shown only with a walkthrough below: then the choices fold away.
        self.setup_row = QWidget()
        self.setup_row.setObjectName("walkthroughSetupRow")
        setup = QHBoxLayout(self.setup_row)
        setup.setContentsMargins(0, 0, 0, 0)
        setup.setSpacing(10)
        self.setup_toggle = CollapsibleSectionToggle(
            "Setup", expand_tooltip="Show what's copied and how it's written up",
            collapse_tooltip="Fold the choices away")
        self.setup_toggle.setObjectName("walkthroughSetupToggle")
        self.setup_toggle.toggled_expanded.connect(self._show_setup)
        setup.addWidget(self.setup_toggle)
        self.setup_summary = QLabel("")
        self.setup_summary.setObjectName("walkthroughSetupSummary")
        setup.addWidget(self.setup_summary, 1)
        self.setup_row.hide()
        body.addWidget(self.setup_row)

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
        self.minimize_check.toggled.connect(
            lambda on: settings_manager.save_setting(SettingsKey.WALKTHROUGH_MINIMIZE_TO_TRAY, on))
        copy.addWidget(self.minimize_check)
        body.addWidget(self.copy_area)
        self.set_hotkeys("", "")
        return hero

    def _write_up_card(self) -> QFrame:
        card = self.write_up_card = QFrame()
        card.setObjectName("engineCard")
        body = QVBoxLayout(card)
        body.setContentsMargins(16, 12, 16, 14)
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
        self.preset_combo.setAccessibleName("Prompt preset")
        for name, _words in PRESETS:
            self.preset_combo.addItem(name)
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
        area.addLayout(options)
        return self.prompt_area

    # ---- state ----

    def load_settings(self) -> None:
        """Show the saved write-up and copy choices without saving them back.

        Typing not yet saved is saved first: it is newer than anything here.
        """
        if self._prompt_timer.isActive():
            self._save_prompt()
        settings = settings_manager.load_all_settings()
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
        """Point the menu at the preset the prompt is, or at the user's own words."""
        text = self.prompt_edit.toPlainText().strip()
        name = preset_name(text)
        if not name:
            self._own_prompt = text
        own = self.preset_combo.findText(OWN_PROMPT)
        if self._own_prompt and own < 0:
            self.preset_combo.addItem(OWN_PROMPT)
            own = self.preset_combo.count() - 1
        self.preset_combo.setCurrentIndex(self.preset_combo.findText(name) if name else own)

    def _on_preset_chosen(self, index: int) -> None:
        # The last item, past the presets, brings back the user's own words.
        words = PRESETS[index][1] if index < len(PRESETS) else self._own_prompt
        if words != self.prompt_edit.toPlainText().strip():
            self.prompt_edit.setPlainText(words)
        self._save_prompt()

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
        """Show ``folder``, or the newest walkthrough, as the storyboard.

        Unsaved edits stay unless ``ask`` is set and the user lets them go;
        False when they stayed, or while the storyboard is saving.
        """
        review = self.review
        if review._busy or (review._dirty and not (ask and review.discard_changes())):
            return False
        if folder is None:
            folders = library.entries(captures_root())
            folder = folders[0] if folders else None
        review.show_folder(folder)
        shown = folder is not None
        review.setVisible(shown)
        self.setup_row.setVisible(shown)
        self._show_setup(self.setup_toggle.is_expanded())
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
        self.copy_area.setVisible(shown)
        self.write_up_card.setVisible(shown)

    def _on_review_changed(self, folder) -> None:
        self.show_walkthrough(folder)
        self.walkthroughs_changed.emit()

    def _sync_setup_summary(self) -> None:
        copy = COPY_CHOICES[self.copy_bar.currentIndex()][1]
        write_up = (f"AI write-up by {AGENT_SPECS[self._agent].name}"
                    if self.write_up_switch.isChecked() else "No AI write-up")
        self.setup_summary.setText(f"Copies {copy}  ·  {write_up}")
