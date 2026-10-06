"""Whole-word term replacement shared by dictation and Meeting Mode."""
from __future__ import annotations

import re
from typing import Mapping

_COMBINING_DOT = "̇"


def _fold(text: str) -> str:
    """``text`` for caseless matching.

    Casefolding turns "İ" into "i" plus a combining dot, which no typed or
    heard "i" has, so that dot is dropped after an "i".
    """
    return text.casefold().replace("i" + _COMBINING_DOT, "i")


def _fold_with_origin(text: str) -> tuple[str, list[int]]:
    """``text`` folded like ``_fold``, and the index in ``text`` of each folded character.

    A letter's folded form can be longer ("ß" → "ss") or, for a dropped
    dot, empty, so matches found in the folded text map back through this.
    """
    pieces: list[str] = []
    origin: list[int] = []
    previous = ""
    for index, char in enumerate(text):
        piece = "" if char == _COMBINING_DOT and previous == "i" else _fold(char)
        pieces.append(piece)
        origin.extend([index] * len(piece))
        previous = piece[-1:] or previous
    return "".join(pieces), origin


def replace_terms(text: str, rules: Mapping[str, str]) -> str:
    """Apply whole-word, case-insensitive replacements in a single pass.

    ``rules`` maps a term, in any case, to its replacement; terms that
    differ only in case count once, the first one winning. One pass means
    a replacement can never be matched by another rule, so ``a -> b`` and
    ``b -> c`` cannot chain. Longer terms are tried first so a phrase wins
    over one of its words.
    """
    if not rules or not text:
        return text
    replacements: dict[str, str] = {}
    for term, replacement in rules.items():
        key = _fold(term)
        if key:
            replacements.setdefault(key, replacement)
    if not replacements:
        return text
    pattern = r"(?<!\w)(?:" + "|".join(
        re.escape(key) for key in sorted(replacements, key=len, reverse=True)
    ) + r")(?!\w)"
    folded, origin = _fold_with_origin(text)
    pieces: list[str] = []
    cursor = 0
    for match in re.finditer(pattern, folded):
        start, end = match.span()
        # Only whole original characters are replaced, never part of one
        # that folded to several.
        if start and origin[start - 1] == origin[start]:
            continue
        if end < len(origin) and origin[end - 1] == origin[end]:
            continue
        pieces += [text[cursor:origin[start]], replacements[match[0]]]
        cursor = origin[end] if end < len(origin) else len(text)
    pieces.append(text[cursor:])
    return "".join(pieces)
