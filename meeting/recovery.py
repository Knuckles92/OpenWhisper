"""Crash recovery for Meeting Mode.

A crash leaves a meeting row in ``active``/``paused`` with a stale
pid/heartbeat and its spooled chunks still ``pending``. On startup the app
scans for such sessions and offers to finalize them (headless: transcribe the
remaining chunks and mark the meeting ended), resume them (a fresh
``MeetingEngine`` built from ``build_resume_options``), or discard them.

No Qt imports; the sibling ASR import is lazy so partial availability never
breaks importing this module.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from meeting.interfaces import SpooledChunk
from meeting.state.schema import FinalizationState, now_iso

logger = logging.getLogger(__name__)

#: A live session must heartbeat at least this often to be considered alive.
STALE_HEARTBEAT_S = 60.0
#: ASR drain budget for headless finalization.
FINALIZE_DRAIN_TIMEOUT_S = 600.0

#: Windows process-liveness constants.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def _pid_alive(pid: Optional[int]) -> bool:
    """Best-effort, psutil-free check whether ``pid`` is a running process.

    On Windows ``os.kill(pid, 0)`` would *terminate* a live target process
    (any signal other than the CTRL events maps to ``TerminateProcess``), so
    a read-only ``OpenProcess`` probe is used there instead; ``os.kill`` with
    signal 0 is safe on POSIX.

    Args:
        pid: The process id recorded on the meeting row.

    Returns:
        True when the process appears to be running.
    """
    if not pid or int(pid) <= 0:
        return False
    pid = int(pid)
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            # Access denied means the process exists but is protected.
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def is_session_dead(meeting: Dict[str, Any]) -> bool:
    """Whether an interrupted meeting's owning app session is dead.

    A session counts as dead when its heartbeat is missing, unparseable, or
    older than ``STALE_HEARTBEAT_S`` — or when the heartbeat looks fresh but
    the recorded pid is not running (pid reuse is covered by the heartbeat
    check).

    Args:
        meeting: A meeting dict from ``repository.find_interrupted_meetings``.

    Returns:
        True when the session is safe to recover.
    """
    heartbeat = meeting.get("app_heartbeat_at")
    if heartbeat:
        try:
            age = (datetime.now() - datetime.fromisoformat(heartbeat)).total_seconds()
        except (TypeError, ValueError):
            age = None
        if age is not None and age < STALE_HEARTBEAT_S:
            return not _pid_alive(meeting.get("app_pid"))
    return True


def find_recoverable_meetings(repository: Any) -> List[Dict[str, Any]]:
    """Interrupted meetings whose owning app session is dead.

    Args:
        repository: A ``MeetingRepository``.

    Returns:
        Meeting dicts eligible for finalize/resume/discard, or an empty list
        when the scan fails.
    """
    try:
        candidates = repository.find_interrupted_meetings()
    except Exception:
        logger.exception("Failed to scan for interrupted meetings")
        return []
    recoverable = [m for m in candidates if is_session_dead(m)]
    if recoverable:
        logger.info("Found %d recoverable meeting(s): %s",
                    len(recoverable), [m.get("id") for m in recoverable])
    return recoverable


def finalize_meeting(repository: Any, meeting: Dict[str, Any],
                     asr_model: str = "auto",
                     asr_language: str = "auto",
                     on_progress: Optional[Callable[[int, int], None]] = None) -> bool:
    """Headless finalization: transcribe remaining chunks and mark ended.

    Rebuilds a minimal pipeline — ASR engine only, no capture, no web server,
    no agent — feeds it the meeting's pending chunks, waits for the queue to
    drain, and marks the meeting ``ended``. Segments are persisted through
    an atomic segment/chunk commit as they arrive, so a crash mid-finalize
    remains recoverable without duplicating evidence anchors.

    Args:
        repository: A ``MeetingRepository``.
        meeting: The meeting dict to finalize.
        asr_model: Fallback model name when the meeting row records none.
        asr_language: Spoken-language preference (``auto`` or ISO-639-1).
        on_progress: Optional ``cb(done_chunks, total_chunks)`` progress hint,
            invoked as chunk transcriptions complete.

    Returns:
        True when all pending chunks were processed and the meeting was
        marked ended; False on ASR unavailability, pipeline failure, or
        drain timeout (the meeting stays recoverable).
    """
    meeting_id = meeting.get("id")
    if not meeting_id:
        return False
    model_name = meeting.get("asr_model") or asr_model or "auto"

    try:
        repository.reset_unfinished_chunks(meeting_id)
        pending = repository.get_pending_chunks(meeting_id)
    except Exception:
        logger.exception("Failed to list pending chunks for %s", meeting_id)
        return False
    total = len(pending)
    progress = {"done": 0}

    def _report() -> None:
        if on_progress is None:
            return
        try:
            on_progress(progress["done"], total)
        except Exception:
            logger.exception("Finalize progress callback raised")

    def _on_chunk_result(chunk, segments) -> None:
        repository.commit_chunk_transcription(
            meeting_id, chunk.chunk_id, segments
        )
        progress["done"] += 1
        _report()

    drained = True
    engine = None
    try:
        if total:
            from meeting.asr.engine import MeetingAsrEngine
            language = (asr_language or "auto").strip().lower()
            engine = MeetingAsrEngine(
                model_name,
                meeting_id,
                repository,
                language=None if language == "auto" else language,
            )
            if not getattr(engine, "is_available", False):
                logger.error("ASR model %r unavailable; cannot finalize %s",
                             model_name, meeting_id)
                return False
            engine.start(_on_chunk_result)
            requeue = getattr(engine, "requeue_pending", None)
            if callable(requeue):
                requeue()
            else:
                for chunk in pending:
                    engine.enqueue(SpooledChunk(
                        chunk_id=chunk["id"],
                        meeting_id=chunk["meeting_id"],
                        channel=chunk["channel"],
                        seq=chunk["seq"],
                        file_path=chunk["file_path"],
                        start_s=chunk["start_s"],
                        duration_s=chunk["duration_s"],
                        sample_rate=chunk["sample_rate"],
                    ))
            _report()
            drained = engine.drain(FINALIZE_DRAIN_TIMEOUT_S)
    except Exception:
        logger.exception("Finalize pipeline failed for meeting %s", meeting_id)
        return False
    finally:
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                logger.exception("ASR stop failed after finalize of %s",
                                 meeting_id)
    if not drained:
        logger.error("ASR drain timed out finalizing meeting %s", meeting_id)
        return False
    if repository.count_unfinished_chunks(meeting_id):
        logger.error("Unfinished chunks remain after finalizing %s", meeting_id)
        repository.update_meeting(meeting_id, status="needs_recovery")
        return False

    _mark_ended(repository, meeting)
    logger.info("Finalized interrupted meeting %s (%d chunk(s) transcribed)",
                meeting_id, total)
    return True


def discard_meeting(repository: Any, meeting_id: str) -> None:
    """Mark an interrupted meeting as failed; audio and segments are kept.

    Args:
        repository: A ``MeetingRepository``.
        meeting_id: The meeting to discard.
    """
    try:
        repository.update_meeting(meeting_id, status="failed",
                                  ended_at=now_iso())
    except Exception:
        logger.exception("Failed to discard meeting %s", meeting_id)


def _resume_endpoint(meeting: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the stored non-secret endpoint snapshot, if any."""
    try:
        from services.text_llm import snapshot_from_meeting

        return snapshot_from_meeting(meeting).to_dict()
    except Exception:
        raw = meeting.get("agent_endpoint_json")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except Exception:
                return None
            return parsed if isinstance(parsed, dict) else None
        return None


