"""Compare legacy vs clean end-of-meeting *packages*, not just ASR WER.

Legacy (old End): live draft transcript + sidecar consolidation.
Clean (new End): offline session re-decode + transcript polish + consolidation.

Both packages are the dashboard a participant would actually keep: topic,
summary, key points, decisions, actions, risks, timeline, questions, live
notes, and the transcript itself. An LLM judge scores them against the AMI
manual reference.

Before either End runs, :func:`simulate_live_meeting` replays the meeting the
way the product experiences it: rolling copilot checkpoints and the dedicated
note-taker pass accumulate cards and live notes over time windows. The legacy
and clean End paths then start from that same live meeting state, so the eval
scores the full product behavior (accumulate live, then reconcile at End) —
not just a consolidation built from an empty dashboard.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from openai import OpenAI

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import config  # noqa: E402
from meeting.agent.base import create_agent_core, find_provider_api_key  # noqa: E402
from meeting.agent.prompts import build_system_prompt, render_state_compact  # noqa: E402
from meeting.agent.scheduler import ConsolidationOutcome  # noqa: E402
from meeting.interfaces import (  # noqa: E402
    AgentConfig,
    AgentResult,
    CheckpointPayload,
    OpResult,
)
from meeting.state.repair import repair_meeting_state  # noqa: E402
from meeting.state.schema import CARD_KEYS, MeetingState  # noqa: E402
from meeting.state.store import MeetingStateStore  # noqa: E402

from benchmarks.meeting_mode.ami import (  # noqa: E402
    annotation_root,
    parse_reference_words,
    select_meetings,
)

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM = """\
You compare two meeting-record packages against a human reference transcript.
Score only what a participant would keep after End: the transcript they can
read, the stated topic, the summary, and the dashboard cards.

Do not reward a package for lower word-error rate unless that actually made
the record more faithful or more useful. Penalize hallucinated decisions,
actions, or claims that the reference does not support. Empty decisions/
actions is correct when the meeting made none.

Return JSON only."""

_JUDGE_RUBRIC = """\
## AMI MEETING
{meeting_id}: {description}

## HUMAN REFERENCE (what was actually said)
{reference}

## PACKAGE A — LEGACY (live draft transcript + consolidation, no offline pass)
{legacy}

## PACKAGE B — CLEAN (offline re-decode + LLM polish + consolidation)
{clean}

Score each package 1-5 on:
- transcript_usefulness: can a reader follow what was said?
- topic_accuracy: is the subject what the reference shows?
- key_points_fidelity: important claims present, grounded, not invented
- decisions_actions_precision: real decisions/actions only; empty if none
- notes_and_timeline_quality: structured minutes (live_notes) and narrative progression (timeline) faithful and clear
- overall_record: which package would you keep as the meeting record?

Return:
{{
  "legacy": {{"transcript_usefulness": n, "topic_accuracy": n,
             "key_points_fidelity": n, "decisions_actions_precision": n,
             "notes_and_timeline_quality": n, "overall_record": n}},
  "clean": {{...same keys...}},
  "winner": "legacy" | "clean" | "tie",
  "rationale": "short paragraph"
}}
"""


def _active_items(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        item for item in items
        if isinstance(item, dict) and item.get("status") != "removed"
    ]


def dashboard_package(
    snapshot: Dict[str, Any],
    segments: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Extract the durable meeting record a participant would keep."""
    cards = snapshot.get("cards") or {}
    questions = snapshot.get("questions") or []
    if isinstance(questions, dict):
        questions = list(questions.values())
    return {
        "title": snapshot.get("title") or "",
        "topic": ((snapshot.get("topic") or {}).get("current") or "").strip(),
        "summary": (snapshot.get("rolling_summary") or "").strip(),
        "cards": {
            key: [
                {
                    "text": (item.get("text") or "").strip(),
                    "data": item.get("data") or {},
                }
                for item in _active_items(cards.get(key) or [])
            ]
            for key in CARD_KEYS
            if key != "user_notes"
        },
        "questions": [
            {
                "text": (item.get("text") or "").strip(),
                "status": item.get("status"),
                "answer_text": item.get("answer_text") or "",
            }
            for item in questions
            if isinstance(item, dict)
        ],
        "transcript": [
            {
                "id": seg.get("id"),
                "start_s": seg.get("start_s"),
                "end_s": seg.get("end_s"),
                "text": (seg.get("text") or "").strip(),
            }
            for seg in segments
            if (seg.get("text") or "").strip()
        ],
        "compact": render_state_compact(snapshot),
    }


