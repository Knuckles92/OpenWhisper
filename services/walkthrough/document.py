"""A walkthrough written out as Markdown, a PDF and a JSON manifest.

A walkthrough is one continuous recording with screenshots kept along the
way. Each screenshot closes a section: the words said since the previous
screenshot come first, then the screenshot they describe. Speech after the
last screenshot is a trailing section without one.

walkthrough.md is for terminal agents and editors, walkthrough.pdf for chat
apps and people, and walkthrough.json keeps the sections as data for a later
write-up. walkthrough.docx, for Word, is laid out here too and written by
``word``. Everything here may run on a worker thread: the PDF is made with
a portable PDF/Word pipeline in the Rune runtime, with the legacy renderer
retained for the existing Qt interface.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


from services.walkthrough.storage import MANIFEST_NAME, MARKDOWN_NAME

logger = logging.getLogger(__name__)

TITLE = "Walkthrough"
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
    #: A screenshot Review left out of the documents; kept so it can return.
    omitted: Path | None = None
    #: What the user held Hold for note to say here: for the AI write-up only,
    #: so never in the documents, only in the manifest.
    notes: tuple[str, ...] = ()


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


@dataclass(frozen=True)
class Look:
    """A write-up's colours and font, which its Markdown can't say.

    "" keeps the walkthrough's own. A colour is anything QColor reads, such
    as "purple" or "#5b2a86"; one it can't is ignored, and a font this
    computer doesn't have falls back to the walkthrough's.
    """

    text_color: str = ""
    #: Headings; "" gives them the text's colour.
    heading_color: str = ""
    font: str = ""


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
    recording, and the start with its UTC offset. Notes held for the agent
    are kept with their section, for a later write-up to follow.
    """
    manifest = {
        "version": MANIFEST_VERSION,
        "started_at": walkthrough.started_at.astimezone().isoformat(timespec="seconds"),
        "duration_s": walkthrough.duration_s,
        "sections": [
            {
                "text": _tidy(section.text),
                "shot": _shot_name(section.shot, folder),
                "kept_at_s": section.kept_at_s,
                # Older readers ignore this and see a section without a screenshot.
                **({"omitted_shot": _shot_name(section.omitted, folder)} if section.omitted else {}),
                **({"notes": list(section.notes)} if section.notes else {}),
            }
            # As in the documents, an empty section is dropped, unless Review
            # is holding a screenshot for it or it holds notes for a write-up.
            for section in walkthrough.sections
            if section.shot is not None or section.omitted is not None or _tidy(section.text)
            or section.notes
        ],
    }
    path = folder / MANIFEST_NAME
    from services.walkthrough.library import atomic_json
    atomic_json(path, manifest)
    return path


def _shot_name(shot: Path | None, folder: Path) -> str | None:
    if shot is None:
        return None
    return _relative(shot, folder) or os.path.abspath(shot)



def write_pdf(walkthrough, folder, *, page_size=None, document_format=DocumentFormat()):
    from services.walkthrough.portable_document import write_pdf as render
    return render(walkthrough, folder, page_size=page_size, document_format=document_format)


def write_markdown_pdf(text, path, *, folder, title, page_size=None, look=Look()):
    from services.walkthrough.portable_document import write_markdown_pdf as render
    return render(text, path, folder=folder, title=title, page_size=page_size, look=look)


# Existing Qt imports retain their exact module identity and patch points.
if os.environ.get("OPENWHISPER_DESKTOP_RUNTIME") != "native":
    import sys
    from services.walkthrough import document_qt
    sys.modules[__name__] = document_qt
