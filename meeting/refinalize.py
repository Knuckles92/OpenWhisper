"""Headless re-run of the post-meeting finalization pipeline.

After End, audio, transcript, and dashboard state are already durable. This
module retries any later step — redecode, speaker labels, polish,
consolidation, or finalize — without a live ``MeetingEngine``. Human pins,
edits, and confirmed cards stay protected because every write goes through
``MeetingStateStore``.

No Qt imports; this package stays standalone-extractable.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from meeting.interfaces import (
    CHANNEL_MIC,
    AgentConfig,
    AgentResult,
    CheckpointPayload,
    TranscriptSegment,
)
from meeting.reinsight import (
    DEFAULT_TIMEOUT_S,
    _OfflineToolHost,
    _load_state,
)
from meeting.respeaker import rerun_speakers
from meeting.state.repair import repair_meeting_state
from meeting.state.schema import CARD_KEYS, FinalizationState, MeetingState
from meeting.state.segment_ops import make_segment_handler
from meeting.state.store import MeetingStateStore

logger = logging.getLogger(__name__)

#: Same block size the live scheduler uses for transcript cleanup.
_POLISH_MAX_SEGMENTS = 400
#: Per-block wall for a headless polish pass.
POLISH_TIMEOUT_S = 60.0

STEP_ORDER = (
    "redecode",
    "speaker_id",
    "polish",
    "consolidation",
    "finalize",
)
STEP_NAMES = {
    "redecode": "Audio Re-transcription",
    "speaker_id": "Speaker Identification",
    "polish": "Transcript Cleanup",
    "consolidation": "Summary & Action Items",
    "finalize": "State Finalization",
}
STEP_DETAILS = {
    "redecode": "High-accuracy full session Whisper decode",
    "speaker_id": "OpenAI labels on the system-audio recording",
    "polish": "AI grammar, punctuation, and speaker formatting",
    "consolidation": (
        "Synthesizing executive summary, key points, decisions, "
        "and action items"
    ),
    "finalize": "Saving final transcript and consolidating meeting state",
}
OPTIONAL_RERUN_STEPS = frozenset({
    "redecode", "speaker_id", "polish", "consolidation",
})
ProgressCb = Callable[[Dict[str, Any]], None]
TranscribeFn = Callable[..., Any]

__all__ = [
    "rerun_finalization",
    "rerun_redecode",
    "rerun_polish",
    "DEFAULT_TIMEOUT_S",
    "POLISH_TIMEOUT_S",
    "OPTIONAL_RERUN_STEPS",
    "STEP_ORDER",
]


def _open_store(repository: Any, meeting_id: str,
                meeting: Dict[str, Any]) -> MeetingStateStore:
    return MeetingStateStore(
        _load_state(meeting, meeting_id),
        repository=repository,
        segment_handler=make_segment_handler(repository, meeting_id),
        segment_exists=lambda segment_id: repository.segment_exists(
            meeting_id, segment_id
        ),
        segment_pinned=lambda segment_id: bool(
            (repository.get_segment(meeting_id, segment_id) or {}).get(
                "speaker_pinned"
            )
        ),
    )


def _reload_store(store: MeetingStateStore, repository: Any,
                  meeting_id: str) -> None:
    """Reload the in-memory document after an out-of-band SQLite write."""
    try:
        meeting = repository.get_meeting(meeting_id)
    except Exception:
        logger.exception("Could not reload meeting %s after a pipeline write",
                         meeting_id)
        return
    raw = (meeting or {}).get("state_json") or ""
    if not raw:
        return
    try:
        data = json.loads(raw)
        store.replace_document(MeetingState.from_dict(data))
    except Exception:
        logger.exception("Could not replace stored meeting state for %s",
                         meeting_id)


def _me_participant_id(store: MeetingStateStore) -> Optional[str]:
    try:
        participants = store.with_state(lambda s: dict(s.participants))
    except Exception:
        return None
    for pid, participant in participants.items():
        kind = getattr(participant, "kind", None)
        if kind is None and isinstance(participant, dict):
            kind = participant.get("kind")
        if kind == "me":
            return str(pid)
    return None


def _strip_unevidenced_proposed(store: MeetingStateStore) -> None:
    """Drop ghost-anchored proposed cards after a transcript replace."""
    snapshot = store.snapshot()
    ops: List[Dict[str, Any]] = []
    cards_snapshot = snapshot.get("cards") or {}
    for key in CARD_KEYS:
        if key in ("user_notes", "live_notes"):
            continue
        for item in cards_snapshot.get(key) or []:
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
        return
    try:
        store.apply("system", "finalization", ops)
    except Exception:
        logger.exception("Could not strip unevidenced proposed cards")


def _assign_mic_speakers(
    segments: Sequence[TranscriptSegment],
    me_id: Optional[str],
) -> None:
    if not me_id:
        return
    for seg in segments:
        if getattr(seg, "channel", None) == CHANNEL_MIC:
            seg.speaker_participant_id = me_id
            seg.speaker_source = "channel"


def _try_diarize(
    segments: List[TranscriptSegment],
    store: MeetingStateStore,
    repository: Any,
    meeting_id: str,
    spool_dir: str,
    chunks: List[Dict[str, Any]],
) -> None:
    """Best-effort loopback diarization; never raises to the caller."""
    try:
        from meeting.asr.audio import prepare_for_whisper
        from meeting.asr.offline import load_channel_session
        from meeting.diarize.clustering import create_diarizer
        from meeting.interfaces import CHANNEL_LOOPBACK
        from services.components import speaker_model_path
    except Exception:
        logger.exception("Offline speaker helpers unavailable")
        return
    try:
        diarizer = create_diarizer(
            speaker_model_path(), store, repository, meeting_id,
        )
    except Exception:
        logger.exception("Could not create a diarizer for redecode retry")
        return
    if diarizer is None:
        return
    loopback = [seg for seg in segments if seg.channel == CHANNEL_LOOPBACK]
    if not loopback:
        return
    try:
        frames, rate, origin = load_channel_session(
            spool_dir, CHANNEL_LOOPBACK, chunks,
        )
    except Exception:
        logger.exception("Could not load loopback audio for diarization")
        return
    if frames is None or getattr(frames, "size", 0) == 0:
        return
    for seg in loopback:
        start = max(0, int(round((seg.start_s - origin) * rate)))
        end = min(len(frames), int(round((seg.end_s - origin) * rate)))
        if end <= start:
            continue
        try:
            audio = prepare_for_whisper(frames[start:end], rate)
            participant_id = diarizer.assign(seg, audio, 16000)
        except Exception:
            logger.exception("Diarizer assignment failed for %s", seg.segment_id)
            continue
        if participant_id:
            seg.speaker_participant_id = participant_id
            seg.speaker_source = "diarizer"


def _word_count(rows: Sequence[Any]) -> int:
    total = 0
    for row in rows:
        if isinstance(row, TranscriptSegment):
            text = row.text
        elif isinstance(row, dict):
            text = row.get("text") or ""
        else:
            text = getattr(row, "text", "") or ""
        total += len(str(text).split())
    return total


def _copy_steps(steps: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [dict(step) for step in steps]


def _make_step(step_id: str, status: str = "pending") -> Dict[str, Any]:
    return {
        "id": step_id,
        "name": STEP_NAMES[step_id],
        "status": status,
        "detail": STEP_DETAILS[step_id],
    }


def _ensure_step(steps: List[Dict[str, Any]], step_id: str) -> List[Dict[str, Any]]:
    if any(step.get("id") == step_id for step in steps):
        return steps
    merged = _copy_steps(steps)
    merged.append(_make_step(step_id))
    order = {sid: idx for idx, sid in enumerate(STEP_ORDER)}
    merged.sort(key=lambda step: order.get(str(step.get("id")), 99))
    return merged


def _steps_from_state(
    store: MeetingStateStore,
    *,
    from_step: str,
    cloud_enabled: bool,
    have_audio: bool,
) -> List[Dict[str, Any]]:
    existing = store.with_state(lambda s: list(s.finalization.steps or []))
    steps = _copy_steps(existing)
    if not steps:
        if have_audio:
            steps.append(_make_step("redecode"))
        if from_step == "speaker_id":
            steps.append(_make_step("speaker_id"))
        if cloud_enabled:
            steps.append(_make_step("polish"))
            steps.append(_make_step("consolidation"))
        steps.append(_make_step("finalize"))
    if from_step in STEP_NAMES and from_step != "failed":
        steps = _ensure_step(steps, from_step)
    return steps


def _run_ids(steps: Sequence[Dict[str, Any]], from_step: str) -> List[str]:
    ids = [str(step.get("id")) for step in steps if step.get("id")]
    if from_step == "failed":
        start = None
        for step in steps:
            if step.get("status") in {"failed", "skipped"}:
                start = str(step.get("id"))
                break
        if start is None:
            if "consolidation" in ids:
                start = "consolidation"
            elif ids:
                start = ids[0]
            else:
                return []
        from_step = start
    if from_step not in ids:
        return [from_step] if from_step in STEP_NAMES else []
    start_idx = ids.index(from_step)
    return ids[start_idx:]


def _set_step(steps: List[Dict[str, Any]], step_id: str, status: str,
              detail: str = "") -> int:
    for idx, step in enumerate(steps, 1):
        if step.get("id") == step_id:
            step["status"] = status
            if detail:
                step["detail"] = detail
            return idx
    steps.append({
        **_make_step(step_id, status),
        "detail": detail or STEP_DETAILS.get(step_id, ""),
    })
    return len(steps)


def _overall_from_steps(
    steps: Sequence[Dict[str, Any]],
    *,
    cloud_enabled: bool,
) -> Tuple[str, str]:
    failed = [step for step in steps if step.get("status") == "failed"]
    if failed:
        names = [str(step.get("name") or step.get("id")) for step in failed]
        return (
            "failed",
            f"{', '.join(names)} failed. The recording and transcript were kept.",
        )
    if not cloud_enabled and not any(
        step.get("id") in {"polish", "consolidation"} for step in steps
    ):
        return "disabled", "Cloud intelligence is off for this meeting."
    return "completed", "Final cloud insights are ready."


def _persist_finalization(
    store: MeetingStateStore,
    *,
    status: str,
    message: str,
    steps: Sequence[Dict[str, Any]],
    stage: str = "",
    current_step: int = 0,
    step_details: str = "",
    summary_stats: Optional[Dict[str, Any]] = None,
    progress_cb: Optional[ProgressCb] = None,
) -> Dict[str, Any]:
    payload = FinalizationState(
        status=status,
        message=message,
        stage=stage,
        current_step=current_step,
        total_steps=len(steps),
        step_details=step_details,
        steps=_copy_steps(steps),
        summary_stats=dict(summary_stats or {}),
    )
    store.update_runtime_fields(finalization=payload)
    data = payload.to_dict()
    if progress_cb is not None:
        try:
            progress_cb(data)
        except Exception:
            logger.exception("Finalization progress callback failed")
    return data


def _collect_summary_stats(
    store: MeetingStateStore,
    repository: Any,
    meeting_id: str,
    meeting: Dict[str, Any],
) -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "segments": 0,
        "words": 0,
        "key_points": 0,
        "action_items": 0,
        "decisions": 0,
        "risks": 0,
        "questions": 0,
        "duration_s": 0.0,
    }
    started = meeting.get("started_at")
    ended = meeting.get("ended_at")
    if started and ended:
        try:
            start_dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(str(ended).replace("Z", "+00:00"))
            paused = float(meeting.get("paused_total_s") or 0)
            stats["duration_s"] = max(
                0.0, (end_dt - start_dt).total_seconds() - paused,
            )
        except (TypeError, ValueError):
            pass
    try:
        cards = store.with_state(lambda s: dict(s.cards))
        questions = store.with_state(lambda s: list(s.questions))
        stats["key_points"] = len(cards.get("key_points", []))
        stats["action_items"] = len(cards.get("action_items", []))
        stats["decisions"] = len(cards.get("decisions", []))
        stats["risks"] = len(cards.get("risks", []))
        stats["questions"] = len(questions)
    except Exception:
        logger.exception("Could not collect card stats for %s", meeting_id)
    try:
        segments = repository.get_segments(meeting_id)
        stats["segments"] = len(segments)
        stats["words"] = _word_count(segments)
    except Exception:
        logger.exception("Could not collect transcript stats for %s", meeting_id)
    return stats


def rerun_redecode(
    repository: Any,
    meeting_id: str,
    *,
    store: Optional[MeetingStateStore] = None,
    asr_model_name: str = "auto",
    language: Optional[str] = None,
    transcribe_fn: Optional[TranscribeFn] = None,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> Dict[str, Any]:
    """Re-decode session audio and replace the stored draft transcript.

    Keeps the live draft when the new pass is empty or has fewer than 80% of
    the draft's words. Human-pinned speakers and evidenced cards survive
    ``replace_final_transcript``.

    Args:
        repository: A ``MeetingRepository``.
        meeting_id: The meeting to re-decode.
        store: Optional existing store; built from ``state_json`` otherwise.
        asr_model_name: Whisper model name used when ``transcribe_fn`` is omitted.
        language: Optional ISO-639-1 language pin.
        transcribe_fn: Injectable decoder (tests).
        progress_cb: Optional window-progress callback.

    Returns:
        ``{ok, error}``. Failures are reported here, not raised, except
        unknown-meeting ``ValueError``.
    """
    meeting = repository.get_meeting(meeting_id)
    if meeting is None:
        raise ValueError("unknown meeting")
    if store is None:
        store = _open_store(repository, meeting_id, meeting)
    spool_dir = meeting.get("spool_dir") or ""
    try:
        chunks = list(repository.get_audio_chunks(meeting_id) or [])
    except Exception:
        logger.exception("Could not load audio chunks for redecode retry")
        chunks = []
    try:
        if transcribe_fn is not None:
            try:
                decoded = list(
                    transcribe_fn(spool_dir, chunks, progress_cb=progress_cb)
                    or []
                )
            except TypeError:
                decoded = list(transcribe_fn(spool_dir, chunks) or [])
        else:
            from meeting.asr.offline import transcribe_meeting_sessions
            from transcriber.local_backend import LocalWhisperBackend

            backend = LocalWhisperBackend(model_name=asr_model_name or "auto")
            if not backend.is_available() or getattr(backend, "model", None) is None:
                missing = bool(getattr(backend, "is_model_missing", False))
                return {
                    "ok": False,
                    "error": (
                        "The Whisper model is not available yet. "
                        "Approve the download and retry."
                        if missing else
                        "The Whisper model failed to load."
                    ),
                }
            decoded = list(transcribe_meeting_sessions(
                backend.model,
                spool_dir,
                meeting_id,
                chunks,
                language=language,
                progress_cb=progress_cb,
            ) or [])
    except Exception as exc:
        logger.exception("Redeocde transcription failed for %s", meeting_id)
        return {"ok": False, "error": str(exc)}
    if not decoded:
        return {"ok": False, "error": "Re-decoding produced no transcript."}
    try:
        existing = repository.get_segments(meeting_id)
    except Exception:
        existing = []
    new_words = _word_count(decoded)
    old_words = _word_count(existing)
    if old_words and new_words < 0.8 * old_words:
        logger.warning(
            "Keeping live draft transcript for %s: offline pass has %d words "
            "vs draft %d",
            meeting_id, new_words, old_words,
        )
        return {
            "ok": False,
            "error": "Re-decoding failed; kept live transcript",
        }
    _assign_mic_speakers(decoded, _me_participant_id(store))
    _try_diarize(decoded, store, repository, meeting_id, spool_dir, chunks)
    replace = getattr(repository, "replace_final_transcript", None)
    if not callable(replace):
        return {"ok": False, "error": "Transcript replace is unavailable."}
    try:
        replace(meeting_id, decoded)
    except Exception as exc:
        logger.exception("Final transcript replace failed for %s", meeting_id)
        return {"ok": False, "error": str(exc)}
    mark_done = getattr(repository, "mark_chunks_done", None)
    if callable(mark_done):
        try:
            mark_done(meeting_id)
        except Exception:
            logger.exception("Could not mark chunks done after redecode retry")
    _reload_store(store, repository, meeting_id)
    _strip_unevidenced_proposed(store)
    return {"ok": True, "error": None}


def _polish_blocks(segments: Sequence[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    rows = list(segments)
    if len(rows) <= _POLISH_MAX_SEGMENTS:
        return [rows] if rows else []
    step = max(1, _POLISH_MAX_SEGMENTS - 40)
    return [
        rows[start:start + _POLISH_MAX_SEGMENTS]
        for start in range(0, len(rows), step)
    ]


def _run_checkpoint(core: Any, payload: CheckpointPayload,
                    timeout_s: float) -> AgentResult:
    box: Dict[str, AgentResult] = {}

    def worker() -> None:
        try:
            box["result"] = core.checkpoint(payload)
        except Exception as exc:
            logger.exception("Agent polish raised during finalization retry")
            box["result"] = AgentResult(ok=False, error=str(exc))

    thread = threading.Thread(target=worker, name="meeting-repolish",
                              daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        logger.warning("Polish retry timed out after %.0fs; canceling", timeout_s)
        try:
            core.cancel()
        except Exception:
            logger.exception("Agent cancel raised during polish retry")
        thread.join(timeout=5.0)
        return AgentResult(ok=False, error=f"timed out after {timeout_s:.0f}s")
    return box.get("result") or AgentResult(ok=False, error="no result")


def rerun_polish(
    repository: Any,
    meeting_id: str,
    *,
    provider: str,
    model: str,
    agent_core_kind: str = "pi",
    sidecar_payload_dir: Optional[str] = None,
    store: Optional[MeetingStateStore] = None,
    timeout_s: float = POLISH_TIMEOUT_S,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> Dict[str, Any]:
    """Run a store-based transcript cleanup pass over stored segments.

    Args:
        repository: A ``MeetingRepository``.
        meeting_id: The meeting to clean up.
        provider: LLM provider id.
        model: Model id.
        agent_core_kind: ``pi`` or ``direct``.
        sidecar_payload_dir: Directory holding the Pi sidecar payload.
        store: Optional existing store.
        timeout_s: Per-block budget.
        progress_cb: Optional ``cb(detail, current, total)``.

    Returns:
        ``{ok, applied, error}``.
    """
    meeting = repository.get_meeting(meeting_id)
    if meeting is None:
        raise ValueError("unknown meeting")
    if store is None:
        store = _open_store(repository, meeting_id, meeting)
    segments = repository.get_segments(meeting_id)
    if not segments:
        return {"ok": True, "applied": 0, "error": None}
    blocks = _polish_blocks(segments)
    try:
        from meeting.agent.base import create_agent_core
        from meeting.agent.prompts import build_system_prompt

        core = create_agent_core(agent_core_kind, sidecar_payload_dir)
    except Exception as exc:
        logger.exception("Agent core unavailable for polish retry")
        return {"ok": False, "applied": 0, "error": str(exc)}
    tools = _OfflineToolHost(store, repository)
    applied_before = tools.applied
    try:
        core.initialize(
            AgentConfig(
                meeting_id=meeting_id,
                provider=provider,
                model=model,
                api_key=None,
                system_prompt=build_system_prompt(),
            ),
            tools,
        )
        last_error = ""
        applied_any = False
        total = len(blocks)
        for idx, block in enumerate(blocks, 1):
            if progress_cb is not None:
                try:
                    progress_cb(
                        f"Cleaning transcript formatting and grammar "
                        f"(block {idx}/{total}, {len(block)} segments)...",
                        idx,
                        total,
                    )
                except Exception:
                    logger.exception("Polish retry progress callback failed")
            payload = CheckpointPayload(
                request_id=uuid.uuid4().hex,
                state_snapshot=store.snapshot(),
                new_segments=block,
                is_polish=True,
            )
            result = _run_checkpoint(core, payload, timeout_s)
            if not result.ok:
                last_error = result.error or "transcript cleanup failed"
                break
            applied_any = True
        if last_error and not applied_any:
            return {"ok": False, "applied": tools.applied - applied_before,
                    "error": last_error}
        return {"ok": True, "applied": tools.applied - applied_before,
                "error": None}
    except Exception as exc:
        logger.exception("Polish retry failed for meeting %s", meeting_id)
        return {"ok": False, "applied": tools.applied - applied_before,
                "error": str(exc)}
    finally:
        try:
            core.shutdown()
        except Exception:
            logger.exception("Agent core shutdown failed after polish retry")


def rerun_finalization(
    repository: Any,
    meeting_id: str,
    *,
    from_step: str = "failed",
    provider: str,
    model: str,
    agent_core_kind: str = "pi",
    sidecar_payload_dir: Optional[str] = None,
    store: Optional[MeetingStateStore] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    asr_model_name: str = "auto",
    language: Optional[str] = None,
    transcribe_fn: Optional[TranscribeFn] = None,
    speaker_api_key: Optional[str] = None,
    speaker_transcribe_fn: Optional[TranscribeFn] = None,
    progress_cb: Optional[ProgressCb] = None,
) -> Dict[str, Any]:
    """Retry post-meeting steps from ``from_step`` through dependents.

    Args:
        repository: A ``MeetingRepository``.
        meeting_id: The meeting to resume.
        from_step: Step id to start from, or ``failed`` for the earliest
            failed/skipped step (falls back to consolidation).
        provider: LLM provider id for polish/consolidation.
        model: LLM model id.
        agent_core_kind: ``pi`` or ``direct``.
        sidecar_payload_dir: Directory holding the Pi sidecar payload.
        store: Optional existing ``MeetingStateStore``.
        timeout_s: Budget for the consolidation pass.
        asr_model_name: Whisper model used for redecode.
        language: Optional ASR language pin.
        transcribe_fn: Injectable offline decoder (tests).
        speaker_api_key: OpenAI key for speaker identification.
        speaker_transcribe_fn: Injectable speaker decoder (tests).
        progress_cb: Receives each persisted finalization snapshot.

    Returns:
        ``{ok, state, applied, error, finalization}``.

    Raises:
        ValueError: When the meeting is unknown.
    """
    meeting = repository.get_meeting(meeting_id)
    if meeting is None:
        raise ValueError("unknown meeting")
    if store is None:
        store = _open_store(repository, meeting_id, meeting)
    cloud_enabled = bool(
        store.with_state(lambda s: s.cloud_enabled)
        if store is not None else meeting.get("cloud_enabled")
    )
    have_audio = bool(meeting.get("spool_dir"))
    steps = _steps_from_state(
        store,
        from_step=from_step,
        cloud_enabled=cloud_enabled,
        have_audio=have_audio,
    )
    run_ids = _run_ids(steps, from_step)
    applied = 0
    last_error: Optional[str] = None

    def _running(step_id: str, detail: str, message: str) -> None:
        current = _set_step(steps, step_id, "running", detail)
        _persist_finalization(
            store,
            status="running",
            message=message,
            steps=steps,
            stage=step_id,
            current_step=current,
            step_details=detail,
            progress_cb=progress_cb,
        )

    for step_id in run_ids:
        if step_id == "redecode":
            _running(
                "redecode",
                "Starting high-accuracy session audio re-decoding...",
                "Re-transcribing meeting…",
            )

            def _offline_progress(detail: str, curr: int, total: int) -> None:
                _running(
                    "redecode",
                    detail,
                    f"Re-transcribing meeting (window {curr}/{total})…",
                )

            result = rerun_redecode(
                repository,
                meeting_id,
                store=store,
                asr_model_name=asr_model_name,
                language=language,
                transcribe_fn=transcribe_fn,
                progress_cb=_offline_progress,
            )
            if result.get("ok"):
                _set_step(
                    steps, "redecode", "completed",
                    "High-accuracy re-decoding complete",
                )
            else:
                last_error = result.get("error") or "Re-decoding failed"
                _set_step(
                    steps, "redecode", "failed",
                    last_error,
                )
        elif step_id == "speaker_id":
            _running(
                "speaker_id",
                "Uploading system audio for speaker labels…",
                "Identifying speakers…",
            )

            def _speaker_progress(detail: str, curr: int, total: int) -> None:
                _running(
                    "speaker_id",
                    detail,
                    f"Identifying speakers (window {curr}/{total})…",
                )

            if not speaker_api_key and speaker_transcribe_fn is None:
                last_error = "No OpenAI API key is configured."
                _set_step(steps, "speaker_id", "failed", last_error)
            else:
                try:
                    result = rerun_speakers(
                        repository,
                        meeting_id,
                        api_key=speaker_api_key or "",
                        store=store,
                        spool_dir=meeting.get("spool_dir") or "",
                        transcribe_fn=speaker_transcribe_fn,
                        progress_cb=_speaker_progress,
                    )
                except Exception as exc:
                    logger.exception("Speaker retry failed for %s", meeting_id)
                    result = {"ok": False, "error": str(exc)}
                if result.get("ok"):
                    applied += int(result.get("applied") or 0)
                    count = int(result.get("applied") or 0)
                    _set_step(
                        steps, "speaker_id", "completed",
                        f"Updated {count} speaker label"
                        f"{'' if count == 1 else 's'}",
                    )
                else:
                    last_error = (
                        result.get("error") or "Speaker identification failed."
                    )
                    _set_step(steps, "speaker_id", "failed", last_error)
        elif step_id == "polish":
            _running(
                "polish",
                "Starting AI transcript cleanup and formatting...",
                "Cleaning transcript…",
            )

            def _polish_progress(detail: str, curr: int, total: int) -> None:
                _running(
                    "polish",
                    detail,
                    f"Cleaning transcript (block {curr}/{total})…",
                )

            result = rerun_polish(
                repository,
                meeting_id,
                provider=provider,
                model=model,
                agent_core_kind=agent_core_kind,
                sidecar_payload_dir=sidecar_payload_dir,
                store=store,
                timeout_s=POLISH_TIMEOUT_S,
                progress_cb=_polish_progress,
            )
            if result.get("ok"):
                applied += int(result.get("applied") or 0)
                _set_step(
                    steps, "polish", "completed",
                    "Transcript cleanup finished",
                )
            else:
                last_error = result.get("error") or "Transcript cleanup failed"
                _set_step(steps, "polish", "failed", last_error)
        elif step_id == "consolidation":
            _running(
                "consolidation",
                "Synthesizing executive summary, key points, decisions, "
                "and action items...",
                "Preparing final report…",
            )
            from meeting.reinsight import rerun_insights

            try:
                result = rerun_insights(
                    repository,
                    meeting_id,
                    provider=provider,
                    model=model,
                    agent_core_kind=agent_core_kind,
                    sidecar_payload_dir=sidecar_payload_dir,
                    store=store,
                    timeout_s=timeout_s,
                )
            except ValueError as exc:
                result = {"ok": False, "applied": 0, "error": str(exc)}
            if result.get("ok"):
                applied += int(result.get("applied") or 0)
                _set_step(
                    steps, "consolidation", "completed",
                    "Summary & action items ready",
                )
            else:
                last_error = result.get("error") or "consolidation failed"
                _set_step(steps, "consolidation", "failed", last_error)
        elif step_id == "finalize":
            _running(
                "finalize",
                "Saving final transcript and meeting state...",
                "Finalizing meeting state…",
            )
            try:
                stats = _collect_summary_stats(
                    store, repository, meeting_id, meeting,
                )
                _set_step(
                    steps, "finalize", "completed",
                    f"Saved {stats['segments']} segments ({stats['words']} words)",
                )
            except Exception as exc:
                logger.exception("Finalize retry failed for %s", meeting_id)
                last_error = str(exc)
                _set_step(steps, "finalize", "failed", last_error)
                stats = {}
            status, message = _overall_from_steps(
                steps, cloud_enabled=cloud_enabled,
            )
            if status == "completed" and stats:
                parts = [f"{stats['segments']} segments"]
                if stats.get("key_points"):
                    parts.append(f"{stats['key_points']} key points")
                if stats.get("action_items"):
                    parts.append(f"{stats['action_items']} action items")
                if stats.get("decisions"):
                    parts.append(f"{stats['decisions']} decisions")
                message = f"Final insights ready — {', '.join(parts)}."
            finalization = _persist_finalization(
                store,
                status=status,
                message=message,
                steps=steps,
                stage="complete" if status == "completed" else status,
                current_step=len(steps),
                step_details=message,
                summary_stats=stats,
                progress_cb=progress_cb,
            )
            return {
                "ok": status != "failed",
                "state": store.snapshot(),
                "applied": applied,
                "error": last_error if status == "failed" else None,
                "finalization": finalization,
            }

    stats = _collect_summary_stats(store, repository, meeting_id, meeting)
    if not any(step.get("id") == "finalize" for step in steps):
        steps.append(_make_step("finalize", "completed"))
        _set_step(
            steps, "finalize", "completed",
            f"Saved {stats['segments']} segments ({stats['words']} words)",
        )
    elif not any(
        step.get("id") == "finalize" and step.get("status") == "completed"
        for step in steps
    ):
        _set_step(
            steps, "finalize", "completed",
            f"Saved {stats['segments']} segments ({stats['words']} words)",
        )
    status, message = _overall_from_steps(steps, cloud_enabled=cloud_enabled)
    finalization = _persist_finalization(
        store,
        status=status,
        message=message,
        steps=steps,
        stage="complete" if status == "completed" else status,
        current_step=len(steps),
        step_details=message,
        summary_stats=stats,
        progress_cb=progress_cb,
    )
    return {
        "ok": status != "failed",
        "state": store.snapshot(),
        "applied": applied,
        "error": last_error if status == "failed" else None,
        "finalization": finalization,
    }
