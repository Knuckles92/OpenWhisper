"""Layout 2, "Three stages": the tab as a pipeline read left to right.

One card, three equal columns: how it's captured, who writes it up, and what
lands on the clipboard. Each choice sits in the stage it belongs to, in the
order things happen, so nothing is said twice and "before" options no longer
mix with "after" ones. Start runs along the bottom of the card.

With a walkthrough open the columns fold into one slim strip (Start, then a
line per stage saying what's chosen) so the storyboard gets the room; Edit
unfolds them in place.
"""
from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from services.settings import WalkthroughCopyFormat
from services.walkthrough.write_up import AGENTS
from ui_qt.widgets.buttons import START_BUTTON_WIDTH
from ui_qt.widgets.walkthrough_copy_tips import set_copy_tip
from ui_qt.widgets.walkthrough_layouts import _kit
from ui_qt.widgets.walkthrough_layouts._kit import (
    Chip,
    LayoutTab,
    Popover,
    bound_switch,
    card,
    ghost,
    label,
    link,
    plain,
    restyle,
    rule,
    step_badge,
)
from ui_qt.widgets.walkthrough_tab import COPY_CHOICES

#: What each copy choice gives you, in a few words, under its name.
_COPY_HINTS = {
    WalkthroughCopyFormat.PDF: "A file to paste or attach",
    WalkthroughCopyFormat.AGENT_TASK: "Agent instructions, plus the PDF",
    WalkthroughCopyFormat.BOTH: "Both; the app you paste into picks",
    WalkthroughCopyFormat.AGENT: "Text with a link to each screenshot",
    WalkthroughCopyFormat.WORD: "A .docx you can edit",
}
#: Start's width in the folded strip: alone, and split with Cancel.
_STRIP_START, _STRIP_START_CANCEL = 200, 300
#: Padding inside each column.
_PAD = 18


class _CopyRow(QPushButton):
    """One copy choice as a radio row: a dot, its name, and what it gives you."""

    def __init__(self, name: str, hint: str):
        super().__init__()
        self.setObjectName("wlCopyRow")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(f"Copy {name}")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 7, 10, 7)
        row.setSpacing(10)
        self.dot = QLabel()
        self.dot.setObjectName("wlRadio")
        self.dot.setFixedSize(16, 16)
        row.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        words = QVBoxLayout()
        words.setSpacing(0)
        words.addWidget(label(name, "wlTitle2"))
        words.addWidget(label(hint, "wlHint"))
        row.addLayout(words, 1)
        for child in self.findChildren(QLabel):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def sizeHint(self) -> QSize:
        return QSize(160, self.layout().sizeHint().height())

    def show_chosen(self, on: bool) -> None:
        self.setChecked(on)
        if self.dot.property("on") != on:
            self.dot.setProperty("on", on)
            restyle(self.dot)


