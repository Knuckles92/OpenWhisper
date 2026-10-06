"""Snippets: a spoken trigger phrase becomes exact text, without AI.

A whole-utterance trigger skips cleanup. Triggers inside a sentence become
protected placeholders before cleanup and are expanded after it; if cleanup
loses a placeholder, the uncleaned text with expansions is used instead.
Live dictation only.

Snippet text is the user's own content: never log it, or a trigger.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass
from html import escape
from typing import Mapping, Sequence
from uuid import uuid4

from config import config
from services.settings import SettingsKey

MIN_TRIGGER_CHARS = 2
MAX_TRIGGER_CHARS = 60
MAX_TEXT_CHARS = 10_000


@dataclass(frozen=True)
class Snippet:
    id: str
    trigger: str
    text: str
    #: Render light Markdown to HTML and paste it as rich text.
    formatted: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SnippetPlan:
    #: The text to clean: placeholders stand in for triggers.
    text: str
    #: Set when the whole utterance was one trigger.
    whole: Snippet | None = None
    placeholders: tuple[tuple[str, Snippet], ...] = ()


# A word keeps apostrophes and dots between its letters ("don't",
# "acme.com"); anything else, hyphens included, separates words, so "sign-off"
# and "sign off" say the same trigger.
_WORD = re.compile(r"\w+(?:['’.]\w+)*")
# What may sit between the words of one trigger inside a sentence: speech
# engines add commas and hyphens, but a sentence end splits a trigger.
_WORD_GAP = re.compile(r"[\s,\-‐‑‒–—]*")
# Plain ASCII, so every cleanup model and tokenizer sees ordinary characters,
# and speech engines never write double brackets. Matched loosely
# ("[[ s1 ]]") because a model may space or lowercase them.
_PLACEHOLDER = re.compile(r"\[\[\s*[Ss]\s*(\d{1,4})\s*\]\]")


def _placeholder(number: int) -> str:
    return f"[[S{number}]]"


def _words(text: str) -> list[tuple[str, int, int]]:
    return [
        (match.group(0).casefold().replace("’", "'"), match.start(), match.end())
        for match in _WORD.finditer(text or "")
    ]


def normalize(text: str) -> str:
    """``text`` as triggers are compared: casefolded words, no punctuation."""
    return " ".join(word for word, _start, _end in _words(text))


def load_snippets(settings: Mapping) -> list[Snippet]:
    """The valid saved snippets, in their saved order."""
    raw = (settings or {}).get(SettingsKey.DICTATION_SNIPPETS)
    if not isinstance(raw, list):
        return []
    snippets: list[Snippet] = []
    ids: set[str] = set()
    triggers: set[str] = set()
    for item in raw:
        if len(snippets) >= config.MAX_SNIPPETS:
            break
        if not isinstance(item, dict):
            continue
        snippet_id, trigger, text = (item.get(key) for key in ("id", "trigger", "text"))
        if not all(isinstance(value, str) for value in (snippet_id, trigger, text)):
            continue
        snippet_id, trigger = snippet_id.strip(), trigger.strip()
        key = normalize(trigger)
        if (
            not snippet_id
            or snippet_id in ids
            or key in triggers
            or _trigger_problem(trigger)
            or _text_problem(text)
        ):
            continue
        ids.add(snippet_id)
        triggers.add(key)
        snippets.append(Snippet(snippet_id, trigger, text, item.get("formatted") is True))
    return snippets


def new_snippet_id() -> str:
    return uuid4().hex


def _trigger_problem(trigger: str) -> str:
    trigger = trigger.strip()
    if not trigger:
        return "Add a trigger phrase to say."
    key = normalize(trigger)
    if not key or _PLACEHOLDER.search(trigger):
        return "Use words you can say for the trigger."
    if len(key) < MIN_TRIGGER_CHARS or len(trigger) > MAX_TRIGGER_CHARS:
        return f"Use {MIN_TRIGGER_CHARS} to {MAX_TRIGGER_CHARS} characters for the trigger."
    return ""


def _text_problem(text: str) -> str:
    if not text.strip():
        return "Add the text to insert."
    if len(text) > MAX_TEXT_CHARS:
        return f"Keep the text to {MAX_TEXT_CHARS:,} characters or fewer."
    return ""


def find_duplicate(trigger: str, snippets: Sequence[Snippet], *, exclude_id: str = "") -> Snippet | None:
    """The snippet whose trigger is said the same way as ``trigger``, if any."""
    key = normalize(trigger)
    if not key:
        return None
    return next(
        (s for s in snippets if s.id != exclude_id and normalize(s.trigger) == key),
        None,
    )


def trigger_note(trigger: str, snippets: Sequence[Snippet], *, exclude_id: str = "") -> str:
    """A short heads-up about how ``trigger`` sits with the other triggers, or "".

    In order: a duplicate (which saving refuses), a trigger inside a longer
    one or containing a shorter one, a near-identical spelling speech could
    confuse, and a single everyday word.
    """
    key = normalize(trigger)
    if not key:
        return ""
    if find_duplicate(trigger, snippets, exclude_id=exclude_id) is not None:
        return "Another snippet already uses this trigger."
    padded = f" {key} "
    others = [s for s in snippets if s.id != exclude_id]
    for other in others:
        other_key = normalize(other.trigger)
        if f" {other_key} " in padded:
            return f"Includes “{other.trigger}”. Saying the full phrase inserts this snippet."
        if padded in f" {other_key} ":
            return f"Also part of “{other.trigger}”, which wins when you say it in full."
    for other in others:
        if difflib.SequenceMatcher(None, key, normalize(other.trigger)).ratio() >= 0.85:
            return f"Sounds close to “{other.trigger}”; speech might mix them up."
    if " " not in key:
        return "A single word can come up in everyday speech; a short phrase is safer."
    return ""


def validate_snippet(snippet: Snippet, settings: Mapping) -> None:
    """Raise ValueError with a message for the user when ``snippet`` can't be saved."""
    problem = _trigger_problem(snippet.trigger) or _text_problem(snippet.text)
    if problem:
        raise ValueError(problem)
    existing = load_snippets(settings)
    if find_duplicate(snippet.trigger, existing, exclude_id=snippet.id) is not None:
        raise ValueError("Another snippet already uses this trigger. Choose a different phrase.")
    if len(existing) >= config.MAX_SNIPPETS and all(s.id != snippet.id for s in existing):
        raise ValueError(
            f"You have {config.MAX_SNIPPETS} snippets, the most OpenWhisper keeps. "
            "Delete one to add another."
        )


