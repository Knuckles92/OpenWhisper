"""Crash recovery for Meeting Mode.

A crash leaves a meeting row in ``active``/``paused`` with a stale
pid/heartbeat and its spooled chunks still ``pending``. On startup the app
scans for such sessions and offers to finalize them (headless: transcribe the
remaining chunks and mark the meeting ended) or discard them.

No Qt imports; the sibling ASR import is lazy so partial availability never
breaks importing this module.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import wave
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from meeting.interfaces import CHANNELS, SpooledChunk
from meeting.state.schema import FinalizationState, now_iso
from meeting.time_utils import seconds_since

logger = logging.getLogger(__name__)

#: A live session must heartbeat at least this often to be considered alive.
STALE_HEARTBEAT_S = 60.0
#: ASR drain budget for headless finalization.
FINALIZE_DRAIN_TIMEOUT_S = 600.0

#: Windows process-liveness constants.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5

# Keep recovered units within the same upper bound as live chunks.  This is
# especially important when SQLite was unavailable for a long time and the
# native session PCM is the only complete source left.
RECOVERED_CHUNK_MAX_S = 20.0


def _read_wav_duration(path: str) -> Optional[Tuple[int, float]]:
    """Validate a recoverable WAV and return ``(rate, duration_s)``."""
    try:
        with wave.open(path, "rb") as wav_file:
            if wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2:
                return None
            rate = int(wav_file.getframerate())
            frames = int(wav_file.getnframes())
    except (OSError, EOFError, wave.Error):
        return None
    if rate <= 0 or frames <= 0:
        return None
    return rate, frames / float(rate)


def _registered_audio(repository: Any, meeting_id: str) -> List[Dict[str, Any]]:
    getter = getattr(repository, "get_audio_chunks", None)
    if not callable(getter):
        return []
    rows = getter(meeting_id)
    return [dict(row) for row in rows]


def _register_recovered_chunk(
    repository: Any,
    fields: Dict[str, Any],
) -> Tuple[int, bool]:
    """Use the repository's idempotent recovery seam when available."""
    method = getattr(repository, "register_chunk_if_missing", None)
    if callable(method):
        result = method(**fields)
        if isinstance(result, tuple):
            return int(result[0]), bool(result[1])
        return int(result), True

    # Compatibility for lightweight test/dry-run repositories.  Recheck the
    # sequence before using the legacy insertion method so repeated recovery
    # is still idempotent in the common single-process case.
    for row in _registered_audio(repository, str(fields["meeting_id"])):
        if (
            str(row.get("channel")) == str(fields["channel"])
            and int(row.get("seq") or 0) == int(fields["seq"])
        ):
            return int(row["id"]), False
    return int(repository.register_chunk(**fields)), True


def _recover_stranded_wavs(
    repository: Any,
    meeting: Dict[str, Any],
    chunks: List[Dict[str, Any]],
) -> int:
    """Register valid ``*.recovery.json`` WAV artifacts exactly once."""
    from meeting.capture.spool import (
        load_chunk_recovery_meta,
        remove_chunk_recovery_meta,
    )

    meeting_id = str(meeting.get("id") or "")
    spool_dir = str(meeting.get("spool_dir") or "")
    if not meeting_id or not os.path.isdir(spool_dir):
        return 0
    by_identity = {
        (str(row.get("channel") or ""), int(row.get("seq") or 0)): row
        for row in chunks
    }
    recovered = 0
    try:
        sidecars = sorted(
            entry.path for entry in os.scandir(spool_dir)
            if entry.is_file() and entry.name.endswith(".recovery.json")
        )
    except OSError:
        logger.exception("Could not scan meeting spool %s", spool_dir)
        return 0

    for sidecar in sidecars:
        canonical = sidecar[:-len(".recovery.json")]
        orphan = canonical + ".orphan"
        artifact = canonical if os.path.isfile(canonical) else orphan
        meta = load_chunk_recovery_meta(canonical)
        if (
            meta is None
            or meta.get("meeting_id") != meeting_id
            or not meta.get("channel")
            or not os.path.isfile(artifact)
        ):
            logger.warning("Leaving unusable recovery artifact %s untouched",
                           sidecar)
            continue
        channel = str(meta["channel"])
        seq = int(meta["seq"])
        identity = (channel, seq)

        # The DB row may have committed immediately before the old process
        # died.  Make its expected canonical path available before removing
        # the leftover transaction marker.
        if artifact == orphan and not os.path.exists(canonical):
            try:
                os.replace(orphan, canonical)
                artifact = canonical
            except OSError:
                logger.exception("Could not restore orphan WAV %s", orphan)
                continue

        wav_info = _read_wav_duration(artifact)
        if wav_info is None:
            logger.warning("Leaving invalid recovery WAV %s untouched", artifact)
            continue
        sample_rate, duration_s = wav_info
        if identity in by_identity:
            remove_chunk_recovery_meta(canonical)
            continue

        fields = {
            "meeting_id": meeting_id,
            "channel": channel,
            "seq": seq,
            "file_path": canonical,
            "start_s": float(meta["start_s"]),
            # Trust the committed WAV header over a sidecar written around a
            # crash boundary.
            "duration_s": duration_s,
            "sample_rate": sample_rate,
            "asr_status": "pending",
        }
        try:
            chunk_id, created = _register_recovered_chunk(repository, fields)
        except Exception:
            logger.exception("Could not register recovery WAV %s", canonical)
            continue
        by_identity[identity] = {"id": chunk_id, **fields}
        chunks.append(by_identity[identity])
        remove_chunk_recovery_meta(canonical)
        if created:
            recovered += 1
            logger.info("Recovered stranded meeting chunk %s (%s seq %d)",
                        chunk_id, channel, seq)
    return recovered