def format_reference(words: Sequence[Any], max_chars: int = 24000) -> str:
    """Turn timed AMI words into speaker-attributed prose for the judge."""
    lines: List[str] = []
    speaker = None
    buf: List[str] = []
    for word in words:
        token = (getattr(word, "text", None) or "").strip()
        if not token:
            continue
        name = getattr(word, "speaker", None) or "speaker"
        if speaker is None:
            speaker = name
        if name != speaker:
            lines.append(f"{speaker}: {' '.join(buf)}")
            speaker = name
            buf = [token]
        else:
            buf.append(token)
    if speaker and buf:
        lines.append(f"{speaker}: {' '.join(buf)}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        half = max_chars // 2
        text = text[:half] + "\n…\n" + text[-half:]
    return text


def _render_package_for_judge(package: Dict[str, Any], max_transcript_chars: int = 12000) -> str:
    cards = package.get("cards") or {}
    lines = [
        f"Topic: {package.get('topic') or '(not set)'}",
        f"Summary: {package.get('summary') or '(not set)'}",
        "",
    ]
    for key in CARD_KEYS:
        if key == "user_notes":
            continue
        items = cards.get(key) or []
        lines.append(f"{key}:")
        if not items:
            lines.append("- (empty)")
        else:
            for item in items:
                data = item.get("data") or {}
                text = (item.get("text") or "").strip()
                prefix = ""
                if key == "live_notes":
                    heading = data.get("heading")
                    start_s = data.get("start_s")
                    if heading and start_s is not None:
                        prefix = f"[{float(start_s):.0f}s] [{heading}] "
                    elif heading:
                        prefix = f"[{heading}] "
                    elif start_s is not None:
                        prefix = f"[{float(start_s):.0f}s] "
                elif key == "timeline":
                    start_s = data.get("start_s")
                    if start_s is not None:
                        prefix = f"[{float(start_s):.0f}s] "
                elif key == "action_items":
                    owner = data.get("owner_participant_id")
                    if owner:
                        prefix = f"[{owner}] "
                elif key == "risks":
                    severity = data.get("severity")
                    if severity:
                        prefix = f"[{severity}] "
                lines.append(f"- {prefix}{text}")
        lines.append("")
    questions = package.get("questions") or []
    lines.append("questions:")
    if not questions:
        lines.append("- (empty)")
    else:
        for item in questions:
            answer = item.get("answer_text") or ""
            suffix = f" → {answer}" if answer else ""
            lines.append(f"- [{item.get('status')}] {item.get('text') or ''}{suffix}")
    lines.append("")
    lines.append("transcript:")
    transcript = "\n".join(
        f"[{seg.get('start_s'):.0f}s] {seg.get('text')}"
        for seg in package.get("transcript") or []
    )
    if len(transcript) > max_transcript_chars:
        half = max_transcript_chars // 2
        transcript = transcript[:half] + "\n…\n" + transcript[-half:]
    lines.append(transcript or "(empty)")
    return "\n".join(lines)


class ProductEvalHost:
    """Minimal AgentToolHost + scheduler engine for an offline product pass."""

    def __init__(
        self,
        meeting_id: str,
        segments: Sequence[Dict[str, Any]],
        *,
        initial_state: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.meeting_id = meeting_id
        self._segments = {
            str(seg["id"]): deepcopy(seg)
            for seg in segments
            if seg.get("id")
        }
        state = MeetingState(
            meeting_id=meeting_id,
            status="ended",
            cloud_enabled=True,
            intelligence_online=True,
        )
        self.store = MeetingStateStore(
            state,
            segment_handler=self._on_segment_op,
            segment_exists=lambda sid: sid in self._segments,
        )
        if initial_state is not None:
            # Resume from a simulated live meeting (cards, notes, topic) the
            # way the engine's End path inherits the in-meeting state.
            self.store.replace_document(MeetingState.from_dict(initial_state))
        self._agent_writes_allowed = False

    def allow_agent_writes(self) -> None:
        self._agent_writes_allowed = True

    def revoke_agent_writes(self) -> None:
        self._agent_writes_allowed = False

    def agent_writes_allowed(self) -> bool:
        return self._agent_writes_allowed

    def segment_exists(self, segment_id: str) -> bool:
        """Exact-match lookup for the agent cores' evidence repair."""
        return segment_id in self._segments

    def get_transcript(
        self,
        after_start_s: float = -1.0,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        rows = sorted(
            self._segments.values(),
            key=lambda row: (float(row.get("start_s") or 0.0), str(row.get("id") or "")),
        )
        if after_start_s >= 0:
            rows = [row for row in rows if float(row.get("start_s") or 0.0) > after_start_s]
        if limit is not None:
            rows = rows[:limit]
        return [deepcopy(row) for row in rows]

    def apply_agent_ops(self, ops: List[Dict[str, Any]]) -> List[OpResult]:
        if not self._agent_writes_allowed:
            return [
                OpResult(
                    ok=False,
                    op=op if isinstance(op, dict) else {"op": op},
                    reason="agent_writes_revoked",
                )
                for op in ops
            ]
        return self.store.apply("agent", "agent", list(ops))

    def ask_question(self, text: str, evidence: List[str]) -> OpResult:
        return self._apply_single({
            "op": "ask_question", "text": text, "evidence": list(evidence or []),
        })

    def resolve_question(
        self,
        question_id: str,
        answer_text: str,
        confidence: float,
        evidence: List[str],
    ) -> OpResult:
        return self._apply_single({
            "op": "resolve_question",
            "question_id": question_id,
            "answer_text": answer_text,
            "confidence": confidence,
            "evidence": list(evidence or []),
        })

    def _apply_single(self, op: Dict[str, Any]) -> OpResult:
        if not self._agent_writes_allowed:
            return OpResult(ok=False, op=op, reason="agent_writes_revoked")
        return self.store.apply("agent", "agent", [op])[0]

    def _on_segment_op(self, result: OpResult) -> Optional[Dict[str, Any]]:
        effect = result.effect or {}
        segment_id = str(effect.get("segment_id") or "")
        prior = self._segments.get(segment_id)
        if prior is None:
            return None
        if (result.op or {}).get("op") != "revise_segment_text":
            return None
        old_text = prior.get("text") or ""
        prior["text"] = effect.get("text") or ""
        return {
            "op": "revise_segment_text",
            "segment_id": segment_id,
            "text": old_text,
            "evidence": [segment_id],
        }


def build_live_windows(
    segments: Sequence[Dict[str, Any]],
    window_s: float,
) -> List[List[Dict[str, Any]]]:
    """Group sorted segments into fixed meeting-time windows.

    Mirrors the scheduler's rolling cadence at benchmark scale: each window is
    one checkpoint's worth of "new transcript segments".
    """
    if window_s <= 0:
        return [list(segments)]
    windows: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_index = -1
    for seg in sorted(
        segments,
        key=lambda row: float(row.get("start_s") or 0.0),
    ):
        index = int(float(seg.get("start_s") or 0.0) // window_s)
        if index != current_index:
            if current:
                windows.append(current)
            current = [seg]
            current_index = index
        else:
            current.append(seg)
    if current:
        windows.append(current)
    return windows


def simulate_live_meeting(
    meeting_id: str,
    segments: Sequence[Dict[str, Any]],
    *,
    provider: str,
    model: str,
    api_key: str,
    window_s: float = 120.0,
    notes_every_n_checkpoints: int = 2,
    checkpoint_timeout_s: float = 180.0,
) -> Dict[str, Any]:
    """Replay a meeting live: rolling checkpoints + note-taker passes.

    Follows the product cadence at benchmark scale — a copilot checkpoint per
    time window, then a dedicated note-taker pass (``is_notes`` persona) after
    every ``notes_every_n_checkpoints`` successful checkpoints, with the notes
    pass carrying every segment not yet covered by a successful notes pass.

    Returns:
        ``{"state": final live state snapshot, "stats": pass counters}``.
    """
    from meeting.agent import openrouter_direct as direct_mod

    direct_mod._CHECKPOINT_TIMEOUT_S = max(
        float(direct_mod._CHECKPOINT_TIMEOUT_S), checkpoint_timeout_s,
    )
    host = ProductEvalHost(meeting_id, segments)
    host.store.replace_document(MeetingState.from_dict({
        "meeting_id": meeting_id,
        "status": "active",
        "cloud_enabled": True,
        "intelligence_online": True,
    }))
    agent = create_agent_core("direct")
    agent.initialize(
        AgentConfig(
            meeting_id=meeting_id,
            provider=provider,
            model=model,
            api_key=api_key,
            system_prompt=build_system_prompt(),
        ),
        host,
    )
    if not agent.is_healthy():
        raise RuntimeError("Meeting intelligence agent is offline (missing API key?)")

    stats = {
        "windows": 0,
        "checkpoints_ok": 0,
        "checkpoints_failed": 0,
        "notes_passes_ok": 0,
        "notes_passes_failed": 0,
        "ops_applied": 0,
        "notes_ops_applied": 0,
    }
    successful_checkpoints = 0
    notes_checkpoint_mark = 0
    pending: List[Dict[str, Any]] = []
    notes_pending: List[Dict[str, Any]] = []

    def _notes_due() -> bool:
        return (
            successful_checkpoints - notes_checkpoint_mark
            >= notes_every_n_checkpoints
        )

    def _run_notes_pass() -> None:
        nonlocal notes_pending, notes_checkpoint_mark
        if not notes_pending:
            return
        payload = CheckpointPayload(
            request_id=uuid.uuid4().hex,
            state_snapshot=host.store.snapshot(),
            new_segments=list(notes_pending),
            is_notes=True,
        )
        try:
            result = agent.checkpoint(payload)
        except Exception as exc:
            logger.exception("Notes pass raised")
            result = AgentResult(ok=False, error=str(exc))
        # Cadence bookkeeping advances on both outcomes (product behavior);
        # only success marks the batch consumed.
        notes_checkpoint_mark = successful_checkpoints
        if result.ok:
            stats["notes_passes_ok"] += 1
            stats["notes_ops_applied"] += sum(
                1 for r in result.op_results if r.ok
            )
            notes_pending = []
        else:
            stats["notes_passes_failed"] += 1
            logger.warning(
                "Notes pass failed: %s", result.error or "unknown",
            )

    host.allow_agent_writes()
    try:
        for window in build_live_windows(segments, window_s):
            stats["windows"] += 1
            pending.extend(window)
            notes_pending.extend(window)
            payload = CheckpointPayload(
                request_id=uuid.uuid4().hex,
                state_snapshot=host.store.snapshot(),
                new_segments=list(pending),
            )
            try:
                result = agent.checkpoint(payload)
            except Exception as exc:
                logger.exception("Live checkpoint raised")
                result = AgentResult(ok=False, error=str(exc))
            if result.ok:
                stats["checkpoints_ok"] += 1
                stats["ops_applied"] += sum(1 for r in result.op_results if r.ok)
                pending = []
                successful_checkpoints += 1
                if _notes_due():
                    _run_notes_pass()
            else:
                # Product behavior: a failed checkpoint's segments stay
                # pending and rejoin the next fire.
                stats["checkpoints_failed"] += 1
                logger.warning(
                    "Live checkpoint failed (retrying with next window): %s",
                    result.error or "unknown",
                )
        if pending or notes_pending:
            logger.warning(
                "Live phase ended with undelivered segments "
                "(cards=%d, notes=%d)", len(pending), len(notes_pending),
            )
    finally:
        host.revoke_agent_writes()
        try:
            agent.shutdown()
        except Exception:
            logger.debug("Agent shutdown failed", exc_info=True)
    snapshot = host.store.snapshot()
    cards = snapshot.get("cards") or {}
    stats["live_notes_blocks"] = len(
        [i for i in (cards.get("live_notes") or [])
         if isinstance(i, dict) and i.get("status") != "removed"],
    )
    logger.info(
        "Live phase done: %s topic=%r notes_blocks=%d",
        meeting_id,
        ((snapshot.get("topic") or {}).get("current") or "")[:60],
        stats["live_notes_blocks"],
    )
    return {"state": snapshot, "stats": stats}


def build_redecode_id_map(
    old_segments: Sequence[Dict[str, Any]],
    new_segments: Sequence[Dict[str, Any]],
) -> Dict[str, str]:
    """Greedy IoU ``old_id -> new_id`` map, like the repository's replace."""
    from meeting.asr.revise import MIN_MATCH_IOU, interval_iou

    pairs: List[Tuple[float, str, str]] = []
    for old in old_segments:
        for new in new_segments:
            if old.get("channel") != new.get("channel"):
                continue
            score = interval_iou(
                float(old.get("start_s") or 0.0), float(old.get("end_s") or 0.0),
                float(new.get("start_s") or 0.0), float(new.get("end_s") or 0.0),
            )
            if score >= MIN_MATCH_IOU:
                pairs.append((score, str(old["id"]), str(new["id"])))
    pairs.sort(reverse=True)
    id_map: Dict[str, str] = {}
    used_new: set = set()
    for _score, old_id, new_id in pairs:
        if old_id in id_map or new_id in used_new:
            continue
        id_map[old_id] = new_id
        used_new.add(new_id)
    return id_map


def remap_state_evidence(
    state: Dict[str, Any],
    id_map: Dict[str, str],
) -> None:
    """Rewrite card/question evidence ids through the re-decode map."""
    cards = state.get("cards") or {}
    for items in (cards.values() if isinstance(cards, dict) else []):
        for item in items or []:
            if isinstance(item, dict):
                item["evidence"] = [
                    id_map[e] for e in (item.get("evidence") or [])
                    if e in id_map
                ]
    for question in state.get("questions") or []:
        if isinstance(question, dict):
            question["evidence"] = [
                id_map[e] for e in (question.get("evidence") or [])
                if e in id_map
            ]


def strip_proposed_after_redecode(
    host: ProductEvalHost,
    old_segments: Optional[Sequence[Dict[str, Any]]] = None,
    new_segments: Optional[Sequence[Dict[str, Any]]] = None,
) -> int:
    """Remove agent-proposed cards after a transcript re-decode.

    Mirrors ``MeetingEngine._run_offline_final_pass`` plus the repository's
    evidence remap: surviving evidence anchors are remapped onto the new
    transcript first, then only proposed items whose anchors all died are
    removed. ``live_notes`` is deliberately kept — consolidation reconciles
    it against the new transcript.
    """
    snapshot = host.store.snapshot()
    state: Dict[str, Any] = snapshot
    if old_segments is not None and new_segments is not None:
        remap_state_evidence(state, build_redecode_id_map(old_segments, new_segments))
        host.store.replace_document(MeetingState.from_dict(state))
        snapshot = state
    ops: List[Dict[str, Any]] = []
    cards = snapshot.get("cards") or {}
    for key in CARD_KEYS:
        if key in ("user_notes", "live_notes"):
            continue
        for item in cards.get(key) or []:
            if not isinstance(item, dict):
                continue
            if item.get("status") != "proposed" or item.get("pinned"):
                continue
            if item.get("evidence"):
                continue
            ops.append({
                "op": "remove_item",
                "id": item.get("id"),
                "base_revision": item.get("revision", 1),
            })
    if not ops:
        return 0
    results = host.store.apply("system", "finalization", ops)
    return sum(1 for r in results if r.ok)


def run_product_pipeline(
    meeting_id: str,
    segments: Sequence[Dict[str, Any]],
    *,
    polish: bool,
    provider: str,
    model: str,
    api_key: str,
    polish_timeout_s: float = 180.0,
    consolidation_timeout_s: float = 360.0,
    initial_state: Optional[Dict[str, Any]] = None,
    strip_proposed: bool = False,
    old_segments: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run polish (optional) then consolidation on a frozen transcript.

    Calls the agent directly so a scheduler watchdog cannot cancel a slow but
    successful report mid-flight. When ``initial_state`` is given the pipeline
    starts from a simulated live meeting; ``strip_proposed`` mirrors the
    engine's post-re-decode evidence remap + card strip for the clean End
    path (``old_segments`` supplies the pre-re-decode evidence ids).
    """
    from meeting.agent import openrouter_direct as direct_mod

    direct_mod._CHECKPOINT_TIMEOUT_S = max(
        float(direct_mod._CHECKPOINT_TIMEOUT_S), polish_timeout_s,
    )
    direct_mod._CONSOLIDATION_TIMEOUT_S = max(
        float(direct_mod._CONSOLIDATION_TIMEOUT_S), consolidation_timeout_s,
    )
    host = ProductEvalHost(meeting_id, segments, initial_state=initial_state)
    stripped = (
        strip_proposed_after_redecode(
            host, old_segments=old_segments, new_segments=segments,
        )
        if strip_proposed
        else 0
    )
    if stripped:
        logger.info("Post-redecode strip removed %d proposed item(s)", stripped)
    agent = create_agent_core("direct")
    agent.initialize(
        AgentConfig(
            meeting_id=meeting_id,
            provider=provider,
            model=model,
            api_key=api_key,
            system_prompt=build_system_prompt(),
        ),
        host,
    )
    if not agent.is_healthy():
        raise RuntimeError("Meeting intelligence agent is offline (missing API key?)")
    host.allow_agent_writes()
    polish_outcome = None
    op_stats: Dict[str, Dict[str, Dict[str, int]]] = {}
    try:
        if polish:
            polish_outcome, op_stats["polish"] = _run_polish(agent, host)
        consolidation, op_stats["consolidation"] = _run_consolidation(agent, host)
        try:
            repair_meeting_state(host.store, host.get_transcript())
        except Exception:
            logger.exception("State repair after consolidation failed")
    finally:
        host.revoke_agent_writes()
        try:
            agent.shutdown()
        except Exception:
            logger.debug("Agent shutdown failed", exc_info=True)
    package = dashboard_package(host.store.snapshot(), host.get_transcript())
    package["polish"] = (
        polish_outcome.to_dict() if polish_outcome is not None else None
    )
    package["consolidation"] = consolidation.to_dict()
    package["op_stats"] = op_stats
    return package


def _payload(host: ProductEvalHost, segments: List[Dict[str, Any]], *,
             is_consolidation: bool, is_polish: bool) -> CheckpointPayload:
    return CheckpointPayload(
        request_id=uuid.uuid4().hex,
        state_snapshot=host.store.snapshot(),
        new_segments=segments,
        is_consolidation=is_consolidation,
        is_polish=is_polish,
    )


def _op_stats(op_results: Sequence[Any]) -> Dict[str, Any]:
    """Applied/rejected op counts by op type and rejection reason."""
    stats: Dict[str, Any] = {"applied": {}, "rejected": {}}
    for item in op_results:
        op = (getattr(item, "op", None) or {}).get("op") or "?"
        bucket = stats["applied"] if getattr(item, "ok", False) else stats["rejected"]
        if not getattr(item, "ok", False):
            reason = getattr(item, "reason", None) or "?"
            op = f"{op}:{reason}"
        bucket[op] = bucket.get(op, 0) + 1
    rejected_samples = [
        {
            "op": getattr(item, "op", None),
            "reason": getattr(item, "reason", None),
        }
        for item in op_results
        if not getattr(item, "ok", False)
    ][:40]
    if rejected_samples:
        stats["rejected_samples"] = rejected_samples  # type: ignore[assignment]
    return stats


def _run_polish(
    agent: Any, host: ProductEvalHost,
) -> Tuple[ConsolidationOutcome, Dict[str, Dict[str, int]]]:
    segments = host.get_transcript()
    if not segments:
        return ConsolidationOutcome(
            status="completed", message="No transcript text needed cleanup.",
        ), _op_stats([])
    max_segments = 400
    if len(segments) > max_segments:
        step = max(1, max_segments - 40)
        blocks = [
            segments[start:start + max_segments]
            for start in range(0, len(segments), step)
        ]
    else:
        blocks = [segments]
    last_error = ""
    applied = False
    all_results: List[Any] = []
    for block in blocks:
        result = agent.checkpoint(
            _payload(host, block, is_consolidation=False, is_polish=True)
        )
        all_results.extend(result.op_results)
        if result.ok:
            applied = True
        else:
            last_error = result.error or "polish failed"
    if last_error and not applied:
        return (
            ConsolidationOutcome(status="failed", message=last_error),
            _op_stats(all_results),
        )
    return (
        ConsolidationOutcome(
            status="completed",
            message="Transcript cleanup finished.",
        ),
        _op_stats(all_results),
    )


def _run_consolidation(
    agent: Any, host: ProductEvalHost,
) -> Tuple[ConsolidationOutcome, Dict[str, Dict[str, int]]]:
    segments = host.get_transcript()
    result = agent.consolidate(
        _payload(host, segments, is_consolidation=True, is_polish=False)
    )
    if result is None:
        return ConsolidationOutcome(
            status="failed", message="Final insights produced no result.",
        ), _op_stats([])
    stats = _op_stats(result.op_results)
    if result.ok:
        applied = sum(1 for item in result.op_results if item.ok)
        logger.info(
            "Consolidation done: %d/%d ops applied",
            applied, len(result.op_results),
        )
        return ConsolidationOutcome(
            status="completed",
            message="Final cloud insights are ready.",
        ), stats
    return ConsolidationOutcome(
        status="failed",
        message=f"Final cloud insights failed: {result.error or 'consolidation failed'}",
    ), stats


def _load_result(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _judge_client(provider: str, api_key: str) -> OpenAI:
    if provider == "openrouter":
        return OpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            default_headers={"X-Title": "OpenWhisper product eval"},
        )
    return OpenAI(api_key=api_key)


def judge_packages(
    *,
    meeting_id: str,
    description: str,
    reference: str,
    legacy: Dict[str, Any],
    clean: Dict[str, Any],
    provider: str,
    model: str,
    api_key: str,
) -> Dict[str, Any]:
    """LLM-as-judge over the two durable meeting records vs AMI reference."""
    client = _judge_client(provider, api_key)
    user = _JUDGE_RUBRIC.format(
        meeting_id=meeting_id,
        description=description,
        reference=reference,
        legacy=_render_package_for_judge(legacy),
        clean=_render_package_for_judge(clean),
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    content = (response.choices[0].message.content or "").strip()
    try:
        judgment = json.loads(content)
    except json.JSONDecodeError:
        return {"winner": "tie", "rationale": content, "parse_error": True}
    return _normalize_judgment(judgment)


def _normalize_judgment(judgment: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce common judge-schema deviations onto the expected shape."""
    for side in ("legacy", "clean"):
        scores = judgment.get(side)
        if isinstance(scores, dict):
            if "overall_record" not in scores and "overall_record_quality" in scores:
                scores["overall_record"] = scores["overall_record_quality"]
    if not any(
        isinstance(judgment.get(side), dict)
        for side in ("legacy", "clean")
    ):
        judgment.setdefault("parse_error", True)
    return judgment


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=(
            PROJECT_ROOT / "benchmarks" / "meeting_mode" / "results"
            / "auto-auto-draft-only-t5-m20-p50-offline"
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=PROJECT_ROOT / "benchmarks" / "meeting_mode" / "data" / "ami",
    )
    parser.add_argument(
        "--meetings",
        default="IN1009,IN1005,IN1007",
        help="Comma-separated ids; default is a short/best/worst slice",
    )
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--polish-timeout", type=float, default=180.0)
    parser.add_argument("--consolidation-timeout", type=float, default=360.0)
    parser.add_argument(
        "--live-window-s",
        type=float,
        default=120.0,
        help=(
            "Meeting-seconds per simulated live checkpoint "
            "(0 = one checkpoint over the whole meeting)"
        ),
    )
    parser.add_argument(
        "--no-live",
        action="store_true",
        help=(
            "Skip the live phase; consolidation starts from an empty "
            "dashboard (pre-notes-feature ablation)"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the product-package comparison and return a process exit code."""
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    provider = config.MEETING_LLM_PROVIDER
    model = "deepseek/deepseek-v4-flash-0731"
    api_key = find_provider_api_key(provider)
    if not api_key:
        print(f"No {provider} API key; cannot run the product judge.", file=sys.stderr)
        return 2

    ids = [item.strip() for item in args.meetings.split(",") if item.strip()]
    meetings = list(select_meetings(ids))
    annotations_dir = annotation_root(args.data_dir)
    out_dir = args.out_dir or (args.results_dir / "product_eval")
    out_dir.mkdir(parents=True, exist_ok=True)

    summary: Dict[str, Any] = {
        "provider": provider,
        "model": model,
        "meetings": [],
        "wins": {"legacy": 0, "clean": 0, "tie": 0},
    }
    for spec in meetings:
        result_path = args.results_dir / f"{spec.meeting_id}.json"
        if not result_path.exists():
            print(f"Missing ASR result {result_path}", file=sys.stderr)
            return 2
        raw = _load_result(result_path)
        draft = raw.get("draft_segments") or []
        offline = raw.get("offline_segments") or draft
        live = None
        if not args.no_live:
            print(
                f"{spec.meeting_id}: simulating live meeting over "
                f"{len(draft)} draft segments "
                f"(window={args.live_window_s:.0f}s)"
            )
            live = simulate_live_meeting(
                spec.meeting_id, draft,
                provider=provider, model=model, api_key=api_key,
                window_s=args.live_window_s,
                checkpoint_timeout_s=args.polish_timeout,
            )
        initial_state = live["state"] if live else None
        print(f"{spec.meeting_id}: legacy consolidation on {len(draft)} draft segments")
        legacy = run_product_pipeline(
            spec.meeting_id, draft, polish=False,
            provider=provider, model=model, api_key=api_key,
            polish_timeout_s=args.polish_timeout,
            consolidation_timeout_s=args.consolidation_timeout,
            initial_state=initial_state,
        )
        print(f"{spec.meeting_id}: clean polish+report on {len(offline)} offline segments")
        clean = run_product_pipeline(
            spec.meeting_id, offline, polish=True,
            provider=provider, model=model, api_key=api_key,
            polish_timeout_s=args.polish_timeout,
            consolidation_timeout_s=args.consolidation_timeout,
            initial_state=initial_state,
            strip_proposed=True,
            old_segments=draft,
        )
        if live is not None:
            for package in (legacy, clean):
                package["live"] = live["stats"]
        reference = format_reference(parse_reference_words(annotations_dir, spec.meeting_id))
        print(f"{spec.meeting_id}: judging packages against AMI reference")
        judgment = judge_packages(
            meeting_id=spec.meeting_id,
            description=spec.description,
            reference=reference,
            legacy=legacy,
            clean=clean,
            provider=provider,
            model=model,
            api_key=api_key,
        )
        winner = str(judgment.get("winner") or "tie")
        if winner not in summary["wins"]:
            winner = "tie"
        summary["wins"][winner] += 1
        meeting_row = {
            "meeting_id": spec.meeting_id,
            "description": spec.description,
            "winner": winner,
            "judgment": judgment,
            "legacy": legacy,
            "clean": clean,
        }
        summary["meetings"].append({
            "meeting_id": spec.meeting_id,
            "winner": winner,
            "judgment": judgment,
            "legacy_topic": legacy.get("topic"),
            "clean_topic": clean.get("topic"),
        })
        dest = out_dir / f"{spec.meeting_id}.json"
        dest.write_text(json.dumps(meeting_row, indent=2, ensure_ascii=False), encoding="utf-8")
        print(
            f"  winner={winner} "
            f"legacy_topic={legacy.get('topic')!r} "
            f"clean_topic={clean.get('topic')!r}"
        )
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(summary["wins"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
