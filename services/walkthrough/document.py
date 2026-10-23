"""A walkthrough written out as Markdown, a PDF and a JSON manifest.

A walkthrough is one continuous recording with screenshots kept along the
way. Each screenshot closes a section: the words said since the previous
screenshot come first, then the screenshot they describe. Speech after the
last screenshot is a trailing section without one.

walkthrough.md is for terminal agents and editors, walkthrough.pdf for chat
apps and people, and walkthrough.json keeps the sections as data for a later
write-up. Everything here may run on a worker thread: the PDF is made with
QImage, QPainter, QTextDocument and QPdfWriter only, never QPixmap or a
widget, and needs only that the QGuiApplication exists, for its fonts.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QLocale, QMarginsF, QPointF, QRectF, QSizeF, Qt, QUrl
from PyQt6.QtGui import (
    QColor,
    QFont,
    QImage,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
    QPen,
    QTextBlock,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextImageFormat,
    QTextLayout,
    QTextLine,
)

logger = logging.getLogger(__name__)

TITLE = "Walkthrough"
MARKDOWN_NAME = "walkthrough.md"
PDF_NAME = "walkthrough.pdf"
MANIFEST_NAME = "walkthrough.json"
MANIFEST_VERSION = 1

_ABOUT = "Spoken notes with screenshots, transcribed by OpenWhisper."
_ORDER = "Each screenshot comes right after the words that describe it."


@dataclass(frozen=True)
class Section:
    """What was said up to one kept screenshot, or after the last one."""

    #: Transcript for this section; may be "".
    text: str
    #: PNG of the screenshot that closes this section; None for trailing speech.
    shot: Path | None = None
    #: Seconds into the recording when that screenshot was kept.
    kept_at_s: float | None = None


@dataclass(frozen=True)
class Walkthrough:
    """One recording's sections, in the order they were spoken."""

    #: Local time.
    started_at: datetime
    #: Recording length in seconds.
    duration_s: float
    sections: tuple[Section, ...]


@dataclass(frozen=True)
class DocumentFormat:
    """What the documents show besides the words and screenshots.

    The defaults are the full layout; Settings → Dictation → Walkthroughs
    changes them. The manifest always keeps everything.
    """

    #: The heading; "" leaves it out.
    title: str = TITLE
    #: The line with the date, the length and the screenshot count.
    details: bool = True
    #: The note saying what the document is and how to read it.
    about: bool = True
    #: "Screenshot 2 · 1:12" under each screenshot.
    captions: bool = True


# --- What every format shares -----------------------------------------------


@dataclass(frozen=True)
class _Part:
    """A section as written out: its tidied words and its screenshot's number."""

    words: str
    shot: Path | None
    number: int  # 1 for the first screenshot; 0 without one
    kept_at_s: float | None


def _parts(walkthrough: Walkthrough) -> list[_Part]:
    """The sections worth writing, each screenshot numbered in order of appearance.

    A section with neither words nor a screenshot says nothing and is dropped.
    """
    parts: list[_Part] = []
    shots = 0
    for section in walkthrough.sections:
        words = _tidy(section.text)
        number = 0
        if section.shot is not None:
            shots += 1
            number = shots
        elif not words:
            continue
        parts.append(_Part(words, section.shot, number, section.kept_at_s))
    return parts


def _shot_count(parts: list[_Part]) -> int:
    return sum(1 for part in parts if part.number)