def _pcm_artifacts(
    spool_dir: str,
    channel: str,
) -> Sequence[Tuple[str, int, int, float]]:
    """Return timeline-ordered ``(path, rate, samples, origin_s)`` PCM spans."""
    from meeting.capture.spool import (
        TARGET_RATE,
        load_session_meta,
        session_meta_path,
        session_pcm_path,
        session_wav_path,
    )

    # A completed session WAV means normal flush reached its final durable
    # representation.  The native PCM in older meetings was intentionally
    # retained and must not be mistaken for a new tail.
    final_wav = session_wav_path(spool_dir, channel)
    if _read_wav_duration(final_wav) is not None:
        return []

    meta = load_session_meta(session_meta_path(spool_dir, channel))
    if meta is None:
        return []
    rate = int(meta.get("sample_rate") or 0)
    if rate <= 0:
        return []
    global_origin = float(meta.get("origin_s") or 0.0)
    prefix_path = os.path.join(spool_dir, f"{channel}_session.16k.pcm")
    raw_path = session_pcm_path(spool_dir, channel)
    artifacts: List[Tuple[str, int, int, float]] = []
    prefix_samples = 0
    try:
        prefix_samples = os.path.getsize(prefix_path) // 2
    except OSError:
        pass
    if prefix_samples > 0:
        artifacts.append((prefix_path, TARGET_RATE, prefix_samples, global_origin))

    try:
        raw_samples = os.path.getsize(raw_path) // 2
    except OSError:
        raw_samples = 0
    # A failed unlink after successful prefix conversion can leave the old raw
    # source in place.  New metadata marks it inactive so recovery does not
    # register the same audio twice.  Legacy metadata has no marker and keeps
    # the conservative placement rules below.
    if raw_samples > 0 and meta.get("pcm_active", True):
        if "pcm_origin_s" in meta:
            raw_origin = float(meta["pcm_origin_s"])
        elif prefix_samples:
            # Metadata predating the segment-origin field cannot tell whether
            # this is the old, already-converted source or a newer native-rate
            # suffix.  Recover the unambiguous prefix and retain the raw file
            # for manual/offline salvage instead of creating corrupt overlap.
            logger.warning(
                "Cannot place legacy raw PCM suffix for %s; leaving %s intact",
                channel, raw_path,
            )
            raw_origin = None
        else:
            raw_origin = global_origin
        if raw_origin is not None:
            artifacts.append((raw_path, rate, raw_samples, raw_origin))
    artifacts.sort(key=lambda item: item[3])
    return artifacts


