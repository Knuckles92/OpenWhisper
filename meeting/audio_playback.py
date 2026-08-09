"""Build seekable, authenticated meeting playback from durable WAV chunks."""
from __future__ import annotations

import json
import logging
import os
import threading
import wave
from typing import Any, Dict, List, Tuple

import numpy as np

logger = logging.getLogger(__name__)

PLAYBACK_RATE = 16000
BLOCK_SAMPLES = PLAYBACK_RATE * 10
_locks_guard = threading.Lock()
_locks: Dict[str, threading.Lock] = {}


def _meeting_lock(meeting_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(meeting_id, threading.Lock())


def _safe_chunk_path(path: str, spool_dir: str) -> str:
    spool = os.path.realpath(os.path.abspath(spool_dir))
    candidate = os.path.realpath(os.path.abspath(path))
    if os.path.dirname(candidate) != spool:
        raise ValueError("audio chunk path is outside its meeting spool")
    return candidate


def _watermark(chunks: List[Dict[str, Any]]) -> List[Tuple[Any, ...]]:
    result = []
    for chunk in chunks:
        path = str(chunk.get("file_path") or "")
        try:
            stat = os.stat(path)
            fingerprint = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            fingerprint = (0, 0)
        result.append((chunk.get("id"), *fingerprint))
    return result


def build_playback(repository: Any, meeting_id: str) -> str:
    """Return a cached mono WAV aligned to meeting-clock timestamps."""
    meeting = repository.get_meeting(meeting_id)
    if meeting is None:
        raise ValueError("unknown meeting")
    spool_dir = str(meeting.get("spool_dir") or "")
    if not spool_dir or not os.path.isdir(spool_dir):
        raise FileNotFoundError("meeting audio is unavailable")
    chunks = repository.get_audio_chunks(meeting_id)
    if not chunks:
        raise FileNotFoundError("meeting audio is unavailable")
    for chunk in chunks:
        chunk["file_path"] = _safe_chunk_path(
            str(chunk.get("file_path") or ""), spool_dir
        )

    output = os.path.join(spool_dir, "playback.wav")
    metadata = os.path.join(spool_dir, "playback.json")
    watermark = _watermark(chunks)
    lock = _meeting_lock(meeting_id)
    with lock:
        try:
            with open(metadata, "r", encoding="utf-8") as handle:
                cached = json.load(handle)
            if cached.get("watermark") == [list(x) for x in watermark] \
                    and os.path.isfile(output):
                return output
        except (OSError, ValueError, TypeError):
            pass

        temp_output = f"{output}.tmp"
        temp_metadata = f"{metadata}.tmp"
        try:
            _render_mono(chunks, temp_output)
            with open(temp_metadata, "w", encoding="utf-8") as handle:
                json.dump({"watermark": watermark}, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_output, output)
            os.replace(temp_metadata, metadata)
        finally:
            for temporary in (temp_output, temp_metadata):
                try:
                    os.remove(temporary)
                except FileNotFoundError:
                    pass
        return output


def _render_mono(chunks: List[Dict[str, Any]], output_path: str) -> None:
    """Render chunks blockwise, averaging overlapping mic/loopback samples."""
    end_s = max(
        float(chunk.get("start_s") or 0.0)
        + float(chunk.get("duration_s") or 0.0)
        for chunk in chunks
    )
    total_samples = max(0, int(round(end_s * PLAYBACK_RATE)))
    with wave.open(output_path, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(PLAYBACK_RATE)
        for block_start in range(0, total_samples, BLOCK_SAMPLES):
            block_end = min(total_samples, block_start + BLOCK_SAMPLES)
            mixed = np.zeros(block_end - block_start, dtype=np.int32)
            contributors = np.zeros(block_end - block_start, dtype=np.int16)
            for chunk in chunks:
                chunk_start = int(round(float(chunk["start_s"]) * PLAYBACK_RATE))
                chunk_frames = int(
                    round(float(chunk["duration_s"]) * PLAYBACK_RATE)
                )
                chunk_end = chunk_start + chunk_frames
                overlap_start = max(block_start, chunk_start)
                overlap_end = min(block_end, chunk_end)
                if overlap_end <= overlap_start:
                    continue
                with wave.open(chunk["file_path"], "rb") as source:
                    if (source.getnchannels() != 1
                            or source.getsampwidth() != 2
                            or source.getframerate() != PLAYBACK_RATE):
                        logger.warning("Skipping incompatible meeting chunk %s",
                                       chunk.get("id"))
                        continue
                    source.setpos(overlap_start - chunk_start)
                    raw = source.readframes(overlap_end - overlap_start)
                frames = np.frombuffer(raw, dtype="<i2")
                size = min(len(frames), overlap_end - overlap_start)
                if size <= 0:
                    continue
                dst = overlap_start - block_start
                mixed[dst:dst + size] += frames[:size].astype(np.int32)
                contributors[dst:dst + size] += 1
            mask = contributors > 1
            mixed[mask] //= contributors[mask].astype(np.int32)
            rendered = np.clip(mixed, -32768, 32767).astype("<i2")
            target.writeframes(rendered.tobytes())
