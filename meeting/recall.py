"""Consent-gated, bounded recall of past meeting transcripts.

Meeting agents call this through ``AgentToolHost.search_past_meetings``.
Policy lives here so no caller can bypass consent, caps, or the rule that
past-meeting segment ids never leave this module — they share the ``sg_``
prefix of live evidence ids and would be fuzzy-repaired onto the current
transcript.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_HIT_LIMIT = 10
MAX_HIT_LIMIT = 20
MAX_SNIPPET_CHARS = 180
MAX_TOTAL_CHARS = 4000
MAX_SLICE_SEGMENTS = 40

_SG_ID_RE = re.compile(r"\bsg_[0-9a-fA-F]{6,}\b")

__all__ = [
    "DEFAULT_HIT_LIMIT",
    "MAX_HIT_LIMIT",
    "MAX_SNIPPET_CHARS",
    "MAX_TOTAL_CHARS",
    "search_past_meetings",
]


def search_past_meetings(
    repository: Any,
    *,
    query: str = "",
    current_meeting_id: str = "",
    meeting_id: Optional[str] = None,
    limit: int = DEFAULT_HIT_LIMIT,
) -> Dict[str, Any]:
    """Bounded, consent-gated keyword recall across stored transcripts.

    Args:
        repository: A ``MeetingRepository``. ``None`` disables the search.
        query: Free-text search terms. Ignored when ``meeting_id`` is set.
        current_meeting_id: The live (or target) meeting; always excluded.
        meeting_id: Optional past meeting to return a bounded transcript slice.
        limit: Requested hit cap. Clamped to ``MAX_HIT_LIMIT``.

    Returns:
        ``{"ok", "disabled"?, "text", "hits"}``. ``text`` is what the model
        sees. Hits never include ``sg_`` segment ids.
    """
    if not _recall_enabled():
        return _disabled("Past-meeting recall is disabled.")
    if repository is None:
        return _disabled("Past-meeting recall is not available.")

    current_id = str(current_meeting_id or "").strip()
    target_id = str(meeting_id or "").strip() or None
    hit_limit = _clamp_limit(limit)

    try:
        if target_id:
            return _slice_meeting(
                repository,
                meeting_id=target_id,
                current_meeting_id=current_id,
                limit=hit_limit,
            )
        return _search_meetings(
            repository,
            query=query,
            current_meeting_id=current_id,
            limit=hit_limit,
        )
    except Exception:
        logger.exception("Past-meeting recall failed")
        return {
            "ok": False,
            "text": "Past-meeting recall failed.",
            "hits": [],
        }


def _recall_enabled() -> bool:
    try:
        from services.settings import resolve_meeting_past_recall_enabled

        return bool(resolve_meeting_past_recall_enabled())
    except Exception:
        return False


def _clamp_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = DEFAULT_HIT_LIMIT
    return max(1, min(value, MAX_HIT_LIMIT))


def _disabled(text: str) -> Dict[str, Any]:
    return {"ok": False, "disabled": True, "text": text, "hits": []}


def _search_meetings(
    repository: Any,
    *,
    query: str,
    current_meeting_id: str,
    limit: int,
) -> Dict[str, Any]:
    cleaned = (query or "").strip()
    if not cleaned:
        return {
            "ok": False,
            "text": "Provide a search query or a past meeting id.",
            "hits": [],
        }
    search = getattr(repository, "search_transcripts", None)
    if not callable(search):
        return _disabled("Past-meeting recall is not available.")
    rows = search(
        cleaned, exclude_meeting_id=current_meeting_id or None, limit=limit,
    ) or []
    return _format_hits(rows, limit=limit, current_meeting_id=current_meeting_id)


def _slice_meeting(
    repository: Any,
    *,
    meeting_id: str,
    current_meeting_id: str,
    limit: int,
) -> Dict[str, Any]:
    if meeting_id == current_meeting_id:
        return {
            "ok": False,
            "text": (
                "Cannot recall the current meeting; it is already in "
                "the transcript."
            ),
            "hits": [],
        }
    getter = getattr(repository, "get_meeting", None)
    meeting = getter(meeting_id) if callable(getter) else None
    if not meeting:
        return {"ok": False, "text": "Unknown meeting.", "hits": []}
    segments_fn = getattr(repository, "get_segments", None)
    if not callable(segments_fn):
        return _disabled("Past-meeting recall is not available.")
    segments = segments_fn(meeting_id, limit=min(limit, MAX_SLICE_SEGMENTS)) or []
    title = _title(meeting.get("title"))
    date = _date_label(meeting.get("started_at"))
    hits: List[Dict[str, Any]] = []
    lines = [f"Meeting \"{title}\" ({date})"]
    budget = MAX_TOTAL_CHARS - len(lines[0])
    for index, segment in enumerate(segments, 1):
        if index > MAX_SLICE_SEGMENTS or budget <= 0:
            break
        snippet = _clip(segment.get("text") or "", MAX_SNIPPET_CHARS)
        if not snippet:
            continue
        ref = _opaque_ref(meeting_id, index)
        start_s = _start_s(segment.get("start_s"))
        line = f"{ref}  t={start_s:g}s  {snippet}"
        if len(line) > budget:
            break
        lines.append(line)
        budget -= len(line) + 1
        hits.append({
            "ref": ref,
            "meeting_id": meeting_id,
            "title": title,
            "started_at": date,
            "start_s": start_s,
            "snippet": snippet,
        })
    if len(lines) == 1:
        return {
            "ok": True,
            "text": f"No transcript text in \"{title}\" ({date}).",
            "hits": [],
        }
    return {"ok": True, "text": "\n".join(lines), "hits": hits}


def _format_hits(
    rows: List[Dict[str, Any]],
    *,
    limit: int,
    current_meeting_id: str,
) -> Dict[str, Any]:
    hits: List[Dict[str, Any]] = []
    lines: List[str] = []
    budget = MAX_TOTAL_CHARS
    per_meeting: Dict[str, int] = {}
    for row in rows:
        if len(hits) >= limit or budget <= 0:
            break
        meeting_id = str(row.get("meeting_id") or "").strip()
        if not meeting_id or meeting_id == current_meeting_id:
            continue
        snippet = _clip(
            row.get("snippet") or row.get("text") or "", MAX_SNIPPET_CHARS,
        )
        if not snippet:
            continue
        per_meeting[meeting_id] = per_meeting.get(meeting_id, 0) + 1
        ref = _opaque_ref(meeting_id, per_meeting[meeting_id])
        title = _title(row.get("title"))
        date = _date_label(row.get("started_at"))
        start_s = _start_s(row.get("start_s"))
        line = f"{ref}  \"{title}\" ({date}) t={start_s:g}s — {snippet}"
        if len(line) > budget:
            break
        lines.append(line)
        budget -= len(line) + 1
        hits.append({
            "ref": ref,
            "meeting_id": meeting_id,
            "title": title,
            "started_at": date,
            "start_s": start_s,
            "snippet": snippet,
        })
    if not hits:
        return {"ok": True, "text": "No past-meeting matches.", "hits": []}
    return {"ok": True, "text": "\n".join(lines), "hits": hits}


def _opaque_ref(meeting_id: str, index: int) -> str:
    return f"past:{meeting_id}:{index}"


def _title(value: Any) -> str:
    title = _sanitize(str(value or "")).strip()
    return title or "Untitled meeting"


def _date_label(started_at: Any) -> str:
    raw = str(started_at or "").strip()
    if len(raw) >= 10 and raw[4] == "-":
        return raw[:10]
    return raw or "unknown date"


def _start_s(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clip(text: Any, max_chars: int) -> str:
    cleaned = _sanitize(str(text or "")).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"


def _sanitize(text: str) -> str:
    return _SG_ID_RE.sub("[id]", text)
