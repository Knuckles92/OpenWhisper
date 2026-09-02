"""Render Markdown into a ``QTextDocument`` with the app's dark-theme typography.

Qt parses Markdown natively, but the importer builds the document directly and
never consults a stylesheet: headings come out at Qt's default scale, and code,
quotes, links, and tables carry no colour of their own. The pass here walks the
imported document once and writes the sizes, margins, and colours the theme
wants onto the block, character, and table formats, so one Markdown transcript
reads the same in a compact preview and in a full reading window.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final, List, Tuple

from PyQt6.QtGui import (
    QColor,
    QFont,
    QTextBlock,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextFrameFormat,
    QTextTable,
)

_HEADING: Final[str] = "#ffffff"
_QUOTE_TEXT: Final[str] = "#c7c7cc"
_CODE_TEXT: Final[str] = "#e5e5ea"
_LINK: Final[str] = "#4da3ff"
_BORDER: Final[str] = "#3a3a3c"
_MONO_FAMILIES: Final[List[str]] = [
    "Cascadia Mono", "Consolas", "SF Mono", "Menlo", "monospace",
]

_PROPORTIONAL_LINE_HEIGHT: Final[int] = (
    QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
)


@dataclass(frozen=True)
class MarkdownStyle:
    """Typography for one rendering context.

    Colours that depend on the surface behind the text travel with the style:
    an inline-code chip must be darker than the transcript pane but lighter
    than the reading window, which are different greys.
    """

    body_pt: float
    line_height: int
    paragraph_gap: int
    indent_px: int
    code_background: str
    quote_background: str
    heading_scale: Tuple[float, ...] = (1.55, 1.3, 1.15, 1.05, 1.0, 1.0)
    family: str = "Segoe UI"

    def heading_pt(self, level: int) -> float:
        index = max(1, min(level, len(self.heading_scale))) - 1
        return round(self.body_pt * self.heading_scale[index], 1)

    @property
    def code_pt(self) -> float:
        return round(self.body_pt * 0.9, 1)

    def scaled(self, factor: float) -> "MarkdownStyle":
        """The same style with every length multiplied by ``factor``."""
        return replace(
            self,
            body_pt=round(self.body_pt * factor, 1),
            paragraph_gap=max(2, round(self.paragraph_gap * factor)),
            indent_px=max(12, round(self.indent_px * factor)),
        )


#: The transcript pane inside a tab (surface #2c2c2e).
PREVIEW_STYLE: Final[MarkdownStyle] = MarkdownStyle(
    body_pt=13,
    line_height=135,
    paragraph_gap=8,
    indent_px=22,
    code_background="#1c1c1e",
    quote_background="#252527",
)

#: The transcript reading window (surface #1c1c1e).
READER_STYLE: Final[MarkdownStyle] = MarkdownStyle(
    body_pt=15,
    line_height=155,
    paragraph_gap=12,
    indent_px=26,
    code_background="#2c2c2e",
    quote_background="#232326",
)


def render_markdown(document: QTextDocument, text: str, style: MarkdownStyle) -> None:
    """Replace ``document``'s content with ``text`` rendered as Markdown."""
    font = QFont(style.family)
    font.setPointSizeF(style.body_pt)
    document.setDefaultFont(font)
    document.setIndentWidth(style.indent_px)
    document.setMarkdown(
        text or "", QTextDocument.MarkdownFeature.MarkdownDialectGitHub
    )
    _style_document(document, style)


def _style_document(document: QTextDocument, style: MarkdownStyle) -> None:
    cursor = QTextCursor(document)
    cursor.beginEditBlock()
    tables: List[QTextTable] = []
    block = document.begin()
    while block.isValid():
        table = QTextCursor(block).currentTable()
        if table is not None:
            if all(table is not seen for seen in tables):
                tables.append(table)
        _style_block(cursor, block, style, in_table=table is not None)
        block = block.next()
    for table in tables:
        _style_table(table, style)
    cursor.endEditBlock()


def _is_code_block(block_format: QTextBlockFormat) -> bool:
    return block_format.hasProperty(
        QTextFormat.Property.BlockCodeFence
    ) or block_format.nonBreakableLines()


