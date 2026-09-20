"""Deterministic post-pass repairs for meeting dashboard state.

Fill structural gaps using existing synthesized key points and notes.
Raw transcript snippets, named entities, and timeline labels are not evidence
of importance: only the agent or a human may select new key points.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Cap on derived navigation beats; repairs never select raw speech.
_MAX_TIMELINE_BEATS = 8
_MAX_BEAT_TEXT = 180


def _live_items(state: Dict[str, Any], card: str) -> List[Dict[str, Any]]:
    cards = state.get("cards") or {}
    return [
        item for item in (cards.get(card) or [])
        if item.get("status") != "removed"
    ]


def _synthesized_items(state: Dict[str, Any], card: str) -> List[Dict[str, Any]]:
    """Do not recycle unreviewed legacy transcript samples as insights."""
    return [
        item for item in _live_items(state, card)
        if not (
            item.get("author_type") == "system"
            and item.get("author_id") == "state_repair"
            and item.get("status") not in ("edited", "confirmed")
            and not item.get("pinned")
            and (item.get("data") or {}).get("insight_synthesized") is not True
        )
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
    ``data.start_s`` taken from the earliest evidence segment), then existing
    note blocks. An empty card stays empty when neither is available.

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

    for item in _synthesized_items(state, "key_points"):
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

    if not ops:
        for item in _live_items(state, "live_notes"):
            data = item.get("data") or {}
            start_s = data.get("start_s")
            evidence = [sid for sid in (item.get("evidence") or []) if sid in by_id]
            if not evidence:
                continue
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
                "evidence": evidence[:20],
            })
            if len(ops) >= _MAX_TIMELINE_BEATS:
                break

    ops.sort(key=lambda op: float(op["data"]["start_s"]))
    return ops[:_MAX_TIMELINE_BEATS]


def _normalize_tokens(text: str) -> set:
    return {
        tok for tok in "".join(
            ch.lower() if ch.isalnum() else " " for ch in (text or "")
        ).split()
        if len(tok) >= 4
    }


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
    key_points = _synthesized_items(state, "key_points")
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
    first live note block's heading/text. Never samples raw speech or
    overwrites a non-empty topic.

    Args:
        state: ``MeetingState.to_dict()`` snapshot.
        segments: Transcript segment dicts.

    Returns:
        Zero or one op.
    """
    current = ((state.get("topic") or {}).get("current") or "").strip()
    if current:
        return []

    key_points = _synthesized_items(state, "key_points")
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
        return []

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
    available. Never presents raw transcript fragments as a summary or
    overwrites a non-empty summary.

    Args:
        state: ``MeetingState.to_dict()`` snapshot.
        segments: Transcript segment dicts.

    Returns:
        Zero or one op.
    """
    if (state.get("rolling_summary") or "").strip():
        return []

    key_points = _synthesized_items(state, "key_points")
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
        return []

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

    Derive timeline navigation and missing summary/topic from synthesized
    content. Never add key points: selection and synthesis require the agent.
    Empty cards are an honest result when no substantive insight is available.

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
    # Copy topic into a blank title only after the meeting is terminal so
    # the immediate end-of-capture repair does not lock in a live draft.
    try:
        snapshot = store.snapshot()
    except Exception:
        logger.exception("State repair could not re-snapshot after topic")
        return applied
    if str(snapshot.get("status") or "") in {
        "ended", "needs_recovery", "failed",
    }:
        from meeting.content import build_untitled_title_ops

        _apply(build_untitled_title_ops(snapshot), "title")
    if applied:
        logger.info("State repair applied %d op(s)", applied)
    return applied
