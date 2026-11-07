"""Copy hints survive UI construction and describe each hovered action."""

import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QHelpEvent, QTextDocument
from PyQt6.QtWidgets import QApplication, QToolTip

from services.settings import WalkthroughCopyFormat
from ui_qt.widgets.walkthrough_ready import WalkthroughReadyCard
from ui_qt.widgets.walkthrough_tab import COPY_CHOICES, WalkthroughTab


def _plain(tip):
    document = QTextDocument()
    document.setHtml(tip)
    return document.toPlainText()


@pytest.mark.parametrize("desktop", ["classic", "omarchy"])
@pytest.mark.parametrize("width", [380, 930])
@pytest.mark.parametrize(
    "copy_format,expected",
    [
        (WalkthroughCopyFormat.PDF, "PDF file"),
        (WalkthroughCopyFormat.AGENT_TASK, "Auto-paste on:"),
        (WalkthroughCopyFormat.BOTH, "chooses which format"),
        (WalkthroughCopyFormat.AGENT, "local file link"),
        (WalkthroughCopyFormat.WORD, ".docx"),
    ],
)
def test_hovering_each_copy_label_shows_its_own_hint(
    monkeypatch, desktop, width, copy_format, expected
):
    monkeypatch.setenv("OPENWHISPER_UI", desktop)
    tab = WalkthroughTab()
    tab.resize(width, 700)
    tab.show()
    QApplication.processEvents()
    initial = tab.copy_bar.currentIndex()
    index = next(i for i, (value, _) in enumerate(COPY_CHOICES) if value == copy_format)
    button = tab.copy_bar.buttons[index]
    label = button._title
    pos = label.rect().center()
    try:
        # Hovering the text must reach its button's tooltip too.
        QApplication.sendEvent(
            label, QHelpEvent(QEvent.Type.ToolTip, pos, label.mapToGlobal(pos))
        )
        shown = _plain(QToolTip.text())
        assert QToolTip.isVisible()
        assert expected in shown
        assert shown.split() == button.accessibleDescription().split()
        assert tab.copy_bar.currentIndex() == initial
    finally:
        QToolTip.hideText()
        tab.close()


@pytest.mark.parametrize("desktop", ["classic", "omarchy"])
def test_manual_copy_hints_match_in_review_and_ready_card(monkeypatch, desktop):
    monkeypatch.setenv("OPENWHISPER_UI", desktop)
    tab = WalkthroughTab()
    card = WalkthroughReadyCard()
    review = tab.review
    try:
        assert card.testAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        assert card.menu.toolTipsVisible() and review.menu.toolTipsVisible()
        for action, control in (
            (card.copy_pdf_action, review.pdf_button),
            (card.copy_word_action, review.copy_word_button),
            (card.copy_markdown_action, review.markdown_button),
            (card.copy_agent_action, review.copy_agent_action),
        ):
            assert action.toolTip() == control.toolTip()
        agent_hint = _plain(card.copy_agent_action.toolTip())
        assert "attach the PDF" in agent_hint
        assert "Auto-paste" not in agent_hint
        finish_hint = tab.copy_bar.buttons[1].accessibleDescription()
        assert "Auto-paste off:" in finish_hint and "unsent" in finish_hint
    finally:
        tab.close()
        card.close()