class StagesTab(LayoutTab):
    WIDTH = 900
    STYLE = """
        QLabel#wlArrow {
            color: @text-muted;
            font-size: 17px;
            background: transparent;
        }
        QLabel#wlStageSummary {
            color: @accent-soft;
            font-size: 12px;
            font-weight: 600;
            background: transparent;
        }
        QPushButton#wlCopyRow {
            background-color: transparent;
            border: 1px solid transparent;
            border-radius: 10px;
            padding: 0;
            min-height: 0;
            text-align: left;
        }
        QPushButton#wlCopyRow:hover, QPushButton#wlCopyRow:focus {
            background-color: rgba(@overlay-rgb, 0.05);
        }
        QPushButton#wlCopyRow:checked {
            background-color: @accent-tint;
            border-color: @accent-tint-border;
        }
        QLabel#wlRadio {
            border: 2px solid @border-strong;
            border-radius: 8px;
            background: transparent;
        }
        QLabel#wlRadio[on="true"] {
            border: 5px solid @accent;
            background-color: @on-accent;
        }
        QPushButton#wlChip[tone="field"]:disabled {
            color: @text-muted;
        }
        QLabel#wlBody:disabled, QLabel#wlHint:disabled,
        QLabel#walkthroughKeyName:disabled {
            color: @text-muted;
        }
        QPushButton#wlLink:disabled {
            color: @text-muted;
        }
    """

    def build(self, page: QVBoxLayout) -> None:
        # Start and Cancel travel together: under the columns, or in the strip.
        self._buttons = plain()
        buttons = QHBoxLayout(self._buttons)
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(12)
        buttons.addWidget(self.walkthrough_button, 1)
        buttons.addWidget(self.cancel_button, 1)
        self._buttons.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        #: Where the buttons are now ("footer" or "strip"), and what was last arranged.
        self._buttons_in = ""
        self._arranged = None

        hero = card()
        body = QVBoxLayout(hero)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._strip())
        self.strip_rule = rule()
        body.addWidget(self.strip_rule)

        self.columns = plain()
        columns = QHBoxLayout(self.columns)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(0)
        for index, column in enumerate((self._capture(), self._write_up(), self._deliver())):
            if index:
                columns.addWidget(rule(vertical=True))
            # Equal thirds whatever each holds: the width comes from the card.
            column.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            columns.addWidget(column, 1)
        body.addWidget(self.columns)

        self.footer_rule = rule()
        body.addWidget(self.footer_rule)
        self.footer = plain()
        self.footer_row = QHBoxLayout(self.footer)
        self.footer_row.setContentsMargins(_PAD, 14, _PAD, 14)
        self.footer_row.setSpacing(0)
        self.footer_row.addStretch()
        self.footer_row.addStretch()
        body.addWidget(self.footer)
        page.addWidget(hero)

        # The prompt, with its styles and save/delete links, opens from the style row.
        self.prompt_popover = Popover(self, width=520)
        self.prompt_popover.body.addWidget(label("What the agent is asked for", "wlTitle2"))
        self.prompt_popover.body.addWidget(self.prompt_area)

        page.addWidget(self.review, 1)
        page.addStretch()

        # Neither saves through a method that ends in a refresh.
        self.auto_check.toggled.connect(lambda _on: self._refresh())
        self.capture_delay.changed.connect(lambda _seconds: self._refresh())

    # ---- the folded strip ----

    def _strip(self) -> QWidget:
        """Start, a line per stage, and Edit: all there is while a walkthrough shows."""
        self.strip = plain()
        self.strip_row = QHBoxLayout(self.strip)
        self.strip_row.setContentsMargins(_PAD, 12, _PAD, 12)
        self.strip_row.setSpacing(12)
        self.strip_row.addSpacing(4)
        self.strip_summaries = []
        for number in (1, 2, 3):
            if number > 1:
                self.strip_row.addWidget(label("→", "wlArrow"))
            self.strip_row.addWidget(step_badge(number, quiet=True))
            summary = label("", "wlBody")
            self.strip_summaries.append(summary)
            self.strip_row.addWidget(summary)
        self.strip_row.addStretch(1)
        self.edit_button = ghost("Edit", lambda _on: self._refresh(), checkable=True)
        self.edit_button.setToolTip("Change how it's captured, written up and copied")
        self.strip_row.addWidget(self.edit_button)
        return self.strip

    # ---- the three stages ----

    def _column(self, number: int, title: str, *, arrow: bool) -> tuple[QWidget, QVBoxLayout, QLabel]:
        """A stage: its numbered header with a live summary, and the column under it."""
        column = plain()
        body = QVBoxLayout(column)
        body.setContentsMargins(_PAD, 16, _PAD, 16)
        body.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(step_badge(number), 0, Qt.AlignmentFlag.AlignVCenter)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(label(title, "wlOverline"))
        summary = label("", "wlStageSummary")
        titles.addWidget(summary)
        head.addLayout(titles, 1)
        if arrow:
            # Points at the next stage: the three read as one line.
            head.addWidget(label("→", "wlArrow"), 0, Qt.AlignmentFlag.AlignVCenter)
        body.addLayout(head)
        body.addWidget(rule())
        return column, body, summary

    @staticmethod
    def _row(title: str, hint: str, control: QWidget) -> QHBoxLayout:
        """A choice: what it is on the left, its control on the right."""
        row = QHBoxLayout()
        row.setSpacing(10)
        words = QVBoxLayout()
        words.setSpacing(0)
        words.addWidget(label(title, "wlBody"))
        if hint:
            words.addWidget(label(hint, "wlHint"))
        row.addLayout(words, 1)
        row.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _capture(self) -> QWidget:
        column, body, self.capture_summary = self._column(1, "CAPTURE", arrow=True)
        self.auto_switch = bound_switch(self.auto_check, "Auto Mode")
        self.minimize_switch = bound_switch(self.minimize_check, "Minimize to tray when it starts")
        # Read here, not imported by name: it is the platform's answer at build time.
        if _kit.AUTO_MODE_HERE:
            body.addLayout(self._row("Auto Mode", "Capture clicks and drawings", self.auto_switch))
            body.addLayout(self._row("Capture delay", "", self.capture_delay))
        body.addLayout(self._row("Minimize to tray", "Out of the way as it starts",
                                 self.minimize_switch))
        body.addWidget(rule())
        for name, key in (("Start / finish", self.start_key), ("Screenshot", self.snap_key)):
            keys = QHBoxLayout()
            keys.setSpacing(8)
            keys.addWidget(label(name, "wlHint"), 1)
            keys.addWidget(key)
            body.addLayout(keys)
        body.addStretch(1)
        # The one place the tab says what a walkthrough is.
        body.addWidget(label("Talk while you take screenshots. Each one lands beside "
                             "what you were saying.", "wlHint", wrap=True))
        return column

    def _write_up(self) -> QWidget:
        column, body, self.write_up_summary_line = self._column(2, "WRITE-UP", arrow=True)
        body.addLayout(self._row("AI write-up", "An agent tidies it up",
                                 self.write_up_switch))
        agents = QVBoxLayout()
        agents.setSpacing(8)
        for agent in AGENTS:
            agents.addWidget(self.agent_choices[agent])
        body.addLayout(agents)

        # What the agent is asked for: only means something with the write-up on.
        self.write_up_details = plain()
        details = QVBoxLayout(self.write_up_details)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(10)
        self.style_chip = Chip(tone="field", menu=lambda: self.style_menu(edit=self.edit_prompt))
        self.style_chip.setAccessibleName("Write-up style")
        details.addLayout(self._row("Style", "", self.style_chip))
        notes = QHBoxLayout()
        notes.setSpacing(8)
        notes.addWidget(self.notes_switch, 0, Qt.AlignmentFlag.AlignTop)
        # A third of the card is narrower than this label's one line.
        self.notes_label.setWordWrap(True)
        notes.addWidget(self.notes_label, 1)
        details.addLayout(notes)
        links = QHBoxLayout()
        self.prompt_link = link("Edit the prompt…", self.edit_prompt,
                                "See and change exactly what the agent is asked for")
        links.addWidget(self.prompt_link)
        links.addStretch(1)
        details.addLayout(links)
        body.addWidget(self.write_up_details)
        body.addStretch(1)
        return column

    def _deliver(self) -> QWidget:
        column, body, self.deliver_summary = self._column(3, "DELIVER", arrow=False)
        body.addWidget(label("Lands on your clipboard", "wlBody"))
        rows = QVBoxLayout()
        rows.setSpacing(4)
        self.copy_rows = []
        for index, (value, name) in enumerate(COPY_CHOICES):
            row = _CopyRow(name, _COPY_HINTS[value])
            set_copy_tip(row, value, on_finish=True)
            row.clicked.connect(lambda _checked=False, i=index: self.choose_copy(i))
            rows.addWidget(row)
            self.copy_rows.append(row)
        body.addLayout(rows)
        body.addStretch(1)
        links = QHBoxLayout()
        links.addStretch(1)
        links.addWidget(self.settings_link)
        body.addLayout(links)
        return column

    # ---- behaviour ----

    def edit_prompt(self) -> None:
        if not self.write_up_on:
            self.set_write_up(True)
        self.prompt_popover.open_under(self.style_chip)
        # The style links come and go while the popover is shut, and a hidden
        # window measures them only once shown: fit and place it again.
        self.prompt_popover.open_under(self.style_chip)

    def _arrange(self) -> None:
        """Columns with Start under them, or the strip (and the columns again on Edit)."""
        has = self.has_walkthrough
        if not has and self.edit_button.isChecked():
            # Nothing to fold for: the next walkthrough starts folded.
            self.edit_button.blockSignals(True)
            self.edit_button.setChecked(False)
            self.edit_button.blockSignals(False)
        unfolded = not has or self.edit_button.isChecked()
        state = (has, unfolded, self.walkthrough_active)
        if state == self._arranged:
            return
        self._arranged = state
        home = "strip" if has else "footer"
        if home != self._buttons_in:
            self._buttons_in = home
            if has:
                self.strip_row.insertWidget(0, self._buttons)
            else:
                self.footer_row.insertWidget(1, self._buttons, 1)
            self._buttons.show()
        if has:
            # Finish and Cancel need the room one Start doesn't.
            self._buttons.setFixedWidth(
                _STRIP_START_CANCEL if self.walkthrough_active else _STRIP_START)
        else:
            self._buttons.setMinimumWidth(0)
            self._buttons.setMaximumWidth(START_BUTTON_WIDTH)
        self.strip.setVisible(has)
        self.strip_rule.setVisible(has and unfolded)
        self.columns.setVisible(unfolded)
        self.footer_rule.setVisible(not has)
        self.footer.setVisible(not has)
        self.edit_button.setText("Done" if self.edit_button.isChecked() else "Edit")

    def refresh(self) -> None:
        if _kit.AUTO_MODE_HERE:
            capture = f"{'Auto' if self.auto_on else 'Manual'} · {self.delay_text}"
        else:
            capture = "With the shortcut"
        if self.minimize_on:
            capture += " · to tray"
        on = self.write_up_on
        write_up = f"{self.agent_name} · {self.style_name}" if on else "Off · as spoken"
        deliver = self.copy_label
        for line, strip, text in zip(
                (self.capture_summary, self.write_up_summary_line, self.deliver_summary),
                self.strip_summaries, (capture, write_up, deliver), strict=True):
            line.setText(text)
            strip.setText(text)
        self.style_chip.set_value(self.style_name)
        # The capture can't change mid-recording; the switch says so as its checkbox does.
        self.auto_switch.setEnabled(self.auto_check.isEnabled())
        self.write_up_details.setEnabled(on)
        for index, row in enumerate(self.copy_rows):
            row.show_chosen(index == self.copy_index)
        self._arrange()
