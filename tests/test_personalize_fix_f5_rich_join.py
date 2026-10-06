"""A formatted snippet's rich text gets the same caret join as its plain text."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import QApplication, QTextEdit

from services import dictation_pipeline, focus_context
from services.dictation_pipeline import DictationJob, JobMode
from services.focus_context import FocusSnapshot, TextContext, join_html_with_context
from services.settings import SettingsKey
from tests.test_personalize_foundation_runtime import OUTLOOK, FakeService, h  # noqa: F401
from ui_qt.clipboard import _text_mime_data

CALENDAR = {
    "id": "cal", "trigger": "my calendar link",
    "text": "**[my calendar](https://cal.example/sam)**", "formatted": True,
}
SIGNATURE = {"id": "sig", "trigger": "my sign off", "text": "**Dana Lee**", "formatted": True}


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


def _caret(before, after=""):
    return TextContext(before=before, after=after, caret_known=True, selection_known=True)


def _deliver(harness, raw, before, after=""):
    harness.settings.values[SettingsKey.DICTATION_SNIPPETS] = [CALENDAR, SIGNATURE]
    focus_context.set_service(FakeService(FocusSnapshot(OUTLOOK, _caret(before, after))))
    job = dictation_pipeline.begin_job(JobMode.DICTATION, {})
    assert harness.runtime._claim_job(job)
    transcript, raw_text, info = harness.runtime._maybe_cleanup_transcript(raw)
    harness.runtime.on_transcription_complete(transcript, raw_text, info)
    (args, kwargs), = harness.ui.stages
    return args[0], kwargs.get("html", "")


def _rich_paste(before, plain, html):
    """What a rich-text editor shows after pasting both flavours at the end of ``before``."""
    editor = QTextEdit()
    editor.setPlainText(before)
    editor.moveCursor(QTextCursor.MoveOperation.End)
    editor.insertFromMimeData(_text_mime_data(plain, html))
    return editor.toPlainText().replace(" ", " ")


def test_a_rich_paste_continues_the_sentence_like_the_plain_one(h):  # noqa: F811 (pytest fixture)
    plain, html = _deliver(h, "Here is my calendar link.", "Hi Sam,")

    assert plain == " here is my calendar (https://cal.example/sam)."
    assert _rich_paste("Hi Sam,", plain, html) == "Hi Sam, here is my calendar."
    assert '<a href="https://cal.example/sam">' in html


def test_a_rich_paste_drops_the_period_before_more_of_the_sentence(h):  # noqa: F811 (pytest fixture)
    plain, html = _deliver(h, "Here is my calendar link.", "Thanks", " and talk soon")

    assert plain == " here is my calendar (https://cal.example/sam)"
    assert _rich_paste("Thanks", plain, html) == "Thanks here is my calendar"


def test_a_whole_formatted_snippet_gets_its_leading_space(h):  # noqa: F811 (pytest fixture)
    plain, html = _deliver(h, "My sign off.", "Regards,")

    assert plain == " Dana Lee"
    assert _rich_paste("Regards,", plain, html) == "Regards, Dana Lee"
    assert "<b>Dana Lee</b>" in html


def test_html_is_unchanged_without_a_known_caret(h):  # noqa: F811 (pytest fixture)
    h.settings.values[SettingsKey.DICTATION_SNIPPETS] = [SIGNATURE]
    assert h.runtime._claim_job(DictationJob())
    transcript, _raw, _info = h.runtime._maybe_cleanup_transcript("My sign off.")
    h.runtime.on_transcription_complete(transcript)

    assert h.ui.stages == [(("Dana Lee",), {"html": "<b>Dana Lee</b>"})]


@pytest.mark.parametrize("before,after,text,html,joined", [
    ("and then I think", "", "We should check see the doc.",
     "We should check see <b>the doc</b>.", "&nbsp;we should check see <b>the doc</b>."),
    ("and then I think", " by Friday", "We should check see the doc.",
     "We should check see <b>the doc</b>.", "&nbsp;we should check see <b>the doc</b>"),
    ("Buy", "Next sentence.", "Milk.", "<i>Milk.</i>", "&nbsp;<i>Milk.</i>&nbsp;"),
    ("Dear Sam, ", " ", " Thanks. ", " <b>Thanks.</b> ", "<b>thanks.</b>"),
    # A first word the markup doesn't start with is left as it is.
    ("Thanks,", "", "Talk soon.", "<b>Dana</b> talk soon.", "&nbsp;<b>Dana</b> talk soon."),
    # Periods inside the markup are text; an ellipsis is never cut.
    ("Wait", " and see", "What...", "<b>What...</b>", "&nbsp;<b>what...</b>"),
    # A snippet that starts with a list is never joined, in either flavour.
    ("Hello", "", "- Milk", "<ul><li>Milk</li></ul>", "<ul><li>Milk</li></ul>"),
])
def test_join_html_with_context(before, after, text, html, joined):
    assert join_html_with_context(html, text, _caret(before, after)) == joined


@pytest.mark.parametrize("context", [None, TextContext(before="Dear Sam,", caret_known=False)])
def test_join_html_leaves_html_alone_without_a_known_caret(context):
    assert join_html_with_context("<b>Hi</b>", "Hi", context) == "<b>Hi</b>"


def test_html_for_paste_only_joins_live_dictation(monkeypatch):
    focus_context.set_service(FakeService(FocusSnapshot(OUTLOOK, _caret("Dear Sam,"))))
    dictation = dictation_pipeline.begin_job(JobMode.DICTATION, {})
    command = dictation_pipeline.begin_job(JobMode.COMMAND, {})

    assert dictation_pipeline.html_for_paste("<b>Thanks</b>", "Thanks", dictation) == (
        "&nbsp;<b>thanks</b>")
    assert dictation_pipeline.html_for_paste("<b>Thanks</b>", "Thanks", command) == "<b>Thanks</b>"
    assert dictation_pipeline.html_for_paste("<b>Thanks</b>", "Thanks", None) == "<b>Thanks</b>"
    assert dictation_pipeline.html_for_paste("", "Thanks", dictation) == ""

    monkeypatch.setattr(focus_context, "join_html_with_context", lambda *args: 1 / 0)
    assert dictation_pipeline.html_for_paste("<b>Thanks</b>", "Thanks", dictation) == "<b>Thanks</b>"
