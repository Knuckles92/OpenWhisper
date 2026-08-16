"""Tolerant evidence-id repair for agent ops.

Segment ids are 23-hex-character strings (``sg_5f2c721de88ac0aa2321``) that
the model must copy verbatim from the prompt into tool calls. Under large
consolidation batches, flash-class models intermittently truncate or typo a
character, and exact-match validation then rejects the whole op — losing
grounded content the dashboard should have kept.

Validation in :mod:`meeting.state.patches` stays exact-match. This module
repairs obviously-intended ids *before* validation: an id is replaced only
when it does not already exist and exactly one citable id is a near-certain
match (unique prefix/containment, or a difflib similarity no other id comes
close to). Ambiguous or unmatched ids pass through untouched and keep their
honest rejection.
"""
from __future__ import annotations

import difflib
from typing import Any, Callable, Dict, Iterable, List, Sequence, Tuple

#: Minimum similarity for a fuzzy repair, and the margin the best candidate
#: must hold over the runner-up. Segment ids are high-entropy hex: a random
#: pair of ids scores far below these floors, while a model-reconstructed id
#: still lands near its intended target even with 5-6 wrong characters
#: (observed ratios ~0.74-0.78). The margin requirement is the real guard
#: against guessing when two citable ids are legitimately similar.
_FUZZY_MIN_RATIO = 0.60
_FUZZY_MARGIN = 0.10


def _repair_one(
    bad_id: str,
    citable: Sequence[str],
) -> str | None:
    """Resolve one unknown id to a unique citable id, else None."""
    if not bad_id.startswith("sg_"):
        return None
    # Unique prefix (handles truncation) or containment.
    prefix_hits = [sid for sid in citable if sid.startswith(bad_id)]
    if len(prefix_hits) == 1:
        return prefix_hits[0]
    contain_hits = [sid for sid in citable if bad_id in sid]
    if len(contain_hits) == 1:
        return contain_hits[0]
    # Fuzzy best-match with a clear margin over the runner-up.
    matches = difflib.get_close_matches(bad_id, citable, n=2, cutoff=0.5)
    if not matches:
        return None
    best = difflib.SequenceMatcher(None, bad_id, matches[0]).ratio()
    second = (
        difflib.SequenceMatcher(None, bad_id, matches[1]).ratio()
        if len(matches) > 1 else 0.0
    )
    if best >= _FUZZY_MIN_RATIO and (best - second) >= _FUZZY_MARGIN:
        return matches[0]
    return None


def repair_evidence_ids(
    ops: List[Dict[str, Any]],
    citable_ids: Iterable[str],
    exists: Callable[[str], bool],
) -> Tuple[List[Dict[str, Any]], int]:
    """Return ops with unknown evidence ids repaired where unambiguous.

    Args:
        ops: Agent op dicts (mutated copies are returned; input preserved).
        citable_ids: Segment ids the model was shown in this payload — the
            only ids a repair may target.
        exists: Exact-match predicate against stored segments; ids that
            already exist are never touched.

    Returns:
        ``(repaired_ops, repair_count)``.
    """
    citable = [sid for sid in dict.fromkeys(citable_ids)]
    cache: Dict[str, str | None] = {}
    repaired = 0
    out: List[Dict[str, Any]] = []
    for op in ops:
        if not isinstance(op, dict) or not isinstance(op.get("evidence"), list):
            out.append(op)
            continue
        evidence = op["evidence"]
        if not evidence:
            out.append(op)
            continue
        new_evidence: List[str] = []
        changed = False
        for ev in evidence:
            if isinstance(ev, str) and exists(ev):
                new_evidence.append(ev)
                continue
            if not isinstance(ev, str):
                changed = True
                continue
            if ev not in cache:
                cache[ev] = _repair_one(ev, citable)
            fixed = cache[ev]
            if fixed is not None:
                changed = True
                repaired += 1
                new_evidence.append(fixed)
            else:
                new_evidence.append(ev)
        if changed:
            op = {**op, "evidence": new_evidence}
        out.append(op)
    return out, repaired