def _tidy(text: str) -> str:
    """``text`` without surrounding whitespace and with ``\\n`` line breaks.

    Other line endings would reach the Markdown file as they are.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _paragraphs(words: str) -> list[str]:
    """``words`` split at blank lines."""
    paragraphs = (paragraph.strip() for paragraph in re.split(r"\n\s*\n", words))
    return [paragraph for paragraph in paragraphs if paragraph]


def clock(seconds: float) -> str:
    """``1:12``, or ``1:02:03`` from an hour on, in whole seconds."""
    whole = max(0, int(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _when(started_at: datetime) -> str:
    """``Tue, Oct 20, 2026, 2:32 PM``."""
    day = f"{started_at.strftime('%a, %b')} {started_at.day}, {started_at.year}"
    return f"{day}, {started_at.strftime('%I:%M %p').lstrip('0')}"


def _meta(walkthrough: Walkthrough, shots: int) -> str:
    """``Tue, Oct 20, 2026, 2:32 PM · 2:16 of speech · 3 screenshots``."""
    count = {0: "no screenshots", 1: "1 screenshot"}.get(shots, f"{shots} screenshots")
    speech = clock(walkthrough.duration_s)
    return f"{_when(walkthrough.started_at)} · {speech} of speech · {count}"


def _about(shots: int) -> str:
    return f"{_ABOUT} {_ORDER}" if shots else _ABOUT


def _header(walkthrough: Walkthrough, shots: int, document_format: DocumentFormat) -> list[str]:
    """The lines under the title that ``document_format`` keeps, in order."""
    lines = []
    if document_format.details:
        lines.append(_meta(walkthrough, shots))
    if document_format.about:
        lines.append(_about(shots))
    return lines


def _label(part: _Part, before_time: str, after_time: str = "") -> str:
    """``Screenshot 2 (1:12)``, ``Screenshot 2 · 1:12``, or without a known
    time just ``Screenshot 2``."""
    label = f"Screenshot {part.number}"
    if part.kept_at_s is None:
        return label
    return f"{label}{before_time}{clock(part.kept_at_s)}{after_time}"


def _relative(path: Path, folder: Path) -> str | None:
    """``path`` relative to ``folder``, with forward slashes; None outside it."""
    absolute = Path(os.path.abspath(path))
    try:
        return absolute.relative_to(os.path.abspath(folder)).as_posix()
    except ValueError:
        return None


# --- Markdown ---------------------------------------------------------------

#: A line that would open a heading, quote, list or rule in Markdown.
_BLOCK_MARKER = re.compile(r"^([ \t]*)([#>+*-]|\d{1,9}[.)](?=\s|$))", re.MULTILINE)


def markdown(
    walkthrough: Walkthrough,
    *,
    folder: Path,
    absolute_images: bool = False,
    document_format: DocumentFormat = DocumentFormat(),
) -> str:
    """The walkthrough as Markdown, each section's words above its screenshot.

    Image links are relative to ``folder``, where walkthrough.md sits beside
    its screenshots. ``absolute_images`` links full paths instead, for
    Markdown read elsewhere, such as by a terminal agent from the clipboard.
    Without captions a screenshot still keeps its label as alt text.
    """
    parts = _parts(walkthrough)
    shots = _shot_count(parts)
    lines = []
    if document_format.title:
        lines += [f"# {document_format.title}", ""]
    header = _header(walkthrough, shots, document_format)
    if header:
        # The details line has no full stop of its own; the note does.
        lines += [f"_{'. '.join(header)}_", ""]
    for part in parts:
        if part.words:
            lines += [_escape_markers(part.words), ""]
        if part.shot is not None:
            target = _image_target(part.shot, folder, absolute_images)
            lines += [f"![{_label(part, ' (', ')')}]({target})", ""]
    return "\n".join(lines)


def _escape_markers(words: str) -> str:
    """Backslash a marker that opens a line, so speech never turns into structure.

    Words that start with "#", ">", "-", "+", "*" or "1." would otherwise
    become a heading, a quote or a list; the backslash is not rendered.
    """
    return _BLOCK_MARKER.sub(
        lambda match: f"{match[1]}{match[2][:-1]}\\{match[2][-1]}", words
    )


def _image_target(path: Path, folder: Path, absolute: bool) -> str:
    """A link destination for ``path``: relative to ``folder``, or the full path.

    Angle brackets keep a destination with spaces in one piece, so a full
    path always gets them, with the platform's own separators.
    """
    relative = None if absolute else _relative(path, folder)
    if relative is None:
        return f"<{os.path.abspath(path)}>"
    if re.search(r"[\s()]", relative):
        return f"<{relative}>"
    return relative


def write_markdown(
    walkthrough: Walkthrough,
    folder: Path,
    *,
    document_format: DocumentFormat = DocumentFormat(),
) -> Path:
    """Write ``folder/walkthrough.md``, its images linked relative to it."""
    path = folder / MARKDOWN_NAME
    text = markdown(walkthrough, folder=folder, document_format=document_format)
    # newline: Windows would otherwise write CRLF.
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


# --- Manifest ---------------------------------------------------------------


def write_manifest(walkthrough: Walkthrough, folder: Path) -> Path:
    """Write ``folder/walkthrough.json``, the sections as data for a later write-up.

    It holds the same sections as the documents: screenshots by file name
    within ``folder`` (null for trailing speech), times in seconds into the
    recording, and the start with its UTC offset.
    """
    manifest = {
        "version": MANIFEST_VERSION,
        "started_at": walkthrough.started_at.astimezone().isoformat(timespec="seconds"),
        "duration_s": walkthrough.duration_s,
        "sections": [
            {
                "text": part.words,
                "shot": _shot_name(part.shot, folder),
                "kept_at_s": part.kept_at_s,
            }
            for part in _parts(walkthrough)
        ],
    }
    path = folder / MANIFEST_NAME
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _shot_name(shot: Path | None, folder: Path) -> str | None:
    if shot is None:
        return None
    return _relative(shot, folder) or os.path.abspath(shot)


# --- PDF --------------------------------------------------------------------

#: About 0.7 inch around the text, a little more at the sides.
_MARGINS_IN = QMarginsF(0.75, 0.7, 0.75, 0.7)
#: The tallest a screenshot is shown, as a share of the text area's height.
_MAX_IMAGE_HEIGHT = 0.62
#: The most a screenshot shrinks to keep its section on one page.
_MIN_IMAGE_SCALE = 0.5
_FONT_FAMILIES = ["Segoe UI", "Noto Sans", "DejaVu Sans"]
#: A hairline around each screenshot, so a light one keeps its edge on white.
_FRAME_COLOR = "#dcd5cb"
_FRAME_WIDTH_PT = 0.6
#: Layout units of give at page edges, where layout positions are rounded.
_SLACK = 0.5


@dataclass(frozen=True)
class _Style:
    """How one kind of paragraph looks; lengths in points."""

    size: float
    color: str
    bold: bool = False
    space_before: float = 0.0
    line_height: int = 100  # percent of single spacing
    centered: bool = False


_INK = "#1d1b19"
_MUTED = "#6b645c"
_TITLE_STYLE = _Style(22, _INK, bold=True)
_META_STYLE = _Style(9.5, _MUTED, space_before=3)
_ABOUT_STYLE = replace(_META_STYLE, space_before=1)
#: A section's first paragraph; the space above it sets sections apart.
_WORDS_STYLE = _Style(11, _INK, space_before=16, line_height=145)
_MORE_WORDS_STYLE = replace(_WORDS_STYLE, space_before=6)
_IMAGE_STYLE = _Style(11, _INK, space_before=6, centered=True)
#: A screenshot with no words above it starts its section.
_LONE_IMAGE_STYLE = replace(_IMAGE_STYLE, space_before=_WORDS_STYLE.space_before)
_CAPTION_STYLE = _Style(8.5, "#8a8278", space_before=3, centered=True)


def write_pdf(
    walkthrough: Walkthrough,
    folder: Path,
    *,
    page_size: QPageSize | None = None,
    document_format: DocumentFormat = DocumentFormat(),
) -> Path:
    """Write ``folder/walkthrough.pdf``: the sections laid out on pages.

    Letter where the system locale measures in US units, else A4, unless
    ``page_size`` is given. A screenshot section's words, image and caption
    stay on one page. Safe to call from a worker thread.

    Raises:
        OSError: The file could not be written.
    """
    path = folder / PDF_NAME
    writer = QPdfWriter(str(path))
    writer.setPageSize(page_size or _local_page_size())
    writer.setPageMargins(_MARGINS_IN, QPageLayout.Unit.Inch)
    # The file's own title, which viewers show in their title bar, keeps a
    # name even when the page has no heading.
    writer.setTitle(f"{document_format.title or TITLE} · {_when(walkthrough.started_at)}")
    writer.setCreator("OpenWhisper")
    dpi = _layout_dpi()
    composer = _Composer(_text_area(writer, dpi), dpi, document_format)
    doc, frames = composer.compose(walkthrough)
    _paint_pages(doc, frames, writer, dpi, path)
    return path


def write_markdown_pdf(
    text: str,
    path: Path,
    *,
    folder: Path,
    title: str,
    page_size: QPageSize | None = None,
) -> Path:
    """Write ``text``, any Markdown, as a PDF in the walkthrough's look.

    For a document someone else laid out, such as an agent's write-up. Its
    images are file names in ``folder``; one that is missing is left out.
    Raw HTML is shown as text, never run. Safe to call from a worker thread.

    Raises:
        OSError: The file could not be written.
    """
    writer = QPdfWriter(str(path))
    writer.setPageSize(page_size or _local_page_size())
    writer.setPageMargins(_MARGINS_IN, QPageLayout.Unit.Inch)
    writer.setTitle(title)
    writer.setCreator("OpenWhisper")
    dpi = _layout_dpi()
    doc, frames = _MarkdownComposer(_text_area(writer, dpi), dpi, folder).compose(text)
    _paint_pages(doc, frames, writer, dpi, path)
    return path


def _local_page_size() -> QPageSize:
    """Letter where the locale measures in US units, A4 everywhere else."""
    us_units = QLocale.MeasurementSystem.ImperialUSSystem
    if QLocale.system().measurementSystem() == us_units:
        return QPageSize(QPageSize.PageSizeId.Letter)
    return QPageSize(QPageSize.PageSizeId.A4)


def _layout_dpi() -> int:
    """Layout units per inch of a QTextDocument without a paint device.

    It sizes point fonts at the screen's logical DPI: 96 on Windows and
    Linux, 72 on macOS. A new QImage carries that same DPI and, unlike the
    QScreen, belongs to no thread.
    """
    return QImage(1, 1, QImage.Format.Format_RGB32).logicalDpiY()


def _text_area(writer: QPdfWriter, dpi: int) -> QSizeF:
    """The page inside its margins, in whole layout units.

    Whole units put page edges exactly where the layout's fixed-point
    arithmetic does, so a line moved to the next page measures as on it.
    """
    paint = writer.pageLayout().paintRect(QPageLayout.Unit.Inch)
    return QSizeF(math.floor(paint.width() * dpi), math.floor(paint.height() * dpi))


def _paint_pages(
    doc: QTextDocument, frames: list[QRectF], writer: QPdfWriter, dpi: int, path: Path
) -> None:
    """Paint each page of ``doc``, and the frames around its screenshots.

    QTextDocument.print would encode the screenshots as JPEG. Lossless they
    stay sharp and, being flat UI, come out smaller: 145 KB instead of
    683 KB for three sample screenshots.
    """
    painter = QPainter()
    if not painter.begin(writer):
        raise OSError(f"Could not write {path}")
    try:
        painter.setRenderHint(QPainter.RenderHint.LosslessImageRendering)
        scale = writer.resolution() / dpi
        painter.scale(scale, scale)
        pen = QPen(QColor(_FRAME_COLOR), _FRAME_WIDTH_PT * dpi / 72)
        for index in range(doc.pageCount()):
            if index:
                writer.newPage()
            _paint_page(painter, doc, index, frames, pen)
    finally:
        painter.end()


def _paint_page(
    painter: QPainter, doc: QTextDocument, index: int, frames: list[QRectF], pen: QPen
) -> None:
    """Paint the lines, screenshots included, and frames on page ``index``.

    Line by line rather than with drawContents: QTextLayout also paints the
    line before a clip rectangle, which the clip hides but the PDF would
    keep as text, so text copied or searched across a page break would come
    up twice.
    """
    height = doc.pageSize().height()
    painter.save()
    painter.translate(0, -index * height)
    for line, origin in _lines(doc):
        if _page_index(origin.y() + line.y(), height) == index:
            line.draw(painter, origin)
    painter.setPen(pen)
    for frame in frames:
        if _page_index(frame.top(), height) == index:
            painter.drawRect(frame)
    painter.restore()


def _lines(doc: QTextDocument) -> Iterator[tuple[QTextLine, QPointF]]:
    """Every line of ``doc``, with where its block is."""
    block = doc.begin()
    while block.isValid():
        layout = block.layout()
        for number in range(layout.lineCount()):
            yield layout.lineAt(number), layout.position()
        block = block.next()


def _page_index(top: float, height: float) -> int:
    """The page something starting at ``top`` is on; pagination puts every
    line, and so every screenshot, wholly on one."""
    return int((top + _SLACK) // height)


def _shown_size(image: QImage, page: QSizeF, unit: float) -> QSizeF:
    """A pixel to 1/96 inch, scaled down to the text width and height limit.

    ``unit`` is layout units per point.
    """
    natural = QSizeF(image.width(), image.height()) * (unit * 72 / 96)
    scale = min(
        1.0,
        page.width() / natural.width(),
        page.height() * _MAX_IMAGE_HEIGHT / natural.height(),
    )
    return natural * scale


def _document_font() -> QFont:
    """Segoe UI on Windows, Noto Sans or DejaVu Sans on Linux, else the
    system's sans-serif."""
    font = QFont(_FONT_FAMILIES)
    font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setPointSizeF(_WORDS_STYLE.size)
    return font


