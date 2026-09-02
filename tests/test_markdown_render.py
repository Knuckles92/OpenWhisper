import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QTextDocument, QTextFormat
from PyQt6.QtWidgets import QApplication

from ui_qt.utils.markdown_render import (
    PREVIEW_STYLE,
    READER_STYLE,
    MarkdownStyle,
    render_markdown,
)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication([])


SAMPLE = """# Title

Intro with **bold**, `code`, and a [link](https://example.com).

## recording.mp3

- one
- two

> quoted

```
line one
line two
```

| a | b |
|---|---|
| 1 | 2 |
"""


def _blocks(document):
    block = document.begin()
    while block.isValid():
        yield block
        block = block.next()


def _block_named(document, text):
    for block in _blocks(document):
        if block.text() == text:
            return block
    raise AssertionError(f"no block with text {text!r}")


def _fragments(block):
    fragment = block.begin()
    while not fragment.atEnd():
        yield fragment.fragment()
        fragment += 1


def _render(text, style=PREVIEW_STYLE):
    document = QTextDocument()
    render_markdown(document, text, style)
    return document


class TestRenderMarkdown:
    def test_headings_scale_from_the_body_size(self):
        document = _render(SAMPLE)
        title = _block_named(document, "Title")
        section = _block_named(document, "recording.mp3")
        assert title.blockFormat().headingLevel() == 1
        title_pt = next(_fragments(title)).charFormat().fontPointSize()
        section_pt = next(_fragments(section)).charFormat().fontPointSize()
        assert title_pt == PREVIEW_STYLE.heading_pt(1) > section_pt
        assert section_pt == PREVIEW_STYLE.heading_pt(2) > PREVIEW_STYLE.body_pt

    def test_inline_code_gets_a_chip_and_links_the_accent(self):
        document = _render(SAMPLE)
        intro = _block_named(document, "Intro with bold, code, and a link.")
        by_text = {f.text(): f.charFormat() for f in _fragments(intro)}
        assert by_text["code"].background().color().name() == PREVIEW_STYLE.code_background
        assert by_text["code"].fontFixedPitch()
        assert by_text["bold"].fontWeight() > by_text[", "].fontWeight()
        assert by_text["link"].isAnchor()
        assert by_text["link"].foreground().color().name() == "#4da3ff"

    def test_fenced_code_is_one_slab_with_the_gap_after_its_last_line(self):
        document = _render(SAMPLE)
        first = _block_named(document, "line one").blockFormat()
        last = _block_named(document, "line two").blockFormat()
        assert first.background().color().name() == PREVIEW_STYLE.code_background
        assert first.bottomMargin() == 0
        assert last.bottomMargin() == PREVIEW_STYLE.paragraph_gap

    def test_quotes_are_tinted_and_indented(self):
        # The block only borrows the document; drop the document first and the
        # format read is a use-after-free that corrupts the heap.
        document = _render(SAMPLE)
        quote = _block_named(document, "quoted").blockFormat()
        assert quote.hasProperty(QTextFormat.Property.BlockQuoteLevel)
        assert quote.background().color().name() == PREVIEW_STYLE.quote_background
        assert quote.leftMargin() == PREVIEW_STYLE.indent_px

    def test_paragraph_spacing_comes_from_the_style_not_the_importer(self):
        document = _render("first paragraph\n\nsecond paragraph")
        first = _block_named(document, "first paragraph").blockFormat()
        second = _block_named(document, "second paragraph").blockFormat()
        assert first.topMargin() == 0
        assert first.bottomMargin() == PREVIEW_STYLE.paragraph_gap
        assert second.topMargin() == 0
        assert first.lineHeight() == PREVIEW_STYLE.line_height

    def test_table_header_row_is_shaded(self):
        document = _render(SAMPLE)
        header = _block_named(document, "a")
        cell_block = _block_named(document, "1")
        assert header.blockFormat().bottomMargin() == 0
        from PyQt6.QtGui import QTextCursor

        table = QTextCursor(header).currentTable()
        assert table is not None
        assert table.cellAt(0, 0).format().background().color().name() == (
            PREVIEW_STYLE.code_background
        )
        assert table.cellAt(1, 0).format().background().style().value == 0
        assert QTextCursor(cell_block).currentTable() is table

    def test_plain_prose_survives_untouched(self):
        text = "Paid $5 * 3 for snake_case_names and #1 priority."
        assert _render(text).toPlainText() == text

    def test_empty_text_leaves_an_empty_document(self):
        assert _render("").isEmpty()

    def test_default_font_follows_the_style(self):
        document = _render("hello", READER_STYLE)
        assert document.defaultFont().pointSizeF() == READER_STYLE.body_pt
        assert document.indentWidth() == READER_STYLE.indent_px


class TestMarkdownStyle:
    def test_scaled_multiplies_lengths_and_keeps_colours(self):
        larger = READER_STYLE.scaled(1.5)
        assert larger.body_pt == pytest.approx(READER_STYLE.body_pt * 1.5)
        assert larger.paragraph_gap == round(READER_STYLE.paragraph_gap * 1.5)
        assert larger.code_background == READER_STYLE.code_background
        assert larger.heading_pt(1) > READER_STYLE.heading_pt(1)

    def test_heading_levels_past_the_table_reuse_the_last_scale(self):
        style = MarkdownStyle(
            body_pt=10, line_height=100, paragraph_gap=4, indent_px=10,
            code_background="#000000", quote_background="#000000",
            heading_scale=(2.0, 1.5),
        )
        assert style.heading_pt(1) == 20
        assert style.heading_pt(2) == 15
        assert style.heading_pt(6) == 15
