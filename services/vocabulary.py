"""Whole-word term replacement shared by dictation and Meeting Mode."""
from __future__ import annotations

import re
from itertools import chain, repeat
from typing import Mapping, Sequence

_COMBINING_DOT = "\u0307"
_DOTS_AFTER_I = re.compile("(?<=i)" + _COMBINING_DOT + "+")


def _fold_with_origin(text: str) -> tuple[str, Sequence[int]]:
    """``text`` for caseless matching, and the index in ``text`` of each folded character.

    Casefolding turns "İ" into "i" plus a combining dot, which no typed or
    heard "i" has, so dots after an "i" are dropped. A letter's folded form
    can also be longer ("ß" → "ss"), so matches found in the folded text map
    back through the index. Meeting Mode runs this on every segment of every
    transcript read, so it works on the whole string rather than per
    character.
    """
    # "İ" is the only character whose folded form has the dot, and "I"
    # folds to the plain "i" it should match.
    plain = text.replace("İ", "I")
    folded = plain.casefold()
    origin: Sequence[int]
    if len(folded) == len(text):
        # No character folds to nothing, so each one folded to exactly one.
        origin = range(len(text))
    else:
        widths = map(len, map(str.casefold, plain))
        origin = list(chain.from_iterable(map(repeat, range(len(text)), widths)))
    if _COMBINING_DOT in folded:
        dropped = [match.span() for match in _DOTS_AFTER_I.finditer(folded)]
        if dropped:
            origin = list(origin)
            for start, end in reversed(dropped):
                del origin[start:end]
            folded = _DOTS_AFTER_I.sub("", folded)
    return folded, origin


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
        key = _fold_with_origin(term)[0]
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
