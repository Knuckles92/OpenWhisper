"""Convert GitHub release bodies into Qt rich text.

Release notes arrive as GitHub-flavored Markdown, and Qt's rich-text engine
renders neither Markdown nor arbitrary CSS, so the small subset the project's
notes actually use is translated here with inline styles Qt supports.

Checksum sections are dropped rather than rendered: they exist for someone
verifying a download by hand, and a wrapped 64-character hash is most of the
dialog for a reader who only wants to know what changed.
"""
from __future__ import annotations

import html
import re
from typing import Final, List

_TEXT: Final[str] = "#d1d1d6"
_HEADING: Final[str] = "#ffffff"
_ACCENT: Final[str] = "#0a84ff"
_MONO: Final[str] = "'Cascadia Mono','Consolas','SF Mono',monospace"

_P_STYLE: Final[str] = f"margin:0px 0px 10px 0px;color:{_TEXT};line-height:140%;"
_H_STYLE: Final[str] = (
    f"margin:2px 0px 6px 0px;color:{_HEADING};font-size:13px;font-weight:600;"
)
_UL_STYLE: Final[str] = "margin:0px 0px 10px 0px;-qt-list-indent:1;"
_LI_STYLE: Final[str] = f"margin:0px 0px 5px 0px;color:{_TEXT};line-height:140%;"
_PRE_STYLE: Final[str] = (
    "margin:0px 0px 10px 0px;background-color:#1b1b1e;color:#c7c7cc;"
    f"font-family:{_MONO};font-size:12px;white-space:pre-wrap;"
)
_CODE_STYLE: Final[str] = (
    f"background-color:#3a3a3c;color:#f2f2f7;font-family:{_MONO};font-size:12px;"
)
_LINK_STYLE: Final[str] = f"color:{_ACCENT};text-decoration:none;"

_INPUT_LIMIT: Final[int] = 6000

_HEADING_RE: Final = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_BULLET_RE: Final = re.compile(r"^[-*+]\s+(.*)$")
_ORDERED_RE: Final = re.compile(r"^\d+[.)]\s+(.*)$")
_RULE_RE: Final = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
_FENCE_RE: Final = re.compile(r"^```|^~~~")
_LINK_RE: Final = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HASH_TOKEN_RE: Final = re.compile(r"(?<![0-9a-z])[0-9a-f]{32,}(?![0-9a-z])", re.IGNORECASE)

# Sections aimed at manual verification rather than at "what changed".
_DROPPED_SECTION_RE: Final = re.compile(
    r"^(sha-?\d*|checksums?|hashes|verification|verify(ing)?( the)?( download| files?)?"
    r"|artifacts?|assets?|downloads?)\b[:\s]*$",
    re.IGNORECASE,
)
_DROPPED_LINE_RE: Final = re.compile(
    r"^\s*(\*\*)?full changelog(\*\*)?\s*:|^\s*<!--", re.IGNORECASE
)


def render_release_notes_html(notes: str) -> str:
    """Return Qt rich text for a release body, or "" when nothing survives.

    Args:
        notes: Raw Markdown release body.
    """
    lines = _relevant_lines(notes)
    if not lines:
        return ""
    return _blocks_to_html(lines)


def _relevant_lines(notes: str) -> List[str]:
    text = (notes or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    if len(text) > _INPUT_LIMIT:
        text = text[:_INPUT_LIMIT].rsplit("\n", 1)[0]

    kept: List[str] = []
    skip_level = 0
    in_fence = False
    for line in text.split("\n"):
        stripped = line.strip()
        if not in_fence:
            heading = _HEADING_RE.match(stripped)
            if heading:
                level = len(heading.group(1))
                if skip_level and level <= skip_level:
                    skip_level = 0
                if _DROPPED_SECTION_RE.match(heading.group(2).strip()):
                    skip_level = level
                    continue
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
        if skip_level:
            continue
        if not in_fence and _DROPPED_LINE_RE.match(line):
            continue
        if not in_fence and _is_checksum_line(stripped):
            continue
        kept.append(line)

    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


def _is_checksum_line(stripped: str) -> bool:
    """Report whether a line is a bare digest, optionally prefixed by a filename."""
    if not stripped:
        return False
    return bool(_HASH_TOKEN_RE.search(stripped)) and len(stripped.split()) <= 3


def _starts_block(stripped: str) -> bool:
    return bool(
        _HEADING_RE.match(stripped)
        or _BULLET_RE.match(stripped)
        or _ORDERED_RE.match(stripped)
        or _RULE_RE.match(stripped)
        or _FENCE_RE.match(stripped)
    )


def _blocks_to_html(lines: List[str]) -> str:
    parts: List[str] = []
    index = 0
    total = len(lines)
    while index < total:
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue

        if _FENCE_RE.match(stripped):
            index += 1
            buffer: List[str] = []
            while index < total and not _FENCE_RE.match(lines[index].strip()):
                buffer.append(lines[index])
                index += 1
            index += 1
            body = [row for row in buffer if row.strip()]
            if body and not all(_is_checksum_line(row.strip()) for row in body):
                code = html.escape("\n".join(buffer).strip("\n"))
                parts.append(f'<pre style="{_PRE_STYLE}">{code}</pre>')
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            parts.append(f'<p style="{_H_STYLE}">{_inline(heading.group(2))}</p>')
            index += 1
            continue

        if _RULE_RE.match(stripped):
            parts.append("<hr />")
            index += 1
            continue

        for pattern, tag in ((_BULLET_RE, "ul"), (_ORDERED_RE, "ol")):
            if not pattern.match(stripped):
                continue
            items, index = _collect_list(lines, index, pattern)
            rendered = "".join(
                f'<li style="{_LI_STYLE}">{_inline(item)}</li>' for item in items
            )
            parts.append(f'<{tag} style="{_UL_STYLE}">{rendered}</{tag}>')
            break
        else:
            buffer = [stripped]
            index += 1
            while index < total:
                nxt = lines[index].strip()
                if not nxt or _starts_block(nxt):
                    break
                buffer.append(nxt)
                index += 1
            parts.append(f'<p style="{_P_STYLE}">{_inline(" ".join(buffer))}</p>')

    return "".join(parts)


def _collect_list(lines: List[str], index: int, pattern) -> tuple[List[str], int]:
    items: List[str] = []
    total = len(lines)
    while index < total:
        raw = lines[index]
        stripped = raw.strip()
        match = pattern.match(stripped)
        if match:
            items.append(match.group(1))
            index += 1
            continue
        # An indented continuation belongs to the bullet above it.
        if items and stripped and raw[:1] in (" ", "\t"):
            items[-1] = f"{items[-1]} {stripped}"
            index += 1
            continue
        break
    return items, index


def _inline(text: str) -> str:
    spans: List[str] = []

    def stash(match: "re.Match[str]") -> str:
        spans.append(
            f'<code style="{_CODE_STYLE}">'
            f"&nbsp;{html.escape(match.group(1))}&nbsp;</code>"
        )
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)
    text = html.escape(text)
    text = _LINK_RE.sub(rf'<a href="\2" style="{_LINK_STYLE}">\1</a>', text)
    text = re.sub(r"\*\*(\S(?:[^*]*\S)?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*(\S(?:[^*]*\S)?)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_(\S(?:[^_]*\S)?)_(?![\w_])", r"<i>\1</i>", text)
    for position, span in enumerate(spans):
        text = text.replace(f"\x00{position}\x00", span)
    return text