def save_snippet(settings: dict, snippet: Snippet) -> None:
    """Add or replace ``snippet``; mutate inside SettingsManager.mutate_settings."""
    validate_snippet(snippet, settings)
    snippet = Snippet(snippet.id, snippet.trigger.strip(), snippet.text, bool(snippet.formatted))
    snippets = load_snippets(settings)
    index = next((i for i, s in enumerate(snippets) if s.id == snippet.id), len(snippets))
    snippets[index:index + 1] = [snippet]
    settings[SettingsKey.DICTATION_SNIPPETS] = [s.to_dict() for s in snippets]


def delete_snippet(settings: dict, snippet_id: str) -> None:
    """Remove one snippet; mutate inside SettingsManager.mutate_settings."""
    settings[SettingsKey.DICTATION_SNIPPETS] = [
        s.to_dict() for s in load_snippets(settings) if s.id != snippet_id
    ]


def plan_expansion(text: str, snippets: Sequence[Snippet]) -> SnippetPlan:
    """Find triggers in ``text`` and protect them for cleanup.

    The whole utterance matching a trigger (ignoring case and punctuation)
    makes a ``whole`` plan. Otherwise every whole-word trigger occurrence,
    longest trigger first and never overlapping, becomes a numbered
    placeholder, one per occurrence.
    """
    text = text or ""
    keyed = [(snippet, normalize(snippet.trigger).split()) for snippet in snippets]
    keyed = [(snippet, words) for snippet, words in keyed if words]
    words = _words(text)
    if not keyed or not words:
        return SnippetPlan(text)
    spoken = [word for word, _start, _end in words]
    for snippet, trigger_words in keyed:
        if spoken == trigger_words:
            return SnippetPlan(text, whole=snippet)
    if _PLACEHOLDER.search(text):
        # Already holds something that reads as a placeholder; expanding
        # would replace the wrong text.
        return SnippetPlan(text)

    by_first_word: dict[str, list[tuple[Snippet, list[str]]]] = {}
    for position in sorted(
        range(len(keyed)),
        key=lambda i: (-len(keyed[i][1]), -len(" ".join(keyed[i][1])), i),
    ):
        snippet, trigger_words = keyed[position]
        by_first_word.setdefault(trigger_words[0], []).append((snippet, trigger_words))
    pieces: list[str] = []
    placeholders: list[tuple[str, Snippet]] = []
    cursor = index = 0
    while index < len(words):
        match = None
        for snippet, trigger_words in by_first_word.get(spoken[index], ()):
            count = len(trigger_words)
            if spoken[index:index + count] == trigger_words and _contiguous(text, words, index, count):
                match = snippet, count
                break
        if match is None:
            index += 1
            continue
        snippet, count = match
        token = _placeholder(len(placeholders) + 1)
        pieces += [text[cursor:words[index][1]], token]
        placeholders.append((token, snippet))
        cursor = words[index + count - 1][2]
        index += count
    if not placeholders:
        return SnippetPlan(text)
    pieces.append(text[cursor:])
    return SnippetPlan("".join(pieces), placeholders=tuple(placeholders))


