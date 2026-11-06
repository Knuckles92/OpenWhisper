"""Qt-free PDF and Word exports for walkthroughs and Markdown write-ups.

Markdown is parsed without HTML support. Only images resolving inside the
capture folder are embedded; link targets are never opened by either writer.
Completed documents replace their destination atomically so a failed renderer
cannot destroy a previous export.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import io
import locale
import os
from pathlib import Path
import re
import tempfile
import threading
import unicodedata
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape, quoteattr

from services.walkthrough.document import (
    DocumentFormat, Look, TITLE, Walkthrough, _header, _label, _paragraphs,
    _parts, _shot_count, _when,
)
from services.walkthrough.storage import DOCX_NAME, PDF_NAME

_INK = "#1d1b19"
_MUTED = "#6b645c"
_FRAME = "#dcd5cb"
_CODE = "#f5f1eb"
_FONT_LOCK = threading.Lock()
_FONT_NAMES = {}
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_FALLBACK_FONTS = (
    ("OWNotoSC", "NotoSansSC-Regular.ttf"),
    ("OWNotoArabic", "NotoSansArabic-Regular.ttf"),
    ("OWNotoDevanagari", "NotoSansDevanagari-Regular.ttf"),
)


@dataclass
class _Run:
    text: str = ""
    bold: bool = False
    italic: bool = False
    strike: bool = False
    code: bool = False
    href: str = ""
    image: Path | None = None


@dataclass
class _Block:
    kind: str = "paragraph"
    runs: list[_Run] = field(default_factory=list)
    level: int = 0
    marker: str = ""
    indent: int = 0
    quote: int = 0
    rows: list[list[list[_Run]]] = field(default_factory=list)


def _clean(text):
    return _INVALID_XML.sub("\N{REPLACEMENT CHARACTER}", str(text))


def _page_size(value):
    from reportlab.lib.pagesizes import A4, LETTER

    choice = str(value or "system").lower()
    if choice in ("system", "auto"):
        language = locale.getlocale()[0] or ""
        choice = "letter" if language.upper().endswith(("_US", "_UNITED STATES")) else "a4"
    if choice not in ("a4", "letter"):
        raise ValueError("page_size must be a4, letter or system")
    return LETTER if choice == "letter" else A4


def _color(value, fallback):
    from PIL import ImageColor

    try:
        rgb = ImageColor.getrgb(str(value).strip())
        return "#" + "".join(f"{channel:02x}" for channel in rgb[:3])
    except (ValueError, TypeError):
        return fallback


def _fonts(look=Look()):
    from config import bundle_root
    from PIL import ImageFont
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFError

    folder = Path(bundle_root()) / "native" / "assets" / "fonts"
    with _FONT_LOCK:
        for name, file in (("OWInter", "Inter-Regular.ttf"), ("OWInterBold", "Inter-SemiBold.ttf"),
                           ("OWMono", "IBMPlexMono-Regular.ttf"), *_FALLBACK_FONTS):
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(_source_font(name, str(folder / file)))
        pdfmetrics.registerFontFamily("OWInter", normal="OWInter", bold="OWInterBold",
                                      italic="OWInter", boldItalic="OWInterBold")
        pdfmetrics.registerFontFamily("OWMono", normal="OWMono", bold="OWMono",
                                      italic="OWMono", boldItalic="OWMono")
        requested = look.font.strip()
        if requested and requested not in _FONT_NAMES:
            result = None
            try:
                font = None
                for candidate in (requested, requested.replace(" ", "") + ".ttf", requested + ".ttf"):
                    try:
                        font = ImageFont.truetype(candidate, 12)
                        break
                    except OSError:
                        continue
                if font is None:
                    raise OSError("font unavailable")
                path = Path(font.path)
                name = "OWUser" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
                pdfmetrics.registerFont(_source_font(name, str(path)))
                pdfmetrics.registerFontFamily(name, normal=name, bold=name, italic=name, boldItalic=name)
                result = (name, font.getname()[0])
            except (OSError, ValueError, TypeError, TTFError):
                pass
            _FONT_NAMES[requested] = result
        selected = _FONT_NAMES.get(requested)
    return selected or ("OWInter", "Inter")


def _source_font(name, path):
    from reportlab.pdfbase.ttfonts import TTFont, makeToUnicodeCMap

    class SourceFont(TTFont):
        def __init__(self):
            super().__init__(name, path)
            self._source_codes = {}
            self._source_text = {}
            self._next_source_code = 0xF0000

        def glyph_code(self, glyph, source, advance):
            key = glyph, source
            if key not in self._source_codes:
                code = self._next_source_code
                while code in self.face.charToGlyph:
                    code += 1
                self._next_source_code = code + 1
                self._source_codes[key] = code
                self._source_text[code] = source
                self.face.charToGlyph[code] = glyph
                self.face.charWidths[code] = self.face._pdfScale(advance)
            return self._source_codes[key]

        def addObjects(self, document):
            state = self.state.get(document)
            subsets = [list(subset) for subset in state.subsets] if state else []
            names = [self.getSubsetInternalName(index, document)[1:] for index in range(len(subsets))]
            super().addObjects(document)
            for internal_name, subset in zip(names, subsets):
                font = document.idToObject[internal_name]
                stream = document.idToObject[font.ToUnicode.name]
                cmap = makeToUnicodeCMap(font.BaseFont, subset).splitlines()
                start = next(index for index, line in enumerate(cmap) if line.endswith(" beginbfchar")) + 1
                # A shaped glyph can encode several source characters, or be
                # another mark in the same cluster. Keep original Unicode here
                # instead of exposing HarfBuzz's private glyph identifiers.
                cmap[start:start + len(subset)] = [
                    f"<{index:02X}> <{self._source_text.get(code, chr(code)).encode('utf-16-be').hex().upper()}>"
                    for index, code in enumerate(subset)
                ]
                stream.content = "\n".join(cmap)

    return SourceFont()


def _font_for_character(character, preferred):
    from reportlab.pdfbase.pdfmetrics import getFont

    code = ord(character)
    for name in (preferred, *(name for name, _file in _FALLBACK_FONTS)):
        if code in getFont(name).face.charToGlyph:
            return name
    return preferred


def _visual_runs(characters):
    from bidi import algorithm

    text = "".join(character for character, _fragment, _font in characters)
    storage = algorithm.get_empty_storage()
    storage["base_level"] = algorithm.get_base_level(text)
    storage["base_dir"] = "R" if storage["base_level"] else "L"
    algorithm.get_embedding_levels(text, storage)
    for index, character in enumerate(storage["chars"]):
        character["source_index"] = index
    algorithm.explicit_embed_and_overrides(storage)
    algorithm.resolve_weak_types(storage)
    algorithm.resolve_neutral_types(storage, False)
    algorithm.resolve_implicit_levels(storage, False)
    algorithm.reorder_resolved_levels(storage, False)
    algorithm.apply_mirroring(storage, False)
    runs = []
    for character in storage["chars"]:
        index = character["source_index"]
        _text, fragment, font = characters[index]
        direction = "rtl" if character["level"] % 2 else "ltr"
        if (runs and runs[-1][0] is fragment and runs[-1][1:3] == (font, direction)
                and index - runs[-1][3][-1][0] == (-1 if direction == "rtl" else 1)):
            runs[-1][3].append((index, character["ch"]))
        else:
            runs.append((fragment, font, direction, [(index, character["ch"])]))
    result = []
    for fragment, font, direction, indexed in runs:
        logical = sorted(indexed)
        result.append((fragment, font, direction, "".join(value for _index, value in logical),
                       "".join(characters[index][0] for index, _value in logical), logical[0][0]))
    return result, bool(storage["base_level"])


def _shape_run(fragment, font_name, direction, text, source, logical_index):
    import uharfbuzz
    from reportlab.pdfbase.pdfmetrics import getFont
    from reportlab.pdfbase.ttfonts import ShapeData, ShapedStr

    with _FONT_LOCK:
        font = getFont(font_name)
        harfbuzz = font.hbFont(fragment.fontSize)
        buffer = uharfbuzz.Buffer()
        buffer.add_str(text)
        buffer.guess_segment_properties()
        buffer.direction = direction
        uharfbuzz.shape(harfbuzz, buffer)
        clusters = sorted({info.cluster for info in buffer.glyph_infos} | {len(source)})
        ends = dict(zip(clusters, clusters[1:]))
        representatives = {}
        for index, (info, position) in enumerate(zip(buffer.glyph_infos, buffer.glyph_positions)):
            previous = representatives.get(info.cluster)
            if previous is None or abs(position.x_advance) > abs(buffer.glyph_positions[previous].x_advance):
                representatives[info.cluster] = index
        codes, positions = [], []
        for index, (info, position) in enumerate(zip(buffer.glyph_infos, buffer.glyph_positions)):
            original = source[info.cluster:ends[info.cluster]] if representatives[info.cluster] == index else ""
            code = font.glyph_code(info.codepoint, original, harfbuzz.get_glyph_h_advance(info.codepoint))
            codes.append(chr(code))
            positions.append(ShapeData(info.cluster, font.pdfScale(position.x_advance),
                font.pdfScale(position.y_advance), font.pdfScale(position.x_offset),
                font.pdfScale(position.y_offset), font.face.charWidths[code]))
        width = sum(position.x_advance for position in positions) * fragment.fontSize / 1000
    return fragment, font_name, ShapedStr("".join(codes), positions), logical_index, source, direction, width


def _outline_glyph(canvas, font, code, size, x, y):
    """Paint auxiliary marks without duplicating a cluster's extracted text.

    PDFium substitutes character codes for empty ToUnicode entries. These marks
    keep their vector outlines while the cluster's advancing glyph carries all
    of its source Unicode, including ligatures and combining characters.
    """
    path = canvas.beginPath()

    class Pen:
        def moveTo(self, point):
            self.current = self.start = point
            path.moveTo(*point)

        def lineTo(self, point):
            self.current = point
            path.lineTo(*point)

        def curveTo(self, first, second, end):
            self.current = end
            path.curveTo(*first, *second, *end)

        def qCurveTo(self, control, end):
            first = tuple(self.current[i] + (control[i] - self.current[i]) * 2 / 3 for i in (0, 1))
            second = tuple(end[i] + (control[i] - end[i]) * 2 / 3 for i in (0, 1))
            self.curveTo(first, second, end)

        def closePath(self):
            self.current = self.start
            path.close()

    harfbuzz = font.hbFont(size)
    harfbuzz.draw_glyph_with_pen(font.face.charToGlyph[code], Pen())
    canvas.saveState()
    canvas.translate(x, y)
    canvas.scale(size / harfbuzz.face.upem, size / harfbuzz.face.upem)
    canvas.drawPath(path, fill=1, stroke=0)
    canvas.restoreState()


def _pdf_paragraph(markup, style):
    from reportlab.pdfbase.pdfmetrics import getFont
    from reportlab.platypus import Flowable, Paragraph

    paragraph = Paragraph(markup, style)
    characters = []
    fallback = False
    for fragment in paragraph.frags:
        if getattr(fragment, "lineBreak", False):
            characters.append(("\n", fragment, fragment.fontName))
        for character in fragment.text:
            font = _font_for_character(character, fragment.fontName)
            fallback = fallback or font != fragment.fontName
            characters.append((character, fragment, font))
    if not fallback:
        return paragraph

    class ScriptParagraph(Flowable):
        def __init__(self, content=None, lines=None, first_line=True):
            super().__init__()
            self.style = style
            self.characters = characters if content is None else content
            self.lines = lines
            self._fixed_lines = lines is not None
            self._wrapped_width = None
            self.first_line = first_line
            self.leading = max(style.leading, style.fontSize * 1.5)

        def _line(self, content):
            runs, rtl = _visual_runs(content)
            shaped = [_shape_run(*run) for run in runs]
            actual = "".join(item[0] for item in content)
            return shaped, sum(run[-1] for run in shaped), rtl, actual

        def wrap(self, available_width, _available_height):
            if not self._fixed_lines and self._wrapped_width != available_width:
                self.lines = []
                line, width = [], 0
                room = available_width - style.leftIndent - style.rightIndent
                tokens, current = [], []
                for item in self.characters:
                    character = item[0]
                    cjk = 0x2E80 <= ord(character) <= 0x9FFF or 0xAC00 <= ord(character) <= 0xD7AF
                    if character == "\n" or cjk:
                        if current:
                            tokens.append(current)
                            current = []
                        tokens.append([item])
                    elif current and character.isspace() != current[-1][0].isspace():
                        tokens.append(current)
                        current = [item]
                    else:
                        current.append(item)
                if current:
                    tokens.append(current)
                for token in tokens:
                    if token[0][0] == "\n":
                        self.lines.append(self._line(line))
                        line, width = [], 0
                        continue
                    if not line and token[0][0].isspace() and style.fontName != "OWMono":
                        continue
                    token_width = self._line(token)[1]
                    indent = style.firstLineIndent if not self.lines else 0
                    if line and width + token_width > room - indent:
                        self.lines.append(self._line(line))
                        line, width = [], 0
                        if token[0][0].isspace():
                            continue
                    if token_width > room:
                        clusters = []
                        for item in token:
                            if clusters and (unicodedata.category(item[0]).startswith("M")
                                    or clusters[-1][-1][0] == "\u200d"
                                    or "VIRAMA" in unicodedata.name(clusters[-1][-1][0], "")):
                                clusters[-1].append(item)
                            else:
                                clusters.append([item])
                        for cluster in clusters:
                            cluster_width = self._line(cluster)[1]
                            if line and width + cluster_width > room:
                                self.lines.append(self._line(line))
                                line, width = [], 0
                            line.extend(cluster)
                            width += cluster_width
                    else:
                        line.extend(token)
                        width += token_width
                if line or not self.lines:
                    self.lines.append(self._line(line))
                self._wrapped_width = available_width
            self.width = available_width
            self.height = len(self.lines) * self.leading
            return self.width, self.height

        def split(self, available_width, available_height):
            self.wrap(available_width, available_height)
            count = int(available_height / self.leading)
            if count < 1:
                return []
            if count >= len(self.lines):
                return [self]
            return [ScriptParagraph(lines=self.lines[:count], first_line=self.first_line),
                    ScriptParagraph(lines=self.lines[count:], first_line=False)]

        def draw(self):
            from reportlab.lib.enums import TA_CENTER, TA_RIGHT
            from reportlab.pdfbase.ttfonts import ShapedStr

            canvas = self.canv
            canvas.saveState()
            if style.backColor:
                canvas.setFillColor(style.backColor)
                canvas.rect(0, 0, self.width, self.height, fill=1, stroke=0)
            if style.borderWidth and style.borderColor:
                canvas.setStrokeColor(style.borderColor)
                canvas.setLineWidth(style.borderWidth)
                canvas.rect(0, 0, self.width, self.height, fill=0, stroke=1)
            canvas.restoreState()
            for index, (runs, width, rtl, original) in enumerate(self.lines):
                if not rtl:
                    # Preserve logical source across mixed fonts and ligatures;
                    # plain RTL lines use the cluster maps directly because PDF
                    # readers apply different bidi rules to ActualText spans.
                    actual = ("\ufeff" + original).encode("utf-16-be").hex().upper()
                    canvas._code.append(f"/Span << /ActualText <{actual}> >> BDC")
                left = style.leftIndent + (style.firstLineIndent if index == 0 and self.first_line else 0)
                room = self.width - left - style.rightIndent
                x = left + ((room - width) / 2 if style.alignment == TA_CENTER else
                            room - width if style.alignment == TA_RIGHT or rtl else 0)
                y = self.height - self.leading * (index + 1) + (self.leading - style.fontSize) / 2
                positioned = []
                for fragment, font, shaped, logical_index, original, direction, advance in runs:
                    positioned.append((logical_index, x, fragment, font, shaped, original, direction, advance))
                    x += advance
                for _logical_index, x, fragment, font, shaped, original, direction, advance in sorted(positioned):
                    canvas.saveState()
                    canvas.setFillColor(fragment.textColor)
                    if getattr(fragment, "backColor", None):
                        canvas.setFillColor(fragment.backColor)
                        canvas.rect(x, y - 2, advance, fragment.fontSize * 1.3, fill=1, stroke=0)
                        canvas.setFillColor(fragment.textColor)
                    face = getFont(font)
                    cursor = 0
                    kept = []
                    for character, position in zip(shaped, shaped.__shapeData__):
                        glyph_x = x + (cursor + position.x_offset) * fragment.fontSize / 1000
                        glyph_y = y + fragment.rise + position.y_offset * fragment.fontSize / 1000
                        if not rtl or face._source_text.get(ord(character), character):
                            kept.append((character, position, cursor))
                        else:
                            _outline_glyph(canvas, face, ord(character), fragment.fontSize, glyph_x, glyph_y)
                        cursor += position.x_advance
                    if kept:
                        positions = [position._replace(x_advance=(kept[index + 1][2] if index + 1 < len(kept) else cursor) - offset)
                                     for index, (_character, position, offset) in enumerate(kept)]
                        text = canvas.beginText(x + kept[0][2] * fragment.fontSize / 1000, y + fragment.rise)
                        text.setFont(font, fragment.fontSize)
                        text.textOut(ShapedStr("".join(item[0] for item in kept), positions))
                        canvas.drawText(text)
                    for _sequence, kind, *_options in fragment.us_lines:
                        canvas.setStrokeColor(fragment.textColor)
                        canvas.setLineWidth(max(.4, fragment.fontSize / 20))
                        offset = fragment.fontSize * (.25 if kind == "strike" else -.125)
                        canvas.line(x, y + offset, x + advance, y + offset)
                    for _sequence, address in fragment.link:
                        canvas.linkURL(address, (x, y - 2, x + advance, y + fragment.fontSize), relative=1)
                    canvas.restoreState()
                if not rtl:
                    canvas._code.append("EMC")

    # Font metrics, fallback faces and layout are independent of GUI state.
    assert all(getFont(font).shapable for _character, _fragment, font in characters)
    return ScriptParagraph()


def _image_path(source, folder):
    try:
        source = str(source)
        if source.startswith(("//", "\\\\")):
            return None
        root = Path(folder).resolve()
        if Path(source).is_absolute():
            path = Path(unquote(source)).resolve()
        else:
            address = urlsplit(source)
            if address.scheme or address.netloc:
                return None
            path = (root / unquote(address.path)).resolve()
        path.relative_to(root)
        return path if path.is_file() else None
    except (OSError, ValueError):
        return None


def _image_data(path):
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(path) as original:
            image = ImageOps.exif_transpose(original)
            image.load()
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA")
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue(), image.width, image.height
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return None


def _link(href):
    try:
        address = urlsplit(href)
    except ValueError:
        return ""
    return href if address.scheme.lower() in ("http", "https", "mailto") or href.startswith("#") else ""


def _inline(tokens, folder):
    runs = []
    bold = italic = strike = 0
    href = ""
    for token in tokens or ():
        kind = token.type
        if kind in ("strong_open", "strong_close"):
            bold += token.nesting
        elif kind in ("em_open", "em_close"):
            italic += token.nesting
        elif kind in ("s_open", "s_close"):
            strike += token.nesting
        elif kind == "link_open":
            href = _link(token.attrGet("href") or "")
        elif kind == "link_close":
            href = ""
        elif kind == "image":
            image = _image_path(token.attrGet("src") or "", folder)
            if image is not None:
                runs.append(_Run(image=image))
        elif kind in ("text", "code_inline", "html_inline", "softbreak", "hardbreak"):
            text = "\n" if kind in ("softbreak", "hardbreak") else token.content
            runs.append(_Run(_clean(text), bool(bold), bool(italic), bool(strike), kind == "code_inline", href))
    return runs


def _markdown(text, folder):
    from markdown_it import MarkdownIt

    tokens = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough").parse(_clean(text))
    blocks = []
    lists = []
    marker = ""
    heading = quote = 0
    table = row = None
    for token in tokens:
        kind = token.type
        if kind in ("ordered_list_open", "bullet_list_open"):
            lists.append([kind == "ordered_list_open", int(token.attrGet("start") or 1)])
        elif kind in ("ordered_list_close", "bullet_list_close"):
            lists.pop()
        elif kind == "list_item_open":
            ordered, number = lists[-1]
            marker = f"{number}." if ordered else "\N{BULLET}"
            lists[-1][1] += 1
        elif kind == "list_item_close":
            marker = ""
        elif kind == "heading_open":
            heading = int(token.tag[1:])
        elif kind == "heading_close":
            heading = 0
        elif kind == "blockquote_open":
            quote += 1
        elif kind == "blockquote_close":
            quote -= 1
        elif kind == "table_open":
            table = _Block("table", indent=len(lists), quote=quote)
        elif kind == "tr_open":
            row = []
        elif kind == "tr_close":
            table.rows.append(row)
            row = None
        elif kind == "table_close":
            blocks.append(table)
            table = None
        elif kind == "inline":
            runs = _inline(token.children, folder)
            if table is not None:
                row.append(runs)
            elif runs:
                blocks.append(_Block("heading" if heading else "paragraph", runs, heading, marker, len(lists), quote))
                marker = ""
        elif kind in ("fence", "code_block"):
            blocks.append(_Block("code", [_Run(token.content.rstrip("\n"), code=True)], indent=len(lists), quote=quote))
        elif kind == "hr":
            blocks.append(_Block("rule"))
    return blocks


def _atomic_write(path, writer):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    try:
        writer(Path(temporary))
        with open(temporary, "rb+") as document:
            os.fsync(document.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def _pdf_markup(runs, font):
    result = []
    for run in runs:
        if run.image is not None:
            continue
        text = escape(run.text).replace("\n", "<br/>")
        for enabled, tag in ((run.bold, "b"), (run.italic, "i"), (run.strike, "strike")):
            if enabled:
                text = f"<{tag}>{text}</{tag}>"
        if run.code:
            text = f'<font name="OWMono" backColor="{_CODE}">{text}</font>'
        if run.href and not run.href.startswith("#"):
            text = f"<link href={quoteattr(run.href)} color=\"#315ea8\"><u>{text}</u></link>"
        result.append(text)
    return "".join(result)


def _pdf_styles(look=Look()):
    from reportlab.lib.styles import ParagraphStyle

    font, _family = _fonts(look)
    color = _color(look.text_color, _INK)
    heading = _color(look.heading_color, color)
    body = ParagraphStyle("body", fontName=font, fontSize=11, leading=15.95, textColor=color, spaceBefore=6, spaceAfter=0)
    return {
        "body": body,
        "title": ParagraphStyle("title", parent=body, fontName="OWInterBold" if font == "OWInter" else font,
                                fontSize=22, leading=28, spaceBefore=0, textColor=heading, keepWithNext=True),
        "meta": ParagraphStyle("meta", parent=body, fontSize=9.5, leading=13, textColor=_MUTED, spaceBefore=3),
        "caption": ParagraphStyle("caption", parent=body, fontSize=8.5, leading=12, textColor="#8a8278", alignment=1, spaceBefore=3),
        "code": ParagraphStyle("code", parent=body, fontName="OWMono", fontSize=9, leading=12, backColor=_CODE, borderPadding=5),
        "heading_color": heading,
    }


def _pdf_image(path, width, height):
    from reportlab.platypus import Flowable, Image

    data = _image_data(path)
    if data is None:
        return None
    content, pixels_w, pixels_h = data
    scale = min(width / pixels_w, height / pixels_h)

    class FramedImage(Flowable):
        def __init__(self):
            super().__init__()
            self.width, self.height = pixels_w * scale, pixels_h * scale
            self.hAlign = "CENTER"

        def draw(self):
            Image(io.BytesIO(content), self.width, self.height).drawOn(self.canv, 0, 0)
            self.canv.setStrokeColor(_FRAME)
            self.canv.setLineWidth(.6)
            self.canv.rect(0, 0, self.width, self.height)

    return FramedImage()


def _pdf_blocks(blocks, styles, width, height):
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import HRFlowable, Spacer, Table, TableStyle

    flow = []
    for block in blocks:
        indent = block.indent * 16 + block.quote * 12
        available = max(36, width - indent)
        if block.kind == "rule":
            flow.append(HRFlowable(width="100%", color=_FRAME, spaceBefore=8, spaceAfter=8))
            continue
        if block.kind == "table":
            columns = max((len(row) for row in block.rows), default=0)
            if not columns:
                continue
            cells = []
            for row_index, row in enumerate(block.rows):
                cells.append([_pdf_paragraph(("<b>" if row_index == 0 else "") + _pdf_markup(cell, styles["body"].fontName)
                                        + ("</b>" if row_index == 0 else ""), styles["body"])
                              for cell in row] + [""] * (columns - len(row)))
            table = Table(cells, colWidths=[available / columns] * columns, repeatRows=1, splitInRow=1, hAlign="LEFT")
            table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, 0), 1, _FRAME),
                                       ("LINEBELOW", (0, 1), (-1, -1), .4, _FRAME), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
            flow.extend((Spacer(1, 8), table))
            continue
        base = styles["code"] if block.kind == "code" else styles["body"]
        style = ParagraphStyle("block", parent=base, leftIndent=indent)
        if block.kind == "heading":
            style.fontSize = {1: 22, 2: 17, 3: 14}.get(block.level, 12)
            style.leading = style.fontSize * 1.3
            style.textColor = styles["heading_color"]
            style.fontName = "OWInterBold" if base.fontName == "OWInter" else base.fontName
            style.spaceBefore = 14
            style.keepWithNext = True
        if block.quote:
            style.borderColor = _FRAME
            style.borderWidth = .6
            style.borderPadding = 6
        if block.marker:
            style.firstLineIndent = -16
        text_runs = []

        def flush():
            if not text_runs:
                return
            markup = _pdf_markup(text_runs, base.fontName)
            if block.marker:
                markup = escape(block.marker) + "\u00a0\u00a0" + markup
            if block.kind == "code":
                markup = escape("".join(run.text for run in text_runs)).replace(" ", "&#160;").replace("\n", "<br/>")
            flow.append(_pdf_paragraph(markup, style))
            text_runs.clear()

        for run in block.runs:
            if run.image is None:
                text_runs.append(run)
            else:
                flush()
                image = _pdf_image(run.image, available, height * .62)
                if image is not None:
                    flow.extend((Spacer(1, 8), image, Spacer(1, 8)))
        flush()
    return flow


def _write_pdf(path, story, title, page_size):
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate

    page = _page_size(page_size)
    def render(temporary):
        document = BaseDocTemplate(str(temporary), pagesize=page, leftMargin=54, rightMargin=54,
                                   topMargin=50.4, bottomMargin=50.4, title=_clean(title), author="", creator="OpenWhisper")
        frame = Frame(54, 50.4, page[0] - 108, page[1] - 100.8, leftPadding=0,
                      rightPadding=0, topPadding=0, bottomPadding=0)
        document.addPageTemplates(PageTemplate(id="walkthrough", frames=[frame]))
        document.build(story)
    return _atomic_write(path, render)


def write_pdf(walkthrough: Walkthrough, folder: Path, *, page_size=None, document_format=DocumentFormat()):
    from reportlab.platypus import KeepTogether, Spacer

    styles = _pdf_styles()
    page = _page_size(page_size)
    width, height = page[0] - 108, page[1] - 100.8
    parts = _parts(walkthrough)
    header = []
    if document_format.title:
        header.append(_pdf_paragraph(escape(_clean(document_format.title)), styles["title"]))
    header.extend(_pdf_paragraph(escape(_clean(line)), styles["meta"]) for line in _header(walkthrough, _shot_count(parts), document_format))
    story = []
    for index, part in enumerate(parts):
        group = header[:] if index == 0 else []
        group.append(Spacer(1, 16))
        group.extend(_pdf_paragraph(escape(_clean(words)).replace("\n", "<br/>"), styles["body"]) for words in _paragraphs(part.words))
        image = _pdf_image(part.shot, width, height * .62) if part.shot is not None else None
        if image is not None:
            group.extend((Spacer(1, 6), image))
            if document_format.captions:
                group.append(_pdf_paragraph(escape(_label(part, " · ")), styles["caption"]))
            total = sum(item.wrap(width, height)[1] + item.getSpaceBefore() + item.getSpaceAfter() for item in group)
            if total > height - 12:
                shrink = min(image.height * .5, total - height + 12)
                factor = (image.height - shrink) / image.height
                image.width *= factor
                image.height *= factor
            story.append(KeepTogether(group))
        else:
            story.extend(group)
    if not parts:
        story = header
    title = f"{document_format.title or TITLE} · {_when(walkthrough.started_at)}"
    return _write_pdf(Path(folder) / PDF_NAME, story or [Spacer(1, 1)], title, page_size)


def write_markdown_pdf(text, path, *, folder, title, page_size=None, look=Look()):
    from reportlab.platypus import Spacer

    page = _page_size(page_size)
    story = _pdf_blocks(_markdown(text, folder), _pdf_styles(look), page[0] - 108, page[1] - 100.8)
    return _write_pdf(path, story or [Spacer(1, 1)], title, page_size)


def _word_document(page_size, title, look=Look()):
    from docx import Document
    from docx.shared import Pt, RGBColor

    _font, family = _fonts(look)
    word = Document()
    section = word.sections[0]
    width, height = _page_size(page_size)
    section.page_width, section.page_height = Pt(width), Pt(height)
    section.left_margin = section.right_margin = Pt(54)
    section.top_margin = section.bottom_margin = Pt(50.4)
    normal = word.styles["Normal"]
    normal.font.name, normal.font.size = family, Pt(11)
    normal.font.color.rgb = RGBColor.from_string(_color(look.text_color, _INK)[1:])
    for level in range(1, 10):
        word.styles[f"Heading {level}"].font.name = family
    normal.paragraph_format.line_spacing = 1.45
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.space_before = Pt(6)
    properties = word.core_properties
    properties.title = _clean(title)
    properties.author = properties.last_modified_by = ""
    properties.comments = "Made by OpenWhisper"
    return word


def _word_runs(paragraph, runs, width, height, *, bold=False, color=None):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.shared import Pt, RGBColor

    for item in runs:
        if item.image is not None:
            data = _image_data(item.image)
            if data is not None:
                content, pixels_w, pixels_h = data
                scale = min(width / pixels_w, height * .62 / pixels_h)
                paragraph.add_run().add_picture(io.BytesIO(content), width=Pt(pixels_w * scale), height=Pt(pixels_h * scale))
            continue
        run = paragraph.add_run(_clean(item.text))
        run.bold = item.bold or bold or None
        run.italic = item.italic or None
        run.font.strike = item.strike or None
        if color:
            run.font.color.rgb = RGBColor.from_string(color[1:])
        if item.code:
            run.font.name, run.font.size = "IBM Plex Mono", Pt(9)
        if item.href:
            hyperlink = OxmlElement("w:hyperlink")
            if item.href.startswith("#"):
                hyperlink.set(qn("w:anchor"), item.href[1:])
            else:
                identifier = paragraph.part.relate_to(item.href, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
                hyperlink.set(qn("r:id"), identifier)
            run.font.underline = True
            run.font.color.rgb = RGBColor.from_string("315EA8")
            hyperlink.append(run._r)
            paragraph._p.append(hyperlink)


def _word_decoration(paragraph, kind):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    properties = paragraph._p.get_or_add_pPr()
    if kind == "code":
        shading = OxmlElement("w:shd")
        shading.set(qn("w:fill"), _CODE[1:].upper())
        properties.append(shading)
    else:
        borders = OxmlElement("w:pBdr")
        edge = OxmlElement("w:left" if kind == "quote" else "w:bottom")
        for name, value in (("val", "single"), ("sz", "5"), ("space", "6"), ("color", _FRAME[1:])):
            edge.set(qn(f"w:{name}"), value)
        borders.append(edge)
        properties.append(borders)


def _word_blocks(word, blocks, look=Look()):
    from docx.oxml import OxmlElement
    from docx.shared import Pt

    section = word.sections[0]
    width = section.page_width.pt - section.left_margin.pt - section.right_margin.pt
    height = section.page_height.pt - section.top_margin.pt - section.bottom_margin.pt
    heading_color = _color(look.heading_color, _color(look.text_color, _INK))
    for block in blocks:
        if block.kind == "table":
            columns = max((len(row) for row in block.rows), default=0)
            if not columns:
                continue
            table = word.add_table(rows=len(block.rows), cols=columns)
            table.style = "Table Grid"
            for index, row in enumerate(block.rows):
                for column, runs in enumerate(row):
                    _word_runs(table.cell(index, column).paragraphs[0], runs, width / columns - 12, height, bold=index == 0)
                table.rows[index]._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
            table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
            continue
        paragraph = word.add_paragraph(style=f"Heading {min(9, block.level)}" if block.kind == "heading" else None)
        layout = paragraph.paragraph_format
        if block.indent or block.quote:
            layout.left_indent = Pt(block.indent * 16 + block.quote * 12)
        if block.marker:
            layout.first_line_indent = Pt(-16)
            paragraph.add_run(block.marker + "\t")
        if block.kind == "heading":
            layout.keep_with_next = True
            _word_runs(paragraph, block.runs, width, height, bold=True, color=heading_color)
            for run in paragraph.runs:
                run.font.size = Pt({1: 22, 2: 17, 3: 14}.get(block.level, 12))
        else:
            _word_runs(paragraph, block.runs, width, height)
        if block.kind in ("code", "rule") or block.quote:
            _word_decoration(paragraph, "quote" if block.quote else block.kind)


def write_docx(walkthrough: Walkthrough, folder: Path, *, page_size=None, document_format=DocumentFormat()):
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    title = f"{document_format.title or TITLE} · {_when(walkthrough.started_at)}"
    word = _word_document(page_size, title)
    parts = _parts(walkthrough)
    if document_format.title:
        paragraph = word.add_paragraph()
        run = paragraph.add_run(_clean(document_format.title))
        run.bold, run.font.size = True, Pt(22)
        paragraph.paragraph_format.keep_with_next = True
    for line in _header(walkthrough, _shot_count(parts), document_format):
        paragraph = word.add_paragraph(_clean(line))
        paragraph.paragraph_format.keep_with_next = True
        for run in paragraph.runs:
            run.font.size, run.font.color.rgb = Pt(9.5), RGBColor.from_string(_MUTED[1:])
    section = word.sections[0]
    width, height = section.page_width.pt - 108, section.page_height.pt - 100.8
    for part in parts:
        group = []
        for words in _paragraphs(part.words):
            group.append(word.add_paragraph(_clean(words)))
        if part.shot is not None and _image_data(part.shot) is not None:
            paragraph = word.add_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _word_runs(paragraph, [_Run(image=part.shot)], width, height)
            group.append(paragraph)
            if document_format.captions:
                caption = word.add_paragraph(_label(part, " · "))
                caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in caption.runs:
                    run.font.size = Pt(8.5)
                group.append(caption)
        for paragraph in group[:-1]:
            paragraph.paragraph_format.keep_with_next = True
        if group:
            group[-1].paragraph_format.keep_with_next = False
    return _atomic_write(Path(folder) / DOCX_NAME, lambda temporary: word.save(str(temporary)))


def write_markdown_docx(text, path, *, folder, title, page_size=None, look=Look()):
    word = _word_document(page_size, title, look)
    _word_blocks(word, _markdown(text, folder), look)
    return _atomic_write(path, lambda temporary: word.save(str(temporary)))