def _style_block(
    cursor: QTextCursor, block: QTextBlock, style: MarkdownStyle, *, in_table: bool
) -> None:
    block_format = block.blockFormat()
    level = block_format.headingLevel()
    is_code = _is_code_block(block_format)
    is_quote = block_format.hasProperty(QTextFormat.Property.BlockQuoteLevel)
    gap = style.paragraph_gap

    merged = QTextBlockFormat()
    merged.setLineHeight(style.line_height, _PROPORTIONAL_LINE_HEIGHT)
    if in_table:
        merged.setTopMargin(0)
        merged.setBottomMargin(0)
    elif level:
        merged.setTopMargin(gap * (1.5 if level <= 2 else 1.0))
        merged.setBottomMargin(gap * 0.6)
    elif is_code:
        # Each fenced line is its own block; only the run's last line gets the
        # gap, so the lines read as one slab.
        merged.setLineHeight(125, _PROPORTIONAL_LINE_HEIGHT)
        merged.setBackground(QColor(style.code_background))
        merged.setTopMargin(0)
        next_block = block.next()
        ends_run = not (next_block.isValid() and _is_code_block(next_block.blockFormat()))
        merged.setBottomMargin(gap if ends_run else 0)
    elif is_quote:
        merged.setBackground(QColor(style.quote_background))
        merged.setLeftMargin(style.indent_px)
        merged.setTopMargin(0)
        merged.setBottomMargin(gap)
    elif block.textList() is not None:
        merged.setTopMargin(0)
        merged.setBottomMargin(gap * 0.5)
    else:
        merged.setTopMargin(0)
        merged.setBottomMargin(gap)
    if block.position() == 0:
        merged.setTopMargin(0)

    cursor.setPosition(block.position())
    cursor.mergeBlockFormat(merged)

    if block.length() <= 1:
        return

    if level:
        char_format = QTextCharFormat()
        char_format.setFontPointSize(style.heading_pt(level))
        char_format.setFontWeight(QFont.Weight.DemiBold)
        char_format.setForeground(QColor(_HEADING))
        _merge_over_block(cursor, block, char_format)
        return

    if is_code:
        _merge_over_block(cursor, block, _code_format(style, background=None))
        return

    if is_quote:
        char_format = QTextCharFormat()
        char_format.setForeground(QColor(_QUOTE_TEXT))
        _merge_over_block(cursor, block, char_format)

    fragment = block.begin()
    while not fragment.atEnd():
        piece = fragment.fragment()
        piece_format = piece.charFormat()
        if piece_format.fontFixedPitch():
            _merge_over_range(
                cursor, piece.position(), piece.length(),
                _code_format(style, background=style.code_background),
            )
        elif piece_format.isAnchor():
            link_format = QTextCharFormat()
            link_format.setForeground(QColor(_LINK))
            link_format.setFontUnderline(False)
            _merge_over_range(cursor, piece.position(), piece.length(), link_format)
        fragment += 1


def _code_format(style: MarkdownStyle, *, background: str | None) -> QTextCharFormat:
    char_format = QTextCharFormat()
    char_format.setFontFamilies(_MONO_FAMILIES)
    char_format.setFontFixedPitch(True)
    char_format.setFontPointSize(style.code_pt)
    char_format.setForeground(QColor(_CODE_TEXT))
    if background:
        char_format.setBackground(QColor(background))
    return char_format


def _merge_over_block(
    cursor: QTextCursor, block: QTextBlock, char_format: QTextCharFormat
) -> None:
    # Not BlockUnderCursor: that selection reaches back over the separator
    # into the previous block and restyles its last character too.
    _merge_over_range(cursor, block.position(), block.length() - 1, char_format)


def _merge_over_range(
    cursor: QTextCursor, position: int, length: int, char_format: QTextCharFormat
) -> None:
    cursor.setPosition(position)
    cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
    cursor.mergeCharFormat(char_format)


def _style_table(table: QTextTable, style: MarkdownStyle) -> None:
    table_format = table.format()
    table_format.setBorder(1)
    table_format.setBorderBrush(QColor(_BORDER))
    table_format.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
    table_format.setBorderCollapse(True)
    table_format.setCellPadding(max(4, style.paragraph_gap * 0.75))
    table_format.setCellSpacing(0)
    table_format.setTopMargin(style.paragraph_gap * 0.5)
    table_format.setBottomMargin(style.paragraph_gap)
    table.setFormat(table_format)

    header_background = QColor(style.code_background)
    for column in range(table.columns()):
        cell = table.cellAt(0, column)
        if not cell.isValid():
            continue
        cell_format = cell.format()
        cell_format.setBackground(header_background)
        cell.setFormat(cell_format)
