"""Headless re-run of the post-meeting finalization pipeline.

After End, audio, transcript, and dashboard state are already durable. This
module retries any later step — redecode, speaker labels, polish,
consolidation, or finalize — without a live ``MeetingEngine``. Human pins,
edits, and confirmed cards stay protected because every write goes through
``MeetingStateStore``.

No Qt imports; this package stays standalone-extractable.
"""
from __future__ import annotations

import functools
import logging
import threading
from weakref import WeakValueDictionary
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from meeting.diarize import cloud_pass
from meeting.finalization import (
    POLISH_TIMEOUT_S,
    polish_transcript,
    sparse_redecode_detail,
    STEP_DETAILS,
    STEP_NAMES,
    STEP_ORDER,
    SpeakerPassGate,
    failed_steps_message,
    insights_ready_message,
    make_step as _make_step,
    saved_state_detail,
    speaker_pass_gate,
    summary_stats,
)
from meeting.interfaces import (
    CHANNEL_LOOPBACK,
    CHANNEL_MIC,
    TranscriptSegment,
)
from meeting.reinsight import (
    DEFAULT_TIMEOUT_S,
    AgentUnavailable,
    StoreToolHost,
    stored_agent,
)
from meeting.stored import (
    open_store as _open_store,
)
from meeting.state.schema import (
    CARD_KEYS,
    CardItem,
    FinalizationState,
    MeetingState,
    parse_state_json,
)
from meeting.state.store import MeetingStateStore
from meeting.time_utils import meeting_duration_s

logger = logging.getLogger(__name__)


OPTIONAL_RERUN_STEPS = frozenset({
    "redecode", "speaker_id", "polish", "consolidation",
})
ProgressCb = Callable[[Dict[str, Any]], None]
TranscribeFn = Callable[..., Any]
#: ``(acquire, release)``. ``acquire`` frees whatever else holds a Whisper
#: model and returns True when it did; ``release`` restores it. Passing a pair
#: rather than importing the app's controller keeps ``meeting`` usable
#: standalone, and keeps the "who owns the engine" policy in the services layer.
ModelLease = Tuple[Callable[[], bool], Callable[[], None]]
#: ``rerun_redecode``'s default: build a diarizer from the speaker model.
_NEW_DIARIZER: Any = object()

__all__ = [
    "FinalizationBusyError",
    "rerun_finalization",
    "rerun_redecode",
    "rerun_polish",
    "rerun_speakers",
    "run_speaker_pass",
    "speaker_step_outcome",
    "ModelLease",
    "acquire_model_lease",
    "release_model_lease",
    "DEFAULT_TIMEOUT_S",
    "POLISH_TIMEOUT_S",
    "OPTIONAL_RERUN_STEPS",
    "STEP_ORDER",
]


def reload_store(store: MeetingStateStore, repository: Any,
                 meeting_id: str) -> None:
    """Reload the in-memory document after an out-of-band SQLite write.

    A transcript replace rewrites evidence ids in ``state_json`` directly, so
    the store must pick those up before anything else writes through it.
    """
    try:
        meeting = repository.get_meeting(meeting_id)
    except Exception:
        logger.exception("Could not reload meeting %s after a pipeline write",
                         meeting_id)
        return
    raw = (meeting or {}).get("state_json")
    data = parse_state_json(raw)
    if data is None:
        if raw:
            logger.warning("Corrupt state_json for %s after a pipeline write",
                           meeting_id)
        return
    try:
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


def strip_unevidenced_proposed(store: MeetingStateStore) -> None:
    """Drop ghost-anchored proposed cards after a transcript replace.

    The re-decode replaced every segment id; the repository remapped
    evidence onto the new transcript where an overlap match exists. Proposed
    items that kept an anchor stay, since their content is grounded in the
    meeting and consolidation reconciles it. ``live_notes`` stays whole: it
    gives the final consolidation structured context, and keeps meeting
    notes when the final report is off. Human-touched items are protected.
    """
    snapshot = store.snapshot()
    ops: List[Dict[str, Any]] = []
    cards_snapshot = snapshot.get("cards") or {}
    for key in CARD_KEYS:
        if key in ("user_notes", "live_notes"):
            continue
        for item in cards_snapshot.get(key) or []:
            if not isinstance(item, dict):
                continue
            if item.get("status") != "proposed" or CardItem.from_dict(item).protected:
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