def _contiguous(text: str, words, index: int, count: int) -> bool:
    return all(
        _WORD_GAP.fullmatch(text[words[i][2]:words[i + 1][1]])
        for i in range(index, index + count - 1)
    )


def prompt_guard(plan: SnippetPlan) -> str:
    """The cleanup-prompt instruction to keep ``plan``'s placeholders, or ""."""
    tokens = [token for token, _snippet in plan.placeholders]
    if not tokens:
        return ""
    if len(tokens) == 1:
        return (
            f"The transcript contains the placeholder {tokens[0]}, which stands for "
            "text inserted later; keep it exactly once, unchanged and in its place."
        )
    listed = ", ".join(tokens[:-1]) + f" and {tokens[-1]}"
    return (
        f"The transcript contains the placeholders {listed}, each standing for text "
        "inserted later; keep every one exactly once, unchanged and in its place."
    )


def expand(text: str, plan: SnippetPlan) -> tuple[str, str, bool]:
    """``(plain, html, ok)``: ``text`` with ``plan``'s placeholders expanded.

    ``html`` is "" unless a formatted snippet was used; ``ok`` is False when
    a placeholder is missing from ``text``, appears twice, or ``text`` holds
    one the plan never made.
    """
    if plan.whole is not None:
        snippet = plan.whole
        return plain_text(snippet), snippet_html(snippet), True
    if not plan.placeholders:
        return text, "", True
    by_number = {}
    for token, snippet in plan.placeholders:
        match = _PLACEHOLDER.fullmatch(token)
        if match is not None:
            by_number[int(match.group(1))] = snippet
    found = [int(match.group(1)) for match in _PLACEHOLDER.finditer(text)]
    if sorted(found) != sorted(by_number) or len(by_number) != len(plan.placeholders):
        return text, "", False

    rich = any(snippet.formatted for _token, snippet in plan.placeholders)
    plain_parts: list[str] = []
    html_parts: list[str] = []
    cursor = 0
    for match in _PLACEHOLDER.finditer(text):
        snippet = by_number[int(match.group(1))]
        before = text[cursor:match.start()]
        plain_parts += [before, plain_text(snippet)]
        if rich:
            html_parts += [_escape_lines(before), snippet_html(snippet) or _escape_lines(snippet.text)]
        cursor = match.end()
    rest = text[cursor:]
    plain_parts.append(rest)
    if rich:
        html_parts.append(_escape_lines(rest))
    return "".join(plain_parts), "".join(html_parts), True


def plain_text(snippet: Snippet) -> str:
    """What ``snippet`` types as plain text: its text, Markdown stripped if formatted."""
    return markdown_to_plain(snippet.text) if snippet.formatted else snippet.text


def snippet_html(snippet: Snippet) -> str:
    """``snippet`` as an HTML fragment when it keeps formatting, else ""."""
    return markdown_to_html(snippet.text) if snippet.formatted else ""


# --- light Markdown -----------------------------------------------------------

