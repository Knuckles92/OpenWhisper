"""Layout 1, "One sentence": every choice is a word you can click in a plain sentence.

Start sits on top as on the other tabs. Under it, three short sentences say
what will happen, and each value in them opens a menu. Nothing folds away
and nothing is said twice; the prompt opens in a popover from the style menu.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ui_qt.widgets.buttons import start_row
from ui_qt.widgets.walkthrough_layouts import _kit
from ui_qt.widgets.walkthrough_layouts._kit import (
    Chip,
    FlowLayout,
    LayoutTab,
    Popover,
    card,
    label,
    link,
    plain,
    rule,
    step_badge,
)


class _Line(QWidget):
    """One sentence: words and chips that wrap together."""

    def __init__(self):
        super().__init__()
        self.setObjectName("wlPlain")
        self._flow = FlowLayout(self, h_gap=4, v_gap=7)

    def say(self, text: str) -> list[QLabel]:
        """Add words (each wraps on its own); returns their labels."""
        words = []
        for word in text.split():
            piece = QLabel(word)
            piece.setObjectName("wlSentence")
            self._flow.addWidget(piece)
            words.append(piece)
        return words

    def chip(self, chip: Chip) -> Chip:
        self._flow.addWidget(chip)
        return chip

    def end(self, chip: Chip) -> None:
        """Add the sentence's last chip with its full stop, which never wraps away from it."""
        pair = QWidget()
        pair.setObjectName("wlPlain")
        row = QHBoxLayout(pair)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(3)
        row.addWidget(chip)
        stop = QLabel(".")
        stop.setObjectName("wlSentence")
        row.addWidget(stop)
        self._flow.addWidget(pair)


class SentenceTab(LayoutTab):
    STYLE = """
        QLabel#wlSentence {
            color: @text;
            font-size: 15px;
            background: transparent;
        }
        QPushButton#wlChip {
            font-size: 15px;
        }
    """

    def build(self, page: QVBoxLayout) -> None:
        hero = card()
        body = QVBoxLayout(hero)
        body.setContentsMargins(20, 16, 20, 14)
        body.setSpacing(12)

        buttons = plain()
        row = QHBoxLayout(buttons)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addWidget(self.walkthrough_button, 1)
        row.addWidget(self.cancel_button, 1)
        body.addLayout(start_row(buttons))

        keys = QHBoxLayout()
        keys.setSpacing(6)
        keys.addStretch(1)
        keys.addWidget(self.walkthrough_badge)
        keys.addWidget(label("Start / finish", "wlHint"))
        keys.addWidget(self.start_key)
        keys.addSpacing(12)
        keys.addWidget(label("Screenshot", "wlHint"))
        keys.addWidget(self.snap_key)
        keys.addStretch(1)
        body.addLayout(keys)
        body.addWidget(rule())

        # ---- the sentences ----
        capture = _Line()
        if _kit.AUTO_MODE_HERE:
            capture.say("I take the screenshots")
            self.capture_chip = capture.chip(Chip(menu=self.capture_menu))
            capture.say("and each is captured")
            self.delay_chip = Chip(menu=self.delay_menu)
            capture.end(self.delay_chip)
        else:
            self.capture_chip = self.delay_chip = None
            capture.say("I take the screenshots with the shortcut as I talk.")
        body.addWidget(capture)

        after = _Line()
        after.say("When I finish,")
        self.agent_chip = after.chip(Chip(menu=self.agent_menu))
        self._style_words = after.say("writes it up in the")
        self.style_chip = after.chip(Chip(menu=lambda: self.style_menu(edit=self.edit_prompt)))
        self._style_words += after.say("style,")
        self._spoken_words = after.say("writes it up, so it stays as I said it,")
        after.say("and the")
        self.copy_chip = after.chip(Chip(menu=self.copy_menu))
        after.say("is copied.")
        body.addWidget(after)

        window = _Line()
        window.say("While I record, this window")
        self.window_chip = Chip(menu=self.window_menu)
        window.end(self.window_chip)
        body.addWidget(window)

        links = QHBoxLayout()
        links.setSpacing(16)
        links.addStretch(1)
        self.prompt_link = link("Edit the prompt…", self.edit_prompt,
                                "See and change exactly what the agent is asked for")
        links.addWidget(self.prompt_link)
        links.addWidget(self.settings_link)
        body.addLayout(links)
        page.addWidget(hero)

        # The prompt, with its styles and the notes switch, opens from the sentence.
        self.prompt_popover = Popover(self, width=520)
        self.prompt_popover.body.addWidget(self.prompt_area)

        self.how = self._how_it_works()
        page.addWidget(self.how)
        page.addWidget(self.review, 1)
        page.addStretch()

    def _how_it_works(self) -> QWidget:
        strip = plain()
        row = QHBoxLayout(strip)
        row.setContentsMargins(6, 4, 6, 0)
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
        return strip

    def edit_prompt(self) -> None:
        if not self.write_up_on:
            self.set_write_up(True)
        self.prompt_popover.open_under(self.style_chip)

    def refresh(self) -> None:
        if self.capture_chip is not None:
            self.capture_chip.set_value("automatically" if self.auto_on else "myself")
            self.delay_chip.set_value(self.delay_text)
        on = self.write_up_on
        self.agent_chip.set_value(self.agent_name if on else "nobody")
        self.style_chip.set_value(self.style.name if self.style else "my own")
        self.style_chip.setVisible(on)
        for word in self._style_words:
            word.setVisible(on)
        for word in self._spoken_words:
            word.setVisible(not on)
        self.copy_chip.set_value(self.copy_label)
        self.window_chip.set_value("goes to the tray" if self.minimize_on else "stays open")
        self.prompt_link.setVisible(on)
        self.how.setVisible(not self.has_walkthrough)
