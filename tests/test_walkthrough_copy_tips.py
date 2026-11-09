"""Copy hints survive UI construction and describe each hovered action."""

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextDocument

from services.settings import WalkthroughCopyFormat
from ui_qt.widgets.walkthrough_ready import WalkthroughReadyCard
from ui_qt.widgets.walkthrough_tab import COPY_CHOICES, WalkthroughTab


def _plain(tip):
    document = QTextDocument()
    document.setHtml(tip)
    return document.toPlainText()


@pytest.mark.parametrize("desktop", ["classic", "omarchy"])
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
def test_each_copy_choice_and_the_chosen_one_explain_themselves(
    monkeypatch, desktop, copy_format, expected
):
    monkeypatch.setenv("OPENWHISPER_UI", desktop)
    tab = WalkthroughTab()
    label = next(text for value, text in COPY_CHOICES if value == copy_format)
    try:
        menu = tab.copy_field.build_menu()
        assert menu.toolTipsVisible()
        action = next(a for a in menu.actions() if a.text() == label)
        assert expected in _plain(action.toolTip())

        # Chosen, the field explains it on hover and to screen readers.
        action.trigger()
        assert expected in _plain(tab.copy_field.toolTip())
        assert (_plain(tab.copy_field.toolTip()).split()
                == tab.copy_field.accessibleDescription().split())
    finally:
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
        agent_choice = next(a for a in tab.copy_field.build_menu().actions()
                            if a.text() == "Agent")
        finish_hint = _plain(agent_choice.toolTip())
        assert "Auto-paste off:" in finish_hint and "unsent" in finish_hint
    finally:
        tab.close()
        card.close()
