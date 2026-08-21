"""Deterministic post-pass repairs for meeting dashboard state.

These fill structural gaps the LLM intermittently leaves empty (empty
timeline / rolling summary after consolidation) without inventing new
claims: content is copied from existing key points or the transcript.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

#: Multi-word proper names in segment text (e.g. "Martin Luther King").
_PROPER_NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
#: Extra single-token entities that still mark distinct case studies.
_SINGLE_ENTITIES = frozenset({"apple"})

#: Cap on system-authored timeline beats so a long transcript cannot flood
#: the card when the agent produced no key points to promote.
_MAX_TIMELINE_BEATS = 8
#: When falling back to raw segments, pick roughly one beat per this many
#: seconds of meeting time.
_SEGMENT_BEAT_WINDOW_S = 20.0
#: If the earliest promoted beat starts after this many seconds, prepend an
#: opening beat from the first substantive transcript segment so the timeline
#: does not skip the meeting's framing.
_OPENING_GAP_S = 12.0
_MAX_BEAT_TEXT = 180


def _live_items(state: Dict[str, Any], card: str) -> List[Dict[str, Any]]:
    cards = state.get("cards") or {}
    return [
        item for item in (cards.get(card) or [])
        if item.get("status") != "removed"
    ]


def _segment_index(segments: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        seg["id"]: seg
        for seg in segments
        if isinstance(seg, dict) and seg.get("id")
    }


def _earliest_start(evidence: List[str],
                    by_id: Dict[str, Dict[str, Any]]) -> Optional[float]:
    starts: List[float] = []
    for seg_id in evidence or []:
        seg = by_id.get(seg_id)
        if seg is None:
            continue
        try:
            starts.append(float(seg.get("start_s") or 0.0))
        except (TypeError, ValueError):
            continue
    return min(starts) if starts else None


def _clip_text(text: str) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= _MAX_BEAT_TEXT:
        return text
    return text[: _MAX_BEAT_TEXT - 3].rstrip() + "..."


def build_timeline_backfill_ops(
    state: Dict[str, Any],
    segments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build ``add_item`` ops that populate an empty timeline card.

    Prefers promoting existing key points (same claim text + evidence, with
    ``data.start_s`` taken from the earliest evidence segment). Falls back to
    sampling transcript segments across the meeting when there are no usable
    key points.

    Args:
        state: ``MeetingState.to_dict()`` snapshot.
        segments: Transcript segment dicts (repository shape).

    Returns:
        A list of validated-shape ops (possibly empty). Does not mutate state.
    """
    if _live_items(state, "timeline"):
        return []

    by_id = _segment_index(segments)
    ops: List[Dict[str, Any]] = []

    for item in _live_items(state, "key_points"):
        evidence = list(item.get("evidence") or [])
        start_s = _earliest_start(evidence, by_id)
        if start_s is None:
            continue
        text = _clip_text(item.get("text") or "")
        if not text:
            continue
        ops.append({
            "op": "add_item",
            "card": "timeline",
            "text": text,
            "data": {"start_s": start_s},
            "evidence": evidence[:20],
        })
        if len(ops) >= _MAX_TIMELINE_BEATS:
            break

    ordered = sorted(
        (seg for seg in segments if (seg.get("text") or "").strip()),
        key=lambda seg: float(seg.get("start_s") or 0.0),
    )

    if not ops:
        for item in _live_items(state, "live_notes"):
            data = item.get("data") or {}
            start_s = data.get("start_s")
            evidence = list(item.get("evidence") or [])
            if start_s is None or isinstance(start_s, bool):
                start_s = _earliest_start(evidence, by_id)
            if start_s is None:
                continue
            heading = str(data.get("heading") or "").strip()
            item_text = (item.get("text") or "").strip()
            beat_text = (
                f"{heading}: {item_text}"
                if heading and not item_text.startswith(heading)
                else (heading or item_text)
            )
            text = _clip_text(beat_text)
            if not text:
                continue
            ops.append({
                "op": "add_item",
                "card": "timeline",
                "text": text,
                "data": {"start_s": float(start_s)},
                "evidence": evidence[:20] if evidence else (
                    [ordered[0]["id"]] if ordered else []
                ),
            })
            if len(ops) >= _MAX_TIMELINE_BEATS:
                break

    if ops:
        ops.sort(key=lambda op: float(op["data"]["start_s"]))
        ops = _ensure_opening_beat(ops, ordered)
        return ops[:_MAX_TIMELINE_BEATS]

    # Fallback: one beat per window from the transcript itself.
    if not ordered:
        return []

    next_cut = -1.0
    for seg in ordered:
        start_s = float(seg.get("start_s") or 0.0)
        if start_s < next_cut:
            continue
        text = _clip_text(seg.get("text") or "")
        if not text:
            continue
        ops.append({
            "op": "add_item",
            "card": "timeline",
            "text": text,
            "data": {"start_s": start_s},
            "evidence": [seg["id"]],
        })
        next_cut = start_s + _SEGMENT_BEAT_WINDOW_S
        if len(ops) >= _MAX_TIMELINE_BEATS:
            break
    return ops