def _recover_pcm_tails(
    repository: Any,
    meeting: Dict[str, Any],
    chunks: List[Dict[str, Any]],
) -> int:
    """Materialize native session audio after the last registered chunk."""
    from meeting.capture.spool import (
        TARGET_RATE,
        pcm_slice_to_wav,
        remove_chunk_recovery_meta,
        write_chunk_recovery_meta,
    )

    meeting_id = str(meeting.get("id") or "")
    spool_dir = str(meeting.get("spool_dir") or "")
    if not meeting_id or not os.path.isdir(spool_dir):
        return 0
    recovered = 0
    channels = set(CHANNELS)
    channels.update(str(row.get("channel") or "") for row in chunks)
    channels.discard("")

    for channel in sorted(channels):
        channel_rows = [
            row for row in chunks if str(row.get("channel") or "") == channel
        ]
        coverage_end = max(
            (
                float(row.get("start_s") or 0.0)
                + max(0.0, float(row.get("duration_s") or 0.0))
                for row in channel_rows
            ),
            default=float("-inf"),
        )
        next_seq = max(
            (int(row.get("seq") or 0) for row in channel_rows), default=-1
        ) + 1

        for pcm_path, rate, sample_count, origin_s in _pcm_artifacts(
            spool_dir, channel
        ):
            artifact_end = origin_s + sample_count / float(rate)
            cursor_s = max(origin_s, coverage_end)
            if cursor_s >= artifact_end - (0.5 / float(rate)):
                continue
            start_sample = max(
                0, int(math.floor((cursor_s - origin_s) * rate + 0.5))
            )
            while start_sample < sample_count:
                end_sample = min(
                    sample_count,
                    start_sample + max(1, int(round(RECOVERED_CHUNK_MAX_S * rate))),
                )
                start_s = origin_s + start_sample / float(rate)
                file_path = os.path.join(
                    spool_dir, f"{channel}_{next_seq:05d}.recovered.wav"
                )
                try:
                    output_frames = pcm_slice_to_wav(
                        pcm_path, rate, start_sample, end_sample, file_path
                    )
                    if output_frames <= 0:
                        break
                    duration_s = output_frames / float(TARGET_RATE)
                    # This sidecar also protects a crash caused by the recovery
                    # pass itself between file replace and SQLite commit.
                    write_chunk_recovery_meta(
                        file_path, meeting_id, channel, next_seq,
                        start_s, duration_s, TARGET_RATE,
                    )
                    fields = {
                        "meeting_id": meeting_id,
                        "channel": channel,
                        "seq": next_seq,
                        "file_path": file_path,
                        "start_s": start_s,
                        "duration_s": duration_s,
                        "sample_rate": TARGET_RATE,
                        "asr_status": "pending",
                    }
                    chunk_id, created = _register_recovered_chunk(
                        repository, fields
                    )
                except Exception:
                    logger.exception(
                        "Could not materialize raw PCM recovery tail for %s",
                        channel,
                    )
                    # Do not advance into later audio: the next startup can
                    # consume the sidecar or retry this exact interval.
                    break
                remove_chunk_recovery_meta(file_path)
                row = {"id": chunk_id, **fields}
                chunks.append(row)
                channel_rows.append(row)
                if created:
                    recovered += 1
                    logger.info(
                        "Recovered %.2fs raw PCM tail as chunk %s (%s)",
                        duration_s, chunk_id, channel,
                    )
                coverage_end = max(coverage_end, start_s + duration_s)
                next_seq += 1
                start_sample = end_sample
    return recovered


def reconcile_meeting_audio(
    repository: Any,
    meeting: Dict[str, Any],
) -> Dict[str, int]:
    """Reconcile crash artifacts into durable pending chunk rows.

    The operation is safe to call before every scan/finalize.  Identity is the
    database's unique ``(meeting_id, channel, seq)`` tuple, and every newly
    materialized tail gets its own transaction sidecar before registration.
    """
    meeting_id = str(meeting.get("id") or "")
    if not meeting_id or not callable(getattr(repository, "get_audio_chunks", None)):
        return {"orphan_chunks": 0, "tail_chunks": 0}
    try:
        chunks = _registered_audio(repository, meeting_id)
        orphans = _recover_stranded_wavs(repository, meeting, chunks)
        # Reload after orphan registration so their coverage prevents the raw
        # session fallback from creating an overlapping chunk.
        chunks = _registered_audio(repository, meeting_id)
        tails = _recover_pcm_tails(repository, meeting, chunks)
        return {"orphan_chunks": orphans, "tail_chunks": tails}
    except Exception:
        logger.exception("Meeting audio reconciliation failed for %s", meeting_id)
        return {"orphan_chunks": 0, "tail_chunks": 0}


def reconcile_startup_audio(repository: Any) -> int:
    """Reconcile dead/terminal meeting spools before recovery candidates load."""
    list_meetings = getattr(repository, "list_meetings", None)
    if not callable(list_meetings):
        return 0
    try:
        meetings = list_meetings()
    except Exception:
        logger.exception("Failed to list meetings for audio reconciliation")
        return 0
    recovered = 0
    for meeting in meetings:
        status = str(meeting.get("status") or "")
        if status in ("active", "paused", "ending", "needs_recovery"):
            if not is_session_dead(meeting):
                continue
        result = reconcile_meeting_audio(repository, meeting)
        recovered += result["orphan_chunks"] + result["tail_chunks"]
    return recovered


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
        age = seconds_since(heartbeat)
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
    # Registration failures and sub-chunk PCM tails are filesystem evidence,
    # so reconcile them before the database query decides which terminal
    # meetings still have unfinished audio.
    reconcile_startup_audio(repository)
    try:
        candidates = repository.find_interrupted_meetings()
    except Exception:
        logger.exception("Failed to scan for interrupted meetings")
        return []
    from meeting.state.schema import finalization_from_meeting_row

    recoverable = []
    for meeting in candidates:
        if not is_session_dead(meeting):
            continue
        try:
            if finalization_from_meeting_row(meeting).card_deferred:
                continue
        except Exception:
            logger.debug(
                "Could not read finalization deferral for meeting %s",
                meeting.get("id"),
                exc_info=True,
            )
        recoverable.append(meeting)
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
        reconcile_meeting_audio(repository, meeting)
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