def assign_session_speakers(
    segments: Sequence[TranscriptSegment],
    *,
    me_id: Optional[str],
    diarizer: Any,
    spool_dir: str,
    chunks: List[Dict[str, Any]],
) -> None:
    """Label re-decoded segments: Me on the mic, the diarizer on system audio.

    System audio is labeled from the whole session recording rather than
    chunk by chunk. Never raises.

    Args:
        segments: Fresh segments; labels are set in place.
        me_id: The meeting's "me" participant.
        diarizer: Labels system audio; ``None`` leaves it channel-labeled.
        spool_dir: Directory holding the session audio.
        chunks: Registered chunk rows for the concat fallback.
    """
    loopback = []
    for seg in segments:
        if seg.channel == CHANNEL_MIC and me_id:
            seg.speaker_participant_id = me_id
            seg.speaker_source = "channel"
        elif seg.channel == CHANNEL_LOOPBACK:
            loopback.append(seg)
    if diarizer is None or not loopback:
        return
    try:
        from meeting.asr.offline import load_channel_session
        from meeting.diarize.assign import assign_from_frames, refresh_labels

        frames, rate, origin = load_channel_session(
            spool_dir, CHANNEL_LOOPBACK, chunks,
        )
        if frames is None or getattr(frames, "size", 0) == 0:
            return
        labeled = assign_from_frames(diarizer, loopback, frames, rate, origin)
        refresh_labels(diarizer, labeled)
    except Exception:
        logger.exception("Offline speaker assignment failed")