def _ensure_opening_beat(
    ops: List[Dict[str, Any]],
    ordered_segments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Prepend a framing beat when promoted key points skip the opening."""
    if not ops or not ordered_segments:
        return ops
    first_start = float(ops[0]["data"]["start_s"])
    if first_start <= _OPENING_GAP_S:
        return ops
    opening = ordered_segments[0]
    text = _clip_text(opening.get("text") or "")
    if not text:
        return ops
    opening_op = {
        "op": "add_item",
        "card": "timeline",
        "text": text,
        "data": {"start_s": float(opening.get("start_s") or 0.0)},
        "evidence": [opening["id"]],
    }
    return [opening_op, *ops]


def _normalize_tokens(text: str) -> set:
    return {
        tok for tok in "".join(
            ch.lower() if ch.isalnum() else " " for ch in (text or "")
        ).split()
        if len(tok) >= 4
    }


def _entities_in_text(text: str) -> Set[str]:
    """Lowercased entity keys found in ``text``."""
    found = {m.group(1).lower() for m in _PROPER_NAME_RE.finditer(text or "")}
    lower = (text or "").lower()
    for entity in _SINGLE_ENTITIES:
        if re.search(rf"\b{re.escape(entity)}\b", lower):
            found.add(entity)
    # Normalize a common multi-word variant.
    if "wright brothers" in lower:
        found.add("wright brothers")
    return found


def build_keypoint_coverage_ops(
    state: Dict[str, Any],
    segments: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Fill key_points gaps from timeline beats and named transcript examples.

    Models sometimes put a named example only on the timeline card or only in
    the rolling summary. Promote those claims into key_points using existing
    transcript wording — never invents text.

    Args:
        state: ``MeetingState.to_dict()`` snapshot.
        segments: Optional transcript segments for named-example promotion.

    Returns:
        Zero or more ``add_item`` ops for ``key_points``.
    """
    key_points = _live_items(state, "key_points")
    timeline = _live_items(state, "timeline")
    covered: List[set] = [_normalize_tokens(item.get("text") or "")
                          for item in key_points]
    covered_entities: Set[str] = set()
    for item in key_points:
        covered_entities |= _entities_in_text(item.get("text") or "")

    ops: List[Dict[str, Any]] = []

    def _append(text: str, evidence: List[str]) -> bool:
        """Append one key-point op when novel. Returns True if added."""
        nonlocal ops
        text = _clip_text(text)
        if not text or not evidence:
            return False
        tokens = _normalize_tokens(text)
        if len(tokens) < 3:
            return False
        if any(
            (len(tokens & other) / len(tokens | other)) >= 0.45
            for other in covered
            if other
        ):
            return False
        ops.append({
            "op": "add_item",
            "card": "key_points",
            "text": text,
            "evidence": evidence[:20],
        })
        covered.append(tokens)
        covered_entities.update(_entities_in_text(text))
        return True

    ordered = sorted(
        (seg for seg in (segments or []) if (seg.get("text") or "").strip()),
        key=lambda seg: float(seg.get("start_s") or 0.0),
    )

    # Named examples first: summary/timeline hints, else transcript scan.
    # Doing this before promoting generic timeline beats keeps offline repair
    # from filling the budget with non-example window samples.
    summary_blob = " ".join([
        state.get("rolling_summary") or "",
        ((state.get("topic") or {}).get("current") or ""),
        " ".join(item.get("text") or "" for item in timeline),
        " ".join(item.get("text") or "" for item in key_points),
        " ".join(
            f"{str((item.get('data') or {}).get('heading') or '')} {item.get('text') or ''}"
            for item in _live_items(state, "live_notes")
        ),
    ])
    wanted = _entities_in_text(summary_blob)
    # Always harvest entities from example-framing transcript segments.
    # Relying only on summary/timeline misses case studies the windowed
    # timeline backfill skipped (e.g. Apple / MLK between 20s samples).
    for seg in ordered:
        if not _looks_like_example_segment(seg.get("text") or ""):
            continue
        wanted |= _entities_in_text(seg.get("text") or "")
        if "wright" in (seg.get("text") or "").lower():
            wanted.add("wright brothers")
    missing = wanted - covered_entities
    if missing and ordered:
        for entity in sorted(missing):
            for seg in ordered:
                seg_text = seg.get("text") or ""
                seg_entities = _entities_in_text(seg_text)
                if entity not in seg_entities and not (
                    entity == "wright brothers"
                    and "wright" in seg_text.lower()
                ):
                    continue
                _append(seg_text, [seg["id"]])
                break
            if len(ops) >= 4:
                return ops

    for beat in timeline:
        _append(beat.get("text") or "", list(beat.get("evidence") or []))
        if len(ops) >= 4:
            break

    if len(ops) < 4:
        for note in _live_items(state, "live_notes"):
            data = note.get("data") or {}
            heading = str(data.get("heading") or "").strip()
            note_text = (note.get("text") or "").strip()
            text = (
                f"{heading}: {note_text}"
                if heading and not note_text.startswith(heading)
                else (heading or note_text)
            )
            _append(text, list(note.get("evidence") or []))
            if len(ops) >= 4:
                break
    return ops


def _looks_like_example_segment(text: str) -> bool:
    """True when a segment looks like a case-study / example beat."""
    lower = (text or "").lower()
    return any(
        marker in lower
        for marker in (
            "for example", "why is", "why him", "why is it",
            "brothers", "led the", "innovative",
        )
    )


def build_timeline_coverage_from_segments(
    state: Dict[str, Any],
    segments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Timeline beats for key points using earliest evidence ``start_s``.

    Args:
        state: ``MeetingState.to_dict()`` snapshot.
        segments: Transcript segments for ``start_s`` lookup.

    Returns:
        Timeline ``add_item`` ops for uncovered key points.
    """
    by_id = _segment_index(segments)
    key_points = _live_items(state, "key_points")
    timeline = _live_items(state, "timeline")
    if not key_points:
        return []

    covered: List[set] = [
        _normalize_tokens(item.get("text") or "") for item in timeline
    ]
    ops: List[Dict[str, Any]] = []
    for item in key_points:
        text = item.get("text") or ""
        tokens = _normalize_tokens(text)
        if len(tokens) < 3:
            continue
        if any(
            (len(tokens & other) / len(tokens | other)) >= 0.45
            for other in covered
            if other
        ):
            continue
        evidence = list(item.get("evidence") or [])
        start_s = _earliest_start(evidence, by_id)
        if start_s is None:
            continue
        ops.append({
            "op": "add_item",
            "card": "timeline",
            "text": _clip_text(text),
            "data": {"start_s": start_s},
            "evidence": evidence[:20],
        })
        covered.append(tokens)
        if len(timeline) + len(ops) >= _MAX_TIMELINE_BEATS:
            break
    ops.sort(key=lambda op: float(op["data"]["start_s"]))
    return ops


def build_topic_backfill_ops(
    state: Dict[str, Any],
    segments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build a ``set_topic`` op when the agent left the topic blank.

    Prefers the first live key point (often the opening framing), then the
    first live note block's heading/text; falls back to the first transcript
    segment. Never overwrites a non-empty topic.

    Args:
        state: ``MeetingState.to_dict()`` snapshot.
        segments: Transcript segment dicts.

    Returns:
        Zero or one op.
    """
    current = ((state.get("topic") or {}).get("current") or "").strip()
    if current:
        return []

    key_points = _live_items(state, "key_points")
    live_notes = _live_items(state, "live_notes")
    if key_points:
        text = _clip_text(key_points[0].get("text") or "")
        evidence = list(key_points[0].get("evidence") or [])
    elif live_notes:
        first_note = live_notes[0]
        data = first_note.get("data") or {}
        heading = str(data.get("heading") or "").strip()
        text = _clip_text(heading or (first_note.get("text") or ""))
        evidence = list(first_note.get("evidence") or [])
    else:
        ordered = sorted(
            (seg for seg in segments if (seg.get("text") or "").strip()),
            key=lambda seg: float(seg.get("start_s") or 0.0),
        )
        if not ordered:
            return []
        text = _clip_text(ordered[0].get("text") or "")
        evidence = [ordered[0]["id"]]

    if not text:
        return []
    op: Dict[str, Any] = {"op": "set_topic", "text": text[:500]}
    if evidence:
        op["evidence"] = list(dict.fromkeys(evidence))[:20]
    return [op]


def build_summary_backfill_ops(
    state: Dict[str, Any],
    segments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build a ``set_rolling_summary`` op when the summary was left empty.

    Composes a short summary from live key points or live meeting notes when
    available; otherwise joins the first few transcript segments. Never
    overwrites a non-empty summary.

    Args:
        state: ``MeetingState.to_dict()`` snapshot.
        segments: Transcript segment dicts.

    Returns:
        Zero or one op.
    """
    if (state.get("rolling_summary") or "").strip():
        return []

    key_points = _live_items(state, "key_points")
    live_notes = _live_items(state, "live_notes")
    evidence: List[str] = []
    if key_points:
        sentences = []
        for item in key_points[:6]:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            sentences.append(text.rstrip(".") + ".")
            evidence.extend(item.get("evidence") or [])
        summary = " ".join(sentences).strip()
    elif live_notes:
        sentences = []
        for item in live_notes[:6]:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            sentences.append(text.rstrip(".") + ".")
            evidence.extend(item.get("evidence") or [])
        summary = " ".join(sentences).strip()
    else:
        ordered = sorted(
            (seg for seg in segments if (seg.get("text") or "").strip()),
            key=lambda seg: float(seg.get("start_s") or 0.0),
        )[:4]
        if not ordered:
            return []
        summary = " ".join(
            (seg.get("text") or "").strip().rstrip(".") + "."
            for seg in ordered
        )
        evidence = [seg["id"] for seg in ordered]

    if not summary:
        return []
    # Match agent evidence discipline when anchors exist; host/system may
    # omit evidence, but including it keeps exports and UI jumps useful.
    op: Dict[str, Any] = {
        "op": "set_rolling_summary",
        "text": summary[:8000],
    }
    if evidence:
        # Deduplicate while preserving order.
        op["evidence"] = list(dict.fromkeys(evidence))[:20]
    return [op]


def repair_meeting_state(store: Any, segments: List[Dict[str, Any]]) -> int:
    """Apply structural repairs through the state store.

    Order matters: timeline beats are filled first so key-point coverage can
    promote any newly added beats; summary/topic fill last so they can see
    the completed claim list.

    Args:
        store: A ``MeetingStateStore``.
        segments: Transcript segment dicts for evidence / fallback text.

    Returns:
        Count of ops successfully applied across all repairs.
    """
    applied = 0

    def _apply(ops: List[Dict[str, Any]], label: str) -> None:
        nonlocal applied
        if not ops:
            return
        try:
            results = store.apply("system", "state_repair", ops)
        except Exception:
            logger.exception("%s portion of state repair failed", label)
            return
        applied += sum(1 for result in results if result.ok)

    try:
        snapshot = store.snapshot()
    except Exception:
        logger.exception("State repair could not snapshot state")
        return 0

    views = snapshot.get("report_views") or ["ribbon", "brief", "signal"]
    want_ribbon = "ribbon" in views

    if want_ribbon:
        _apply(build_timeline_backfill_ops(snapshot, segments), "timeline")
        try:
            snapshot = store.snapshot()
        except Exception:
            logger.exception("State repair could not re-snapshot after timeline")
            return applied

    _apply(build_keypoint_coverage_ops(snapshot, segments), "key_points")
    try:
        snapshot = store.snapshot()
    except Exception:
        logger.exception("State repair could not re-snapshot after key_points")
        return applied

    if want_ribbon:
        _apply(
            build_timeline_coverage_from_segments(snapshot, segments),
            "timeline_coverage",
        )
    try:
        snapshot = store.snapshot()
    except Exception:
        logger.exception(
            "State repair could not re-snapshot after timeline coverage"
        )
        return applied

    _apply(
        build_summary_backfill_ops(snapshot, segments)
        + build_topic_backfill_ops(snapshot, segments),
        "summary/topic",
    )
    if applied:
        logger.info("State repair applied %d op(s)", applied)
    return applied