@dataclass(frozen=True)
class _Group:
    """One screenshot section's blocks, which belong on one page."""

    first: int  # its first block; the title's for the section under the header
    image: int | None  # the screenshot's block; None when it could not be read
    last: int  # the caption's block, or the screenshot's without captions


class _Composer:
    """Lays a walkthrough out as a paginated QTextDocument.

    The document measures in layout units, ``dpi`` to the inch; styles give
    lengths in points.
    """

    def __init__(
        self, page: QSizeF, dpi: int, document_format: DocumentFormat = DocumentFormat()
    ) -> None:
        self.page = page
        self.unit = dpi / 72  # layout units per point
        self.format = document_format
        self.doc = QTextDocument()
        self.doc.setDocumentMargin(0)
        # Laid out at screen resolution but painted at the PDF's: hinted
        # advances would space the letters unevenly once scaled.
        self.doc.setUseDesignMetrics(True)
        self.doc.setDefaultFont(_document_font())
        self.doc.setPageSize(page)
        self.cursor = QTextCursor(self.doc)
        self.groups: list[_Group] = []
        # The header's last block; None without a header.
        self.header_end: int | None = None
        # A new document holds one empty block, which the first block fills.
        self.empty = True

    def compose(self, walkthrough: Walkthrough) -> tuple[QTextDocument, list[QRectF]]:
        """Lay out the walkthrough; returns the document and where its
        screenshots ended up."""
        parts = _parts(walkthrough)
        shots = _shot_count(parts)
        if self.format.title:
            self.header_end = self._paragraph(self.format.title, _TITLE_STYLE)
        for index, line in enumerate(_header(walkthrough, shots, self.format)):
            style = _ABOUT_STYLE if index else _META_STYLE
            self.header_end = self._paragraph(line, style)
        for part in parts:
            self._section(part)
        pages = _Pages(self.doc)
        pages.paginate(self.groups)
        return self.doc, pages.frames(self.groups)

    def _section(self, part: _Part) -> None:
        # The section right under the header keeps the title company, so
        # the title never sits alone on the first page.
        under_header = self.cursor.blockNumber() == self.header_end
        first = 0 if under_header or self.empty else self.cursor.blockNumber() + 1
        for index, paragraph in enumerate(_paragraphs(part.words)):
            style = _MORE_WORDS_STYLE if index else _WORDS_STYLE
            # A single line break stays inside its paragraph.
            self._paragraph(paragraph.replace("\n", "\N{LINE SEPARATOR}"), style)
        if part.shot is None:
            return
        style = _IMAGE_STYLE if part.words else _LONE_IMAGE_STYLE
        image = self._image(part.shot, part.number, style)
        last = image
        if self.format.captions:
            last = self._paragraph(_label(part, " · "), _CAPTION_STYLE)
        if last is not None:
            self.groups.append(_Group(first, image, last))

    def _paragraph(self, text: str, style: _Style) -> int:
        """Add a paragraph; returns its block number."""
        char_format = self._char_format(style)
        self._new_block(style, char_format)
        self.cursor.insertText(text, char_format)
        return self.cursor.blockNumber()

    def _new_block(self, style: _Style, char_format: QTextCharFormat) -> None:
        """Start a block in ``style``: the document's first, or a new one."""
        if self.empty:
            self.empty = False
            self.cursor.setBlockFormat(self._block_format(style))
            self.cursor.setBlockCharFormat(char_format)
            return
        self.cursor.insertBlock(self._block_format(style), char_format)

    def _image(self, path: Path, number: int, style: _Style) -> int | None:
        """Add a screenshot as its own paragraph; returns its block number.

        None when the file cannot be read: its words and caption still go in.
        """
        image = QImage(str(path))
        if image.isNull():
            logger.warning("Could not read screenshot %s for the walkthrough PDF", path)
            return None
        name = f"shot-{number}"
        resource = QTextDocument.ResourceType.ImageResource
        self.doc.addResource(resource, QUrl(name), image)
        size = _shown_size(image, self.page, self.unit)
        image_format = QTextImageFormat()
        image_format.setName(name)
        image_format.setWidth(size.width())
        image_format.setHeight(size.height())
        self._new_block(style, self._char_format(style))
        self.cursor.insertImage(image_format)
        return self.cursor.blockNumber()

    def _char_format(self, style: _Style) -> QTextCharFormat:
        char_format = QTextCharFormat()
        char_format.setFontPointSize(style.size)
        weight = QFont.Weight.Bold if style.bold else QFont.Weight.Normal
        char_format.setFontWeight(weight.value)
        char_format.setForeground(QColor(style.color))
        return char_format

    def _block_format(self, style: _Style) -> QTextBlockFormat:
        block_format = QTextBlockFormat()
        block_format.setTopMargin(style.space_before * self.unit)
        block_format.setLineHeight(
            style.line_height, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
        )
        if style.centered:
            block_format.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        return block_format