def _new_offline_diarizer(
    store: MeetingStateStore, repository: Any, meeting_id: str,
) -> Any:
    """A fresh diarizer for a stored meeting, or None when unavailable."""
    try:
        from meeting.diarize.clustering import create_diarizer
        from services.components import speaker_model_path

        return create_diarizer(
            speaker_model_path(), store, repository, meeting_id,
        )
    except Exception:
        logger.exception("Could not create a diarizer for redecode retry")
        return None


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
    failure = failed_steps_message(steps)
    if failure:
        return "failed", failure
    if not cloud_enabled and not any(
        step.get("id") in {"polish", "consolidation"} for step in steps
    ):
        return "disabled", "AI insights are off for this meeting."
    return "completed", "Final insights are ready."


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
    current = store.with_state(lambda state: state.finalization)
    payload = FinalizationState(
        status=status,
        message=message,
        stage=stage,
        current_step=current_step,
        total_steps=len(steps),
        step_details=step_details,
        steps=_copy_steps(steps),
        summary_stats=dict(summary_stats or {}),
        card_deferred=bool(getattr(current, "card_deferred", False)),
    )
    logger.log(
        logging.WARNING if status == "failed" else logging.INFO,
        "Meeting re-finalization meeting_id=%s stage=%s status=%s detail=%s",
        store.with_state(lambda state: state.meeting_id), stage, status, step_details or message,
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
    duration_s = meeting_duration_s(meeting) or 0.0
    cards: Dict[str, Any] = {}
    questions: List[Any] = []
    try:
        cards, questions = store.with_state(
            lambda s: (dict(s.cards), list(s.questions))
        )
    except Exception:
        logger.exception("Could not collect card stats for %s", meeting_id)
    try:
        segments = repository.get_segments(meeting_id)
    except Exception:
        logger.exception("Could not collect transcript stats for %s", meeting_id)
        segments = []
    return summary_stats(cards, questions, segments, duration_s)


def acquire_model_lease(lease: Optional[ModelLease]) -> bool:
    """Run a lease's acquire half. Never raises; returns True when it took."""
    if not lease:
        return False
    try:
        return bool(lease[0]())
    except Exception:
        logger.exception("Model lease acquire failed; continuing without it")
        return False


def release_model_lease(lease: Optional[ModelLease]) -> None:
    """Run a lease's release half. Never raises."""
    if not lease:
        return
    try:
        lease[1]()
    except Exception:
        logger.exception("Model lease release failed")


def rerun_redecode(
    repository: Any,
    meeting_id: str,
    *,
    store: Optional[MeetingStateStore] = None,
    asr_model_name: str = "auto",
    language: Optional[str] = None,
    transcribe_fn: Optional[TranscribeFn] = None,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
    model_lease: Optional[ModelLease] = None,
    redecode_coverage_guard: bool = False,
    speaker_id_backend: str = "local",
    diarizer: Any = _NEW_DIARIZER,
) -> Dict[str, Any]:
    """Re-decode session audio and replace the stored draft transcript.

    Live End runs this with the meeting's own ASR engine as
    ``transcribe_fn``; a retry loads a model. Keeps the live draft when the
    new pass is empty. The optional coverage guard also rejects results with
    fewer than 80% of the draft's words. Human-pinned speakers and evidenced
    cards survive ``replace_final_transcript``.

    Args:
        repository: A ``MeetingRepository``.
        meeting_id: The meeting to re-decode.
        store: Optional existing store; built from ``state_json`` otherwise.
        asr_model_name: Whisper model name used when ``transcribe_fn`` is omitted.
        language: Optional ISO-639-1 language pin.
        transcribe_fn: ``(spool_dir, chunks, progress_cb=)`` decoder; the
            live meeting's ``transcribe_offline_session``, or a test fake.
        progress_cb: Optional window-progress callback.
        model_lease: Optional ``(acquire, release)`` pair invoked around the
            Whisper load. The app passes its dictation-engine lease here so
            only one model is ever resident; ``meeting`` itself stays
            independent of the services layer.
        redecode_coverage_guard: Keep the draft when the new pass is sparse.
        speaker_id_backend: ``off`` leaves system audio unlabeled, as a live
            meeting with speaker identification off does.
        diarizer: Labels system audio. By default a fresh one is built from
            the speaker model; live End passes the meeting's own, or None.

    Returns:
        ``{ok, error}``, plus the stored ``rows`` and ``removed_ids`` on
        success and ``kept_draft`` when the coverage guard refused the pass.
        Failures are reported here, not raised, except unknown-meeting
        ``ValueError``.
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
    # Released in the ``finally`` below rather than at each exit: this block
    # has several early returns, and a retained model keeps a second copy of
    # the weights resident alongside the dictation engine.
    backend = None
    leased = False
    try:
        if transcribe_fn is not None:
            decoded = list(
                transcribe_fn(spool_dir, chunks, progress_cb=progress_cb) or []
            )
        else:
            from meeting.asr.offline import transcribe_meeting_sessions
            from transcriber.local_backend import LocalWhisperBackend

            from meeting.asr.remote import MeetingRemoteBackend, saved_remote_route
            remote = saved_remote_route(meeting)
            leased = acquire_model_lease(model_lease) if remote is None else False
            from services.local_asr.catalog import MODELS
            if remote is not None:
                backend = MeetingRemoteBackend(remote)
                backend.reload_model()
                language = remote.get("language") or language
            elif asr_model_name in MODELS:
                from transcriber.optional_backend import LocalSpeechBackend
                backend = LocalSpeechBackend(MODELS[asr_model_name].backend, model_name=asr_model_name)
                backend.reload_model()
            else:
                backend = LocalWhisperBackend(model_name=asr_model_name or "auto")
            if not backend.is_available() or getattr(backend, "model", None) is None:
                missing = bool(getattr(backend, "is_model_missing", False))
                return {
                    "ok": False,
                    "error": (
                        "The Whisper model is not available yet. "
                        "Approve the download and retry."
                        if missing else
                        getattr(backend, "last_error", "") or "The speech model failed to load."
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
        logger.exception("Redecode transcription failed for %s", meeting_id)
        return {"ok": False, "error": str(exc)}
    finally:
        if backend is not None:
            try:
                backend.cleanup()
            except Exception:
                logger.exception("Error releasing the redecode Whisper model")
        if leased:
            release_model_lease(model_lease)
    if not decoded:
        return {"ok": False, "error": "Re-decoding produced no transcript."}
    try:
        existing = repository.get_segments(meeting_id)
    except Exception:
        existing = []
    new_words = _word_count(decoded)
    old_words = _word_count(existing)
    if redecode_coverage_guard and old_words and new_words < 0.8 * old_words:
        logger.warning(
            "Keeping live draft transcript for %s: offline pass has %d words "
            "vs draft %d (AMI IN1009 guard: do not replace a sparser decode)",
            meeting_id, new_words, old_words,
        )
        return {
            "ok": False,
            "error": sparse_redecode_detail(new_words, old_words),
            "kept_draft": True,
        }
    if speaker_id_backend == "off":
        diarizer = None
    elif diarizer is _NEW_DIARIZER:
        diarizer = (
            _new_offline_diarizer(store, repository, meeting_id)
            if any(seg.channel == CHANNEL_LOOPBACK for seg in decoded)
            else None
        )
    assign_session_speakers(
        decoded, me_id=_me_participant_id(store), diarizer=diarizer,
        spool_dir=spool_dir, chunks=chunks,
    )
    replace = getattr(repository, "replace_final_transcript", None)
    if not callable(replace):
        return {"ok": False, "error": "Transcript replace is unavailable."}
    try:
        rows, removed_ids, _id_map = replace(meeting_id, decoded)
    except Exception as exc:
        logger.exception("Final transcript replace failed for %s", meeting_id)
        return {"ok": False, "error": str(exc)}
    mark_done = getattr(repository, "mark_chunks_done", None)
    if callable(mark_done):
        try:
            mark_done(meeting_id)
        except Exception:
            logger.exception("Could not mark chunks done after a redecode")
    reload_store(store, repository, meeting_id)
    strip_unevidenced_proposed(store)
    # Repair ran against the draft at End; timeline coverage and summary
    # fallbacks are rebuilt from the final transcript.
    try:
        from meeting.state.repair import repair_meeting_state

        repair_meeting_state(store, rows)
    except Exception:
        logger.exception("State repair after a redecode failed")
    return {"ok": True, "error": None, "rows": rows, "removed_ids": removed_ids}


def rerun_polish(
    repository: Any,
    meeting_id: str,
    *,
    provider: str,
    model: str,
    endpoint: Optional[Dict[str, Any]] = None,
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
    tools = StoreToolHost(store, repository)
    try:
        with stored_agent(
            meeting_id, meeting, tools,
            provider=provider, model=model, endpoint=endpoint,
            agent_core_kind=agent_core_kind,
            sidecar_payload_dir=sidecar_payload_dir,
        ) as core:
            error = polish_transcript(
                core, store, segments,
                timeout_s=timeout_s, progress_cb=progress_cb,
            )
    except AgentUnavailable as exc:
        return {"ok": False, "applied": 0, "error": str(exc)}
    except Exception as exc:
        logger.exception("Polish retry failed for meeting %s", meeting_id)
        error = str(exc)
    return {"ok": error is None, "applied": tools.applied, "error": error}


def run_speaker_pass(
    repository: Any,
    meeting_id: str,
    store: MeetingStateStore,
    spool_dir: str,
    *,
    gate: SpeakerPassGate,
    transcribe_fn: Optional[TranscribeFn] = None,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
    on_start: Optional[Callable[[], None]] = None,
) -> Dict[str, Any]:
    """Upload system audio and relabel speakers when ``gate`` allows it.

    Live End, the finalization retry, and the dashboard's re-run all come
    through here. Never raises.

    Args:
        repository: A ``MeetingRepository``.
        meeting_id: The meeting to relabel.
        store: The meeting's store; built with a segment handler so relabels
            persist and broadcast.
        spool_dir: Directory holding the meeting's session audio.
        gate: From ``speaker_pass_gate``; nothing is uploaded unless ``ok``.
        transcribe_fn: Injectable decoder (tests).
        progress_cb: Optional ``cb(detail, current, total)``.
        on_start: Called once the upload is about to begin.

    Returns:
        ``{ok, skipped, applied, created, windows, error}``. ``skipped`` means
        the gate refused or OpenAI retired the model early; either way the
        on-device labels stand.
    """
    if not gate.ok:
        return {
            "ok": False, "skipped": True, "applied": 0, "created": 0,
            "windows": 0, "error": gate.reason,
        }
    if on_start is not None:
        on_start()
    try:
        result = cloud_pass.run_cloud_speaker_pass(
            repository, meeting_id, store, spool_dir,
            api_key=gate.api_key,
            transcribe_fn=transcribe_fn,
            progress_cb=progress_cb,
        )
    except Exception as exc:
        logger.exception("Cloud speaker pass raised for %s", meeting_id)
        result = {"ok": False, "error": str(exc)}
    logger.info(
        "Speaker pass for meeting %s finished: ok=%s applied=%s",
        meeting_id, result.get("ok"), result.get("applied"),
    )
    return {
        "ok": bool(result.get("ok")),
        "skipped": bool(result.get("retired")),
        "applied": int(result.get("applied") or 0),
        "created": int(result.get("created") or 0),
        "windows": int(result.get("windows") or 0),
        "error": result.get("error"),
    }


def speaker_step_outcome(result: Dict[str, Any]) -> Tuple[str, str]:
    """``(status, detail)`` for the speaker step; a skip is not a failure."""
    if result.get("ok"):
        count = int(result.get("applied") or 0)
        return "completed", (
            f"Updated {count} speaker label{'' if count == 1 else 's'}"
        )
    if result.get("skipped"):
        return "completed", (
            str(result.get("error") or "") or "Speaker identification skipped."
        )
    return "failed", (
        str(result.get("error") or "") or "Speaker identification failed."
    )


class FinalizationBusyError(RuntimeError):
    """Raised when another retry is already running for the same meeting."""


_running_lock = threading.Lock()
_running_meetings: set = set()
_latest_claims: WeakValueDictionary = WeakValueDictionary()


def is_running(meeting_id: str) -> bool:
    """Whether post-meeting steps are re-running for ``meeting_id`` now."""
    with _running_lock:
        return meeting_id in _running_meetings


class FinalizationClaim:
    """Keep one retry exclusive through caller-side cleanup and publication.

    A caller may pass this claim as ``_claim`` to a decorated pipeline, then
    release it only after publishing the outcome. Claims are not reusable
    after release and must not be shared by concurrent pipeline calls.
    """

    def __init__(self, meeting_id: str):
        self.meeting_id = meeting_id
        self._active = False
        with _running_lock:
            if meeting_id in _running_meetings:
                raise FinalizationBusyError(
                    "post-meeting steps are already running for this meeting"
                )
            _running_meetings.add(meeting_id)
            _latest_claims[meeting_id] = self
            self._active = True

    @property
    def is_active(self) -> bool:
        with _running_lock:
            return self._active

    @property
    def is_current(self) -> bool:
        """Whether no later retry has superseded this claim's queued events."""
        with _running_lock:
            return _latest_claims.get(self.meeting_id) is self

    def release(self) -> None:
        with _running_lock:
            if self._active:
                self._active = False
                _running_meetings.discard(self.meeting_id)

    def _validate(self, meeting_id: str) -> None:
        with _running_lock:
            if not self._active or meeting_id != self.meeting_id:
                raise ValueError("inactive or mismatched finalization claim")


def _one_run_per_meeting(fn: Callable[..., Dict[str, Any]]) -> Callable[..., Dict[str, Any]]:
    """Refuse concurrent desktop, dashboard, and recovery retries."""
    @functools.wraps(fn)
    def wrapper(repository: Any, meeting_id: str, *,
                _claim: Optional[FinalizationClaim] = None,
                **kwargs: Any) -> Dict[str, Any]:
        claim = _claim or FinalizationClaim(meeting_id)
        try:
            claim._validate(meeting_id)
            return fn(repository, meeting_id, **kwargs)
        finally:
            if _claim is None:
                claim.release()

    return wrapper


@_one_run_per_meeting
def rerun_speakers(
    repository: Any,
    meeting_id: str,
    *,
    gate: SpeakerPassGate,
    store: Optional[MeetingStateStore] = None,
    spool_dir: Optional[str] = None,
    transcribe_fn: Optional[TranscribeFn] = None,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> Dict[str, Any]:
    """Relabel a finished meeting's speakers from its system-audio recording.

    Someone who renames a speaker can re-run this: the new name becomes a
    reference clip for the next pass.

    Args:
        repository: A ``MeetingRepository``.
        meeting_id: The meeting to relabel.
        gate: From ``speaker_pass_gate``; nothing is uploaded unless ``ok``.
        store: Optional existing ``MeetingStateStore``.
        spool_dir: Override for the meeting's stored spool directory.
        transcribe_fn: Injectable decoder (tests).
        progress_cb: Optional progress callback.

    Returns:
        ``run_speaker_pass``'s result plus the post-pass ``state`` snapshot.

    Raises:
        ValueError: When the meeting is unknown.
        FinalizationBusyError: When post-meeting steps of this meeting are
            already running.
    """
    meeting = repository.get_meeting(meeting_id)
    if meeting is None:
        raise ValueError("unknown meeting")
    if store is None:
        store = _open_store(repository, meeting_id, meeting)
    result = run_speaker_pass(
        repository, meeting_id, store,
        spool_dir or meeting.get("spool_dir") or "",
        gate=gate, transcribe_fn=transcribe_fn, progress_cb=progress_cb,
    )
    return {**result, "state": store.snapshot()}


@_one_run_per_meeting
def rerun_finalization(
    repository: Any,
    meeting_id: str,
    *,
    from_step: str = "failed",
    provider: str,
    model: str,
    endpoint: Optional[Dict[str, Any]] = None,
    agent_core_kind: str = "pi",
    sidecar_payload_dir: Optional[str] = None,
    store: Optional[MeetingStateStore] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    asr_model_name: str = "auto",
    language: Optional[str] = None,
    transcribe_fn: Optional[TranscribeFn] = None,
    speaker_id_backend: str = "local",
    speaker_audio_consent: bool = False,
    speaker_api_key: Optional[str] = None,
    speaker_transcribe_fn: Optional[TranscribeFn] = None,
    progress_cb: Optional[ProgressCb] = None,
    model_lease: Optional[ModelLease] = None,
    redecode_coverage_guard: bool = False,
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
        speaker_id_backend: Current speaker-identification setting. ``off``
            also skips re-diarizing a redecoded transcript.
        speaker_audio_consent: Whether the user approved uploading meeting
            audio. The speaker step skips without it.
        speaker_api_key: OpenAI key for speaker identification.
        speaker_transcribe_fn: Injectable speaker decoder (tests); stands in
            for the key, never for the backend or consent.
        progress_cb: Receives each persisted finalization snapshot.

    Returns:
        ``{ok, state, applied, error, finalization}``.

    Raises:
        ValueError: When the meeting is unknown.
        FinalizationBusyError: When a retry of this meeting is already running.
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
                model_lease=model_lease,
                redecode_coverage_guard=redecode_coverage_guard,
                speaker_id_backend=speaker_id_backend,
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
            gate = speaker_pass_gate(
                backend=speaker_id_backend,
                consent=speaker_audio_consent,
                find_key=(
                    None if speaker_transcribe_fn is not None
                    else lambda: speaker_api_key
                ),
            )

            def _speaker_progress(detail: str, curr: int, total: int) -> None:
                _running(
                    "speaker_id",
                    detail,
                    f"Identifying speakers (window {curr}/{total})…",
                )

            result = run_speaker_pass(
                repository, meeting_id, store, meeting.get("spool_dir") or "",
                gate=gate,
                transcribe_fn=speaker_transcribe_fn,
                progress_cb=_speaker_progress,
                on_start=lambda: _running(
                    "speaker_id",
                    "Uploading system audio for speaker labels…",
                    "Identifying speakers…",
                ),
            )
            status, detail = speaker_step_outcome(result)
            applied += int(result.get("applied") or 0)
            if status == "failed":
                last_error = detail
            _set_step(steps, "speaker_id", status, detail)
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
                endpoint=endpoint,
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
                    endpoint=endpoint,
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
                    steps, "finalize", "completed", saved_state_detail(stats),
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
                message = insights_ready_message(stats)
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
    if not any(
        step.get("id") == "finalize" and step.get("status") == "completed"
        for step in steps
    ):
        _set_step(steps, "finalize", "completed", saved_state_detail(stats))
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