def build_resume_options(meeting: Dict[str, Any]) -> Dict[str, Any]:
    """Kwargs hints for ``MeetingEngineOptions`` when resuming a meeting.

    Resume-in-place is handled by the Qt runtime constructing a fresh
    ``MeetingEngine`` — these hints carry the interrupted meeting's title and
    engine-relevant settings forward.

    Args:
        meeting: The interrupted meeting dict.

    Returns:
        A dict of ``MeetingEngineOptions`` field values.
    """
    spool_dir = meeting.get("spool_dir") or ""
    return {
        "title": meeting.get("title") or "",
        "cloud_enabled": bool(meeting.get("cloud_enabled")),
        "asr_model": meeting.get("asr_model") or "auto",
        "llm_provider": meeting.get("agent_provider") or "openrouter",
        "llm_model": meeting.get("agent_model") or "",
        "llm_endpoint": _resume_endpoint(meeting),
        "spool_root": os.path.dirname(spool_dir) if spool_dir else "",
    }


def _mark_ended(repository: Any, meeting: Dict[str, Any]) -> None:
    """Mark a meeting ended, patching status and historical finalization.

    Headless ASR finalize never runs the cloud consolidation pass, so any
    interrupted ``pending``/``running`` finalization is normalized to a durable
    terminal outcome before the snapshot is persisted.
    """
    fields: Dict[str, Any] = {"status": "ended", "ended_at": now_iso()}
    state_json = meeting.get("state_json")
    if state_json:
        try:
            state = json.loads(state_json)
            if not isinstance(state, dict):
                raise ValueError("state snapshot is not an object")
            state["status"] = "ended"
            cloud_enabled = bool(
                meeting.get("cloud_enabled", state.get("cloud_enabled", False))
            )
            state["cloud_enabled"] = cloud_enabled
            state["finalization"] = FinalizationState.normalize_historical(
                state.get("finalization"),
                cloud_enabled=cloud_enabled,
                meeting_status="ended",
            ).to_dict()
            fields["state_json"] = json.dumps(state, ensure_ascii=False)
        except (TypeError, ValueError):
            logger.warning("Unparseable state_json on meeting %s; "
                           "leaving snapshot untouched", meeting.get("id"))
    try:
        repository.update_meeting(meeting["id"], **fields)
    except Exception:
        logger.exception("Failed to mark meeting %s ended", meeting.get("id"))
