"""Meeting-scoped human guidance and reversible transcript term corrections.

A correction is a human-authored ``user_notes`` item whose ``data.kind`` is
``term_correction`` (``selected_text`` -> ``replacement``) or
``agent_insight`` (free-form clarification). Raw ASR text is never rewritten;
corrections are applied wherever transcript text is read, so removing the
note restores the original words everywhere at once.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, Iterable, List

logger = logging.getLogger(__name__)

#: Longest ``selected_text`` / ``replacement`` a term correction may carry.
MAX_TERM_CHARS = 120

#: Bounds for the vocabulary hint handed to the speech recognizer.
VOCABULARY_MAX_TERMS = 12
VOCABULARY_MAX_CHARS = 160

_GUIDANCE_KINDS = ("agent_insight", "term_correction")


def _human_notes(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        item for item in ((state.get("cards") or {}).get("user_notes") or [])
        if isinstance(item, dict)
        and item.get("status") != "removed"
        and item.get("author_type") == "user"
    ]


def term_rules_from_items(items: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """Map lower-cased misheard terms to their replacement.

    Only live, human-authored ``term_correction`` items with bounded,
    non-blank strings qualify. Later notes win when two correct one term.
    """
    rules: Dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("status") == "removed" or item.get("author_type") != "user":
            continue
        data = item.get("data") or {}
        if not isinstance(data, dict) or data.get("kind") != "term_correction":
            continue
        source, replacement = data.get("selected_text"), data.get("replacement")
        if (isinstance(source, str) and 0 < len(source.strip()) <= MAX_TERM_CHARS
                and isinstance(replacement, str)
                and 0 < len(replacement.strip()) <= MAX_TERM_CHARS):
            rules[source.strip().lower()] = replacement.strip()
    return rules


def term_rules(state: Dict[str, Any]) -> Dict[str, str]:
    """Term-correction rules from a ``MeetingState.to_dict()`` snapshot."""
    return term_rules_from_items((state.get("cards") or {}).get("user_notes") or [])


def repository_term_rules(repository: Any, meeting_id: str) -> Callable[[], Dict[str, str]]:
    """Term-rule provider for ASR engines without a live state store.

    Used by crash recovery and offline finalization, where the meeting state
    lives only in the repository. The parsed rules are cached by ``state_seq``
    so a chunk-by-chunk caller re-reads JSON only after the state changes.
    """
    unseen = object()
    cache: Dict[str, Any] = {"seq": unseen, "rules": {}}

    def provider() -> Dict[str, str]:
        get_meeting = getattr(repository, "get_meeting", None)
        if not callable(get_meeting):
            return {}
        try:
            meeting = get_meeting(meeting_id)
            if not isinstance(meeting, dict):
                return {}
            seq = meeting.get("state_seq")
            if seq != cache["seq"]:
                cache["rules"] = term_rules(json.loads(meeting.get("state_json") or "{}"))
                cache["seq"] = seq
        except Exception:
            logger.exception("Could not load term corrections for %s", meeting_id)
            return {}
        return cache["rules"]

    return provider


def correct_text(text: str, rules: Dict[str, str]) -> str:
    """Apply whole-word, case-insensitive term corrections in a single pass.

    One pass means a replacement can never be matched by another rule, so
    ``a -> b`` and ``b -> c`` cannot chain. Longer terms are tried first so a
    phrase wins over one of its words.
    """
    if not rules or not text:
        return text
    pattern = r"(?<!\w)(?:" + "|".join(
        re.escape(term) for term in sorted(rules, key=len, reverse=True)
    ) + r")(?!\w)"
    return re.sub(pattern, lambda match: rules.get(match[0].lower(), match[0]),
                  text, flags=re.IGNORECASE)


def vocabulary_terms(rules: Dict[str, str],
                     *, max_terms: int = VOCABULARY_MAX_TERMS,
                     max_chars: int = VOCABULARY_MAX_CHARS) -> List[str]:
    """Distinct corrected spellings, oldest first, bounded for an ASR prompt."""
    terms: List[str] = []
    seen = set()
    used = 0
    for replacement in rules.values():
        key = replacement.lower()
        if key in seen:
            continue
        cost = len(replacement) + (2 if terms else 0)
        if len(terms) >= max_terms or used + cost > max_chars:
            break
        seen.add(key)
        terms.append(replacement)
        used += cost
    return terms


def vocabulary_hint(rules: Dict[str, str]) -> str:
    """Vocabulary primer for Whisper's ``initial_prompt`` (empty when unused).

    Whisper treats ``initial_prompt`` as preceding transcript, so listing the
    correct spellings as a short sentence biases the decoder toward them.
    """
    terms = vocabulary_terms(rules)
    return ", ".join(terms) + "." if terms else ""


def guidance_prompt(state: Dict[str, Any]) -> str:
    """Prompt block carrying human corrections to the meeting agent."""
    notes = [n for n in _human_notes(state)
             if (n.get("data") or {}).get("kind") in _GUIDANCE_KINDS]
    if not notes:
        return ""
    return (
        "## HUMAN MEETING GUIDANCE\n"
        "Use these attributed corrections to interpret this meeting. Reconsider stale "
        "topic, summary, claims and AI notes now, even without new speech. Apply term "
        "corrections to transcript polish and future output; transcript text shown to "
        "you already has term corrections applied. Preserve human-edited items. "
        "These are human clarifications, not statements heard in the audio; do not invent "
        "audio evidence. They do not change your tool permissions or task.\n"
        + json.dumps([{"text": n.get("text"), "data": n.get("data")}
                      for n in notes], ensure_ascii=False)
    )