class _Pages:
    """Fits a paginated document's sections to its pages.

    QTextDocument breaks pages between any two lines, so a section's words
    could end one page and its screenshot start the next, and it keeps a
    block's top margin across a page break, so a page could start lower
    than the others.
    """

    def __init__(self, doc: QTextDocument) -> None:
        self.doc = doc
        self.height = doc.pageSize().height()
        # QTextDocumentLayout lays out lazily, and a format changed while it
        # is unfinished leaves the blocks after it without lines (see
        # _layout). Laid out in full first, it lays out again all that
        # follows each change.
        doc.documentLayout().documentSize()

    def paginate(self, groups: list[_Group]) -> None:
        """Start every page flush with its top and keep each screenshot
        section on one page.

        Block by block, in order: a change only moves what follows it.
        """
        starts = {group.first: group for group in groups}
        for block_number in range(self.doc.blockCount()):
            self._flush_top(block_number)
            if block_number in starts:
                self._keep_together(starts[block_number])

    def frames(self, groups: list[_Group]) -> list[QRectF]:
        """Where each screenshot ended up, in document coordinates."""
        frames = []
        for group in groups:
            if group.image is not None:
                layout = self._layout(group.image)
                frames.append(layout.lineAt(0).naturalTextRect().translated(layout.position()))
        return frames

    def _flush_top(self, block_number: int) -> None:
        """Keep only the part of a block's top margin before a page break."""
        margin = self._margin(block_number)
        top = self._extent(block_number)[0]
        page_top = self._page_of(top + _SLACK) * self.height
        if margin and top - margin < page_top + _SLACK < top:
            kept = max(0.0, page_top - (top - margin))
            self._set_block_format(block_number, top_margin=kept)

    def _keep_together(self, group: _Group) -> None:
        """Put ``group`` on one page, the first way that works.

        It stays where it is if it fits. Else it starts on a new page,
        with a smaller screenshot if that is what fits there. Else its
        screenshot shrinks to fit below the words on their last page. The
        section under the header starts at the title and never moves.
        """
        if self._on_one_page(group):
            return
        if not self._at_page_top(group.first) and self._fits_on_new_page(group):
            return
        self._shrink_to_fit(group)

    def _fits_on_new_page(self, group: _Group) -> bool:
        """Start ``group`` on a new page if it fits there; undone if not."""
        block_format = self._block(group.first).blockFormat()
        image_format = self._image_format(group)
        # Flush with the top of the page, like the title.
        self._set_block_format(group.first, top_margin=0.0, new_page=True)
        self._shrink_to_fit(group)
        if self._on_one_page(group):
            return True
        QTextCursor(self._block(group.first)).setBlockFormat(block_format)
        if image_format is not None:
            self._image_cursor(group.image).setCharFormat(image_format)
        return False

    def _shrink_to_fit(self, group: _Group) -> None:
        """Shrink the screenshot to fit below the words on their last page.

        Never below half its size: a screenshot too small to read is worse
        than one on the next page. One that starts its section has no words
        to stay with, and fits on any page as it is.

        The caption, when there is one, goes below it on the same page.
        """
        image_format = self._image_format(group)
        if image_format is None or group.image == group.first:
            return
        above = self._end(group.image - 1) + self._margin(group.image)
        page_end = (self._page_of(above - _SLACK) + 1) * self.height
        below = 0.0
        if group.last != group.image:  # the caption
            caption_top, caption_bottom = self._extent(group.last)
            below = self._margin(group.last) + caption_bottom - caption_top
        # A unit to spare: the layout rounds an image's size to whole units.
        scale = (page_end - above - below - 1) / image_format.height()
        if not _MIN_IMAGE_SCALE <= scale < 1:
            return
        image_format.setWidth(image_format.width() * scale)
        image_format.setHeight(image_format.height() * scale)
        self._image_cursor(group.image).setCharFormat(image_format)

    def _on_one_page(self, group: _Group) -> bool:
        top = self._extent(group.first)[0]
        bottom = self._extent(group.last)[1]
        return self._page_of(top + _SLACK) == self._page_of(bottom - _SLACK)

    def _at_page_top(self, block_number: int) -> bool:
        return self._extent(block_number)[0] % self.height < _SLACK

    def _page_of(self, y: float) -> int:
        return int(y // self.height)

    def _extent(self, block_number: int) -> tuple[float, float]:
        """Where a block's first line starts and its last line ends.

        Not blockBoundingRect: when pagination pushes a block's lines to the
        next page, that rectangle stays where the block began.
        """
        layout = self._layout(block_number)
        origin = layout.position().y()
        first, last = layout.lineAt(0), layout.lineAt(layout.lineCount() - 1)
        return origin + first.y(), origin + last.y() + last.height()

    def _end(self, block_number: int) -> float:
        """Where the next block's top margin starts: below the last line,
        with the spacing a proportional line height adds there too."""
        layout = self._layout(block_number)
        last = layout.lineAt(layout.lineCount() - 1)
        spacing = self._block(block_number).blockFormat().lineHeight() / 100
        return layout.position().y() + last.y() + last.height() * spacing

    def _layout(self, block_number: int) -> QTextLayout:
        layout = self._block(block_number).layout()
        if layout.lineCount() == 0:
            # Asking an empty layout for a line crashes the process; see
            # __init__ for why it never should be empty.
            raise RuntimeError(f"Walkthrough PDF block {block_number} was not laid out")
        return layout

    def _margin(self, block_number: int) -> float:
        return self._block(block_number).blockFormat().topMargin()

    def _set_block_format(
        self, block_number: int, *, top_margin: float, new_page: bool = False
    ) -> None:
        block = self._block(block_number)
        block_format = block.blockFormat()
        block_format.setTopMargin(top_margin)
        if new_page:
            block_format.setPageBreakPolicy(QTextFormat.PageBreakFlag.PageBreak_AlwaysBefore)
        QTextCursor(block).setBlockFormat(block_format)

    def _block(self, block_number: int) -> QTextBlock:
        return self.doc.findBlockByNumber(block_number)

    def _image_format(self, group: _Group) -> QTextImageFormat | None:
        if group.image is None:
            return None
        return self._image_cursor(group.image).charFormat().toImageFormat()

    def _image_cursor(self, block_number: int) -> QTextCursor:
        """A cursor selecting the screenshot, the only thing in its block."""
        cursor = QTextCursor(self._block(block_number))
        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor
        )
        return cursor


