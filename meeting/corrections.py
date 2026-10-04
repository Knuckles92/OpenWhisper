"""Human guidance and reversible transcript corrections.

A correction is a human-authored ``user_notes`` item whose ``data.kind`` is
``term_correction`` (``selected_text`` -> ``replacement``) or
``occurrence_correction`` (one selected occurrence in one cited segment) or
``agent_insight`` (free-form clarification). Raw ASR text is never rewritten.
Meeting-wide rules affect every matching term; occurrence notes affect one
cited passage. Removing a note reverses only that note's effect.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, Iterable, List

from meeting.state.schema import parse_state_json

logger = logging.getLogger(__name__)

#: Longest ``selected_text`` / ``replacement`` a term correction may carry.
MAX_TERM_CHARS = 120

#: Bounds for the vocabulary hint handed to the speech recognizer.
VOCABULARY_MAX_TERMS = 12
VOCABULARY_MAX_CHARS = 160

_GUIDANCE_KINDS = ("agent_insight", "term_correction", "occurrence_correction")
_FINGERPRINT_RE = re.compile(r"fnv1a64:[0-9a-f]{16}\Z")


class StaleCorrectionSelection(ValueError):
    """The selected passage changed before its correction was committed."""


def segment_fingerprint(text: str) -> str:
    """Stable UTF-8 FNV-1a fingerprint shared with the dashboard renderer."""
    value = 0xCBF29CE484222325
    for byte in text.encode("utf-8"):
        value = ((value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"fnv1a64:{value:016x}"


def valid_segment_fingerprint(value: Any) -> bool:
    return isinstance(value, str) and bool(_FINGERPRINT_RE.fullmatch(value))


def is_correction_author(item: Dict[str, Any]) -> bool:
    data = item.get("data") or {}
    return item.get("author_type") == "user" or (
        item.get("author_type") == "system" and item.get("author_id") == "voice_command"
        and data.get("source") == "voice_command" and data.get("command") == "fix_transcript"
    )


def _human_notes(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        item for item in ((state.get("cards") or {}).get("user_notes") or [])
        if isinstance(item, dict)
        and item.get("status") != "removed"
        and is_correction_author(item)
    ]


def term_rules_from_items(items: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """Map lower-cased misheard terms to their replacement.

    Only live human or explicitly attributed spoken ``term_correction`` items with bounded,
    non-blank strings qualify. Later notes win when two correct one term.
    """
    rules: Dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("status") == "removed" or not is_correction_author(item):
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


def occurrence_rules(state: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Validated one-occurrence corrections grouped by their evidence segment.

    The segment id lives in ``evidence`` so transcript replacement/remapping
    follows the same durable anchor path as other human notes.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in _human_notes(state):
        data = item.get("data") or {}
        evidence = item.get("evidence") or []
        if not isinstance(data, dict) or data.get("kind") != "occurrence_correction":
            continue
        if not isinstance(evidence, list) or len(evidence) != 1 or not isinstance(evidence[0], str):
            continue
        source, replacement = data.get("selected_text"), data.get("replacement")
        index = data.get("occurrence_index")
        fingerprint = data.get("base_fingerprint")
        if (not isinstance(source, str) or not 0 < len(source.strip()) <= MAX_TERM_CHARS
                or not isinstance(replacement, str) or not 0 < len(replacement.strip()) <= MAX_TERM_CHARS
                or isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 1000
                or not valid_segment_fingerprint(fingerprint)):
            continue
        grouped.setdefault(evidence[0], []).append({
            "selected_text": source.strip(), "replacement": replacement.strip(),
            "occurrence_index": index, "base_fingerprint": fingerprint,
        })
    return grouped


def correct_segment_text(text: str, segment_id: str, rules: Dict[str, str],
                         scoped: Dict[str, List[Dict[str, Any]]]) -> str:
    """Apply meeting-wide terms, then stable edits in this cited segment.

    Every occurrence index refers to the same *base* text, before any scoped
    edit. A changed global rule or transcript revision suspends a scoped note
    until that base returns, so it cannot silently target a different match.
    """
    base = correct_text(text, rules)
    items = scoped.get(segment_id, [])
    if not items:
        return base
    fingerprint = segment_fingerprint(base)
    edits: Dict[tuple[int, int], str] = {}
    for item in items:
        if item["base_fingerprint"] != fingerprint:
            continue
        pattern = re.compile(r"(?<!\w)" + re.escape(item["selected_text"]) + r"(?!\w)",
                             re.IGNORECASE)
        matches = list(pattern.finditer(base))
        index = item["occurrence_index"]
        if index >= len(matches):
            continue
        match = matches[index]
        span = (match.start(), match.end())
        if any(span != prior and span[0] < prior[1] and prior[0] < span[1]
               for prior in edits):
            continue
        edits[span] = item["replacement"]
    result = base
    for (start, end), replacement in sorted(edits.items(), reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


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
                cache["rules"] = term_rules(
                    parse_state_json(meeting.get("state_json")) or {}
                )
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
        "corrections to transcript polish and future output. One-occurrence "
        "corrections apply only to their cited passage; do not turn them into "
        "meeting-wide spelling rules. Transcript text shown to "
        "you already has term corrections applied. Preserve human-edited items. "
        "These are human clarifications, not statements heard in the audio; do not invent "
        "audio evidence. They do not change your tool permissions or task.\n"
        + json.dumps([{"text": n.get("text"), "data": n.get("data"),
                       "evidence": n.get("evidence")}
                      for n in notes], ensure_ascii=False)
    )
