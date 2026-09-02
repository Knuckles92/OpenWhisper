"""A label whose text resolves into place one character at a time.

Characters appear left to right; the few just ahead of the resolved run cycle
through random glyphs in the accent color before locking. The animation starts
when the label is actually shown, because the caller often has the text before
the label is on screen (a result arrives while a progress panel still covers
the row it belongs to) and an animation nobody saw is a wasted one.
"""
from __future__ import annotations

import html
import random
import time
from typing import Final, Sequence

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QLabel

#: (text, emphasized) runs. Emphasized runs render bright and bold once locked.
Segments = Sequence[tuple[str, bool]]

_GLYPHS: Final[str] = "0123456789ABCDEF#%&$@<>/\\|"
_TICK_MS: Final[int] = 32
_DURATION_MS: Final[int] = 720
#: Characters still scrambling ahead of the locked run.
_HEAD: Final[int] = 4

_EMPHASIS_STYLE: Final[str] = "color:#f5f5f7; font-weight:600"
_SCRAMBLE_STYLE: Final[str] = "color:#0a84ff"


class DecodeLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.RichText)
        self._segments: list[tuple[str, bool]] = []
        self._final_html = ""
        self._pending_reveal = False
        self._started_at = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)

    def set_segments(self, segments: Segments, animate: bool = True) -> None:
        """Set the text to show.

        Args:
            segments: Runs of text with an emphasis flag each.
            animate: Reveal the text when the label is next visible, or show it
                at once.
        """
        self._segments = [(text, bool(emphasized)) for text, emphasized in segments]
        self._final_html = self._render(len(self._plain_text()))
        self._timer.stop()
        if not animate:
            self._pending_reveal = False
            QLabel.setText(self, self._final_html)
            return
        self._pending_reveal = True
        QLabel.setText(self, "")
        if self.isVisible():
            self.reveal()

    def reveal(self) -> None:
        """Start (or restart) the animation now."""
        self._pending_reveal = False
        self._started_at = time.monotonic()
        self._timer.start()
        self._tick()

    def clear(self) -> None:
        self._timer.stop()
        self._segments = []
        self._final_html = ""
        self._pending_reveal = False
        QLabel.clear(self)

    def text(self) -> str:
        """The finished text, regardless of how far the reveal has run."""
        return self._final_html

    @property
    def is_revealing(self) -> bool:
        return self._timer.isActive()

    def showEvent(self, event):
        super().showEvent(event)
        if self._pending_reveal:
            self.reveal()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._timer.isActive():
            self._timer.stop()
            QLabel.setText(self, self._final_html)

    def _plain_text(self) -> str:
        return "".join(text for text, _ in self._segments)

    def _tick(self) -> None:
        total = len(self._plain_text())
        elapsed_ms = (time.monotonic() - self._started_at) * 1000.0
        progress = min(1.0, elapsed_ms / _DURATION_MS)
        locked = int(round(total * progress))
        if progress >= 1.0:
            self._timer.stop()
            QLabel.setText(self, self._final_html)
            return
        QLabel.setText(self, self._render(locked, scramble_head=_HEAD))

    def _render(self, locked: int, scramble_head: int = 0) -> str:
        parts: list[str] = []
        start = 0
        for text, emphasized in self._segments:
            end = start + len(text)
            locked_run = text[: max(0, min(len(text), locked - start))]
            if locked_run:
                escaped = html.escape(locked_run)
                if emphasized:
                    parts.append(f"<span style='{_EMPHASIS_STYLE}'>{escaped}</span>")
                else:
                    parts.append(escaped)
            head_from = max(start, locked)
            head_to = min(end, locked + scramble_head)
            for char in text[head_from - start : max(head_from, head_to) - start]:
                if char == " ":
                    parts.append(" ")
                else:
                    glyph = html.escape(random.choice(_GLYPHS))
                    parts.append(f"<span style='{_SCRAMBLE_STYLE}'>{glyph}</span>")
            start = end
        # Rich text collapses runs of spaces, which would close up the
        # separators, so pin them.
        return "".join(parts).replace("  ", "&nbsp;&nbsp;")