#: Space above a heading, by level, in points; the first block has none.
_HEADING_SPACE = {1: 0.0, 2: 18.0, 3: 14.0}
#: Space between a heading and what it heads, in points.
_HEADING_GAP = 5.0
_BODY_LINE_HEIGHT = 140


def _words(block: QTextBlock) -> str:
    """The block's text without its images."""
    return block.text().replace("\N{OBJECT REPLACEMENT CHARACTER}", "").strip()


class _MarkdownComposer:
    """Lays free-form Markdown out as a paginated QTextDocument.

    Qt's own Markdown reader does the structure; this gives it the
    walkthrough's font, line spacing and screenshot sizing and framing.
    """

    def __init__(self, page: QSizeF, dpi: int, folder: Path) -> None:
        self.page = page
        self.unit = dpi / 72  # layout units per point
        self.folder = folder
        self.doc = QTextDocument()
        self.doc.setDocumentMargin(0)
        self.doc.setUseDesignMetrics(True)
        self.doc.setDefaultFont(_document_font())
        self.doc.setPageSize(page)

    def compose(self, text: str) -> tuple[QTextDocument, list[QRectF]]:
        """Lay out ``text``; returns the document and where its screenshots ended up."""
        features = QTextDocument.MarkdownFeature
        self.doc.setMarkdown(
            text, features(features.MarkdownDialectGitHub.value | features.MarkdownNoHTML.value)
        )
        groups = []
        block = self.doc.begin()
        while block.isValid():
            if self._style_block(block):
                number = block.blockNumber()
                groups.append(_Group(self._lead_in(number), number, number))
            block = block.next()
        pages = _Pages(self.doc)
        pages.paginate(groups)
        return self.doc, pages.frames(groups)

    def _style_block(self, block: QTextBlock) -> bool:
        """Space and size one block; True when it is a screenshot on its own."""
        block_format = block.blockFormat()
        level = block_format.headingLevel()
        images = self._size_images(block)
        alone = images == 1 and not _words(block)
        if level:
            space = 0.0 if block.blockNumber() == 0 else _HEADING_SPACE.get(level, 12.0)
            block_format.setTopMargin(space * self.unit)
            block_format.setBottomMargin(_HEADING_GAP * self.unit)
        elif alone:
            # Not the body's line height: on a line as tall as a screenshot
            # it would open a gap under it.
            block_format.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            block_format.setTopMargin(_IMAGE_STYLE.space_before * self.unit * 2)
        else:
            block_format.setLineHeight(
                _BODY_LINE_HEIGHT, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
            )
        QTextCursor(block).setBlockFormat(block_format)
        return alone

    def _lead_in(self, image: int) -> int:
        """Where the words leading into the screenshot in block ``image`` start.

        The paragraph right before it, and the heading over that paragraph,
        stay on the screenshot's page; the screenshot itself when neither
        is there. With only headings above them, they keep the title
        company, so it never sits alone on the first page.
        """
        first = image
        for number in (image - 1, image - 2):
            block = self.doc.findBlockByNumber(number)
            if not block.isValid() or not _words(block):
                break
            first = number
            if block.blockFormat().headingLevel():
                break
        above = (self.doc.findBlockByNumber(number) for number in range(first))
        if all(block.blockFormat().headingLevel() for block in above):
            return 0
        return first

    def _size_images(self, block: QTextBlock) -> int:
        """Load and size the block's images; returns how many it shows."""
        shown = 0
        resource = QTextDocument.ResourceType.ImageResource
        iterator = block.begin()
        while not iterator.atEnd():
            fragment = iterator.fragment()
            iterator += 1
            char_format = fragment.charFormat()
            if not char_format.isImageFormat():
                continue
            image_format = char_format.toImageFormat()
            name = image_format.name()
            image = QImage(str(self.folder / name))
            cursor = QTextCursor(self.doc)
            cursor.setPosition(fragment.position())
            cursor.setPosition(fragment.position() + fragment.length(), QTextCursor.MoveMode.KeepAnchor)
            if image.isNull():
                logger.warning("Could not read %s for the walkthrough write-up", name)
                cursor.removeSelectedText()
                continue
            self.doc.addResource(resource, QUrl(name), image)
            size = _shown_size(image, self.page, self.unit)
            image_format.setWidth(size.width())
            image_format.setHeight(size.height())
            cursor.setCharFormat(image_format)
            shown += 1
        return shown
