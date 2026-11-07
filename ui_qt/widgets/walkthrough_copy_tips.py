"""Shared, wrapped hints for walkthrough copy choices and copy actions."""

from html import escape

from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QWidget

from services.settings import WalkthroughCopyFormat

_COPY_TIPS = {
    WalkthroughCopyFormat.PDF: "Copies the PDF file to paste or attach to your chat.",
    WalkthroughCopyFormat.AGENT_TASK: (
        "Copies instructions asking your agent to carry out the work in this walkthrough. "
        "Paste them, then attach the PDF to your chat."
    ),
    WalkthroughCopyFormat.BOTH: (
        "Copies the PDF file and Markdown text together. "
        "The app you paste into chooses which format to use."
    ),
    WalkthroughCopyFormat.AGENT: (
        "Copies Markdown text with a local file link to each screenshot, "
        "for pasting into a terminal agent."
    ),
    WalkthroughCopyFormat.WORD: "Copies a Word (.docx) file you can edit.",
}

_AGENT_ON_FINISH = (
    "Auto-paste on: pastes the PDF, then instructions asking your agent to carry out "
    "the work in it. Leaves the message unsent.",
    "Auto-paste off: copies the instructions. Attach the PDF separately, "
    "or use Paste latest walkthrough.",
)


def set_copy_tip(
    target: QWidget | QAction, copy_format: str, *, on_finish: bool = False
) -> None:
    """Explain the actual copy action, or delivery when a walkthrough finishes.

    A rich-text table gives Qt a wrapping width, including in menus. Keep the
    accessibility description as plain text so readers don't announce markup.
    """
    paragraphs = (
        _AGENT_ON_FINISH
        if on_finish and copy_format == WalkthroughCopyFormat.AGENT_TASK
        else (_COPY_TIPS[copy_format],)
    )
    body = "".join(f"<p>{escape(text)}</p>" for text in paragraphs)
    target.setToolTip(f'<qt><table width="300"><tr><td>{body}</td></tr></table></qt>')
    if isinstance(target, QWidget):
        target.setAccessibleDescription("\n\n".join(paragraphs))
