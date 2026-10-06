"""Whole-word term replacement shared by dictation and Meeting Mode."""
from __future__ import annotations

import re
from typing import Mapping


def replace_terms(text: str, rules: Mapping[str, str]) -> str:
    """Apply whole-word, case-insensitive replacements in a single pass.

    ``rules`` maps a lower-cased term to its replacement. One pass means a
    replacement can never be matched by another rule, so ``a -> b`` and
    ``b -> c`` cannot chain. Longer terms are tried first so a phrase wins
    over one of its words.
    """
    if not rules or not text:
        return text
    pattern = r"(?<!\w)(?:" + "|".join(
        re.escape(term) for term in sorted(rules, key=len, reverse=True)
    ) + r")(?!\w)"
    return re.sub(pattern, lambda match: rules.get(match[0].lower(), match[0]),
                  text, flags=re.IGNORECASE)
