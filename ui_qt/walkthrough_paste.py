"""Two clipboard pastes for walkthrough auto-paste and the dedicated shortcut.

Qt owns the clipboard throughout. Returning to its event loop between pastes
lets the receiving app read the PDF before we replace it with the instructions.
No ordinary paste shortcut is intercepted and no message is submitted.
"""

from __future__ import annotations

import logging
import uuid

from PyQt6.QtCore import QObject, QTimer

from services.focus_context import same_target
from services.settings import WalkthroughCopyFormat
from services.walkthrough import library, storage
from ui_qt.clipboard import AUTO_PASTE_MARKER_FORMAT

logger = logging.getLogger(__name__)
PDF_READ_DELAY_MS = 800


def latest_result():
    """Newest completed capture; ignore newer drafts, but report broken exports."""
    for folder in library.entries(storage.captures_root()):
        if (folder / storage.MANIFEST_NAME).is_file():
            return library.load_result(folder)
    return None


class WalkthroughPaste(QObject):
    def __init__(self, clipboard, writer, *, identity, send_paste, report,
                 parent=None):
        super().__init__(parent)
        self.clipboard = clipboard
        self.writer = writer
        self.identity = identity
        self.send_paste = send_paste
        self.report = report
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._paste_instructions)
        self.busy = False
        self._token = b""
        self._target = None
        self._prompt = ""

    def start(self, result, target) -> None:
        from services.hotkey_manager import is_accessibility_trusted
        from services.synthetic_keys import modifiers_held

        if self.busy:
            return
        if not is_accessibility_trusted():
            self.report("Walkthrough not pasted: enable Accessibility permission to auto-paste")
            return
        if target is None or target.is_self or not same_target(self.identity(), target):
            self.report("Walkthrough not pasted: focus changed or the destination is unavailable")
            return
        if not result.pdf.is_file():
            self.report("Walkthrough not pasted: the PDF is missing")
            return
        self._target = target
        self._prompt = result.clipboard(WalkthroughCopyFormat.AGENT_TASK)[1]
        self._token = uuid.uuid4().bytes
        try:
            if modifiers_held():
                self.cancel()
                self.report("Walkthrough not pasted: modifier keys are held")
                return
            if not self.writer.write_files([result.pdf], marker=self._token):
                raise RuntimeError("couldn't copy the PDF")
            self.busy = True
            self.send_paste()
            self.timer.start(PDF_READ_DELAY_MS)
        except Exception as exc:
            self.cancel()
            logger.exception("Could not paste walkthrough PDF")
            self.report(f"Couldn't paste the walkthrough PDF: {exc}")

    def _paste_instructions(self) -> None:
        if not self.busy:
            return
        try:
            if not same_target(self.identity(), self._target):
                self.report("PDF paste sent; instructions stopped because focus changed")
                return
            mime = self.clipboard.mimeData()
            owned = mime is not None and bytes(mime.data(AUTO_PASTE_MARKER_FORMAT)) == self._token
            # A Windows delayed-render read may advance the OS sequence. The
            # unique marker survives our own rendering, but not a new copy.
            if not owned:
                self.report("PDF paste sent; instructions stopped because the clipboard changed")
                return
            # New modifiers pressed during the PDF's read window must not turn
            # Ctrl+V into a different command. This check never waits on Qt.
            from services.synthetic_keys import modifiers_held

            if modifiers_held():
                self.report("PDF paste sent; instructions stopped because modifier keys are held")
                return
            if not self.writer.write_text(self._prompt):
                raise RuntimeError("couldn't copy the instructions")
            self.send_paste()
            self.report("PDF and instructions pasted; review them before sending")
        except Exception as exc:
            logger.exception("Could not paste walkthrough instructions")
            self.report(f"PDF paste sent; couldn't paste instructions: {exc}")
        finally:
            self.cancel()

    def cancel(self) -> None:
        self.timer.stop()
        self.busy = False
        self._token = b""
        self._target = None
        self._prompt = ""