_LIST_ITEM = re.compile(r"^ {0,3}(?:(?P<bullet>[-*+])|(?P<number>\d{1,9})[.)])[ \t]+(?P<body>.*)$")
_INLINE = re.compile(
    r"\\(?P<escaped>[\\`*_\[\]()#+\-.!])"
    r"|\[(?P<label>[^\]\n]+)\]\((?P<href>[^)\s]+)\)"
    r"|(?P<url>https?://[^\s<>\"]*[^\s<>\".,;:!?)\]'])"
    r"|\*\*(?P<bold>(?!\s).+?(?<!\s))\*\*"
    r"|(?<!\w)__(?P<bold_>(?!\s).+?(?<!\s))__(?!\w)"
    r"|\*(?P<italic>(?![\s*])[^*\n]+?(?<!\s))\*"
    r"|(?<!\w)_(?P<italic_>(?![\s_])[^_\n]+?(?<!\s))_(?!\w)"
)
_SAFE_SCHEMES = ("http://", "https://", "mailto:", "tel:")


def _safe_href(href: str) -> str:
    if href.lower().startswith(_SAFE_SCHEMES):
        return href
    if href.lower().startswith("www."):
        return "https://" + href
    return ""


def _inline_html(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in _INLINE.finditer(text):
        parts.append(escape(text[cursor:match.start()], quote=False))
        cursor = match.end()
        if match["escaped"] is not None:
            parts.append(escape(match["escaped"], quote=False))
        elif match["label"] is not None:
            href = _safe_href(match["href"])
            if href:
                parts.append(f'<a href="{escape(href)}">{_inline_html(match["label"])}</a>')
            else:
                parts.append(escape(match.group(0), quote=False))
        elif match["url"] is not None:
            parts.append(f'<a href="{escape(match["url"])}">{escape(match["url"], quote=False)}</a>')
        elif (bold := match["bold"] or match["bold_"]) is not None:
            parts.append(f"<b>{_inline_html(bold)}</b>")
        else:
            parts.append(f"<i>{_inline_html(match['italic'] or match['italic_'])}</i>")
    parts.append(escape(text[cursor:], quote=False))
    return "".join(parts)


def _inline_plain(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in _INLINE.finditer(text):
        parts.append(text[cursor:match.start()])
        cursor = match.end()
        if match["escaped"] is not None:
            parts.append(match["escaped"])
        elif match["label"] is not None:
            label, href = _inline_plain(match["label"]), match["href"]
            if not _safe_href(href):
                parts.append(match.group(0))
            elif label == href:
                parts.append(href)
            else:
                parts.append(f"{label} ({href})")
        elif match["url"] is not None:
            parts.append(match["url"])
        else:
            inner = match["bold"] or match["bold_"] or match["italic"] or match["italic_"]
            parts.append(_inline_plain(inner))
    parts.append(text[cursor:])
    return "".join(parts)


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def markdown_to_plain(text: str) -> str:
    """Light Markdown as readable plain text: markers dropped, links as "label (url)"."""
    lines = []
    for line in _lines(text):
        item = _LIST_ITEM.match(line)
        if item is None:
            lines.append(_inline_plain(line))
        else:
            lines.append(line[:item.start("body")] + _inline_plain(item["body"]))
    return "\n".join(lines)


def markdown_to_html(text: str) -> str:
    """Light Markdown as an HTML fragment.

    Bold, italic, links (http, https, mailto, tel), bare web addresses, line
    breaks and bullet or numbered lists; everything else is escaped.
    """
    blocks: list[tuple[str, str, list[str]]] = []
    for line in _lines(text.strip("\r\n")):
        item = _LIST_ITEM.match(line)
        kind = "text" if item is None else ("ul" if item["bullet"] else "ol")
        content = _inline_html(line if item is None else item["body"])
        if blocks and blocks[-1][0] == kind:
            blocks[-1][2].append(content)
        else:
            first = item["number"] if item is not None and item["number"] else "1"
            blocks.append((kind, "" if first == "1" else f' start="{int(first)}"', [content]))
    parts = []
    for index, (kind, attributes, lines) in enumerate(blocks):
        if kind == "text":
            # Lists are blocks already, so blank lines beside one only add gaps.
            if index > 0:
                while lines and not lines[0].strip():
                    lines.pop(0)
            if index < len(blocks) - 1:
                while lines and not lines[-1].strip():
                    lines.pop()
            parts.append("<br>".join(lines))
        else:
            items = "".join(f"<li>{line}</li>" for line in lines)
            parts.append(f"<{kind}{attributes}>{items}</{kind}>")
    return "".join(parts)


def _escape_lines(text: str) -> str:
    return "<br>".join(escape(line, quote=False) for line in _lines(text))
