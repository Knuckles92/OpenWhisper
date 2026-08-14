"""Durable chunk spool: capture blocks in, registered 16 kHz WAV chunks out.

One ``SpoolWriter`` per channel turns incoming ``CaptureBlock`` values into a
continuous 16 kHz mono int16 stream on the meeting clock (gap-filling silence
when the source goes quiet), cuts chunks at quiet points after a target
duration (hard cut at a maximum), writes each chunk atomically (temp file +
``os.replace``), registers it in the repository with ``asr_status='pending'``,
and hands the resulting ``SpooledChunk`` to the ASR engine.
Registration-before-notification is what makes the pipeline crash-safe: a
chunk on disk and in SQLite survives a hard kill and is picked up by recovery.

Threading model
---------------
``feed()`` runs on the PortAudio callback thread, whose budget is 23.2 ms at
44.1 kHz / 1024 frames, so it does the absolute minimum: convert the block
timestamp to meeting time and hand the *native-rate* frames to a bounded
queue. Everything expensive -- gap-fill, cut scanning, resampling, WAV
writing, SQLite registration and the ``on_chunk`` callback -- runs on a
dedicated daemon writer thread. A full queue drops blocks and logs once
(bounded memory beats a stalled audio thread); it is sized for ~12 s of
audio, far beyond the worst observed SQLite busy-wait.

Timeline accuracy
-----------------
Audio is buffered at the device's native rate and resampled to 16 kHz exactly
once, at cut time. Resampling every block instead rounds each block to a
whole number of output samples 43 times a second (371.52 -> 372 samples at
44.1 kHz), which accumulates about 4.7 s of drift per hour on mic and drives
spurious gap-fills on loopback; it also concatenates independently
FFT-resampled blocks, which injects a discontinuity artifact at every block
boundary. Chunk offsets are derived from cumulative *native* sample counts,
so ``start_s`` is exact and consecutive chunks are contiguous by
construction.

The gap-fill and cut-point decisions live in pure module-level helpers so
tests can drive them with synthetic arrays.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import wave
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np

from meeting.clock import MeetingClock
from meeting.interfaces import CaptureBlock, SpooledChunk
from services.streaming_transcriber import fft_resample

logger = logging.getLogger(__name__)

#: Spool output sample rate (what faster-whisper wants).
TARGET_RATE = 16000

#: Gaps larger than this (seconds of meeting time) are filled with silence;
#: overlaps larger than this are trimmed off the incoming block.
GAP_TOLERANCE_S = 0.12

#: RMS (int16 units) below which audio counts as quiet for chunk cutting.
QUIET_RMS = 300.0

#: A quiet stretch must last this long (seconds) to be a cut point.
QUIET_WINDOW_S = 0.4

#: ``flush()`` drops remainders shorter than this (seconds). Kept small so a
#: meeting's final words survive; only degenerate slivers are discarded.
MIN_FLUSH_S = 0.25

#: Hop (seconds) between candidate quiet windows when scanning for a cut.
_CUT_SCAN_HOP_S = 0.05

#: Capture blocks the queue holds before dropping (~12 s at 1024/44.1 kHz).
QUEUE_MAX_BLOCKS = 512

#: Preferred chunk duration (seconds) before quiet-cut scanning starts.
#: Kept short so the dashboard sees near-live transcripts.
DEFAULT_TARGET_SEC = 5.0

#: Hard maximum chunk duration (seconds) when no quiet cut appears.  A longer
#: ceiling than the live target avoids slicing continuous speech every few
#: seconds while still bounding transcript latency when a speaker never pauses.
DEFAULT_MAX_SEC = 20.0

#: ``flush()`` waits at most this long for the writer thread to finish.
FLUSH_TIMEOUT_S = 30.0

#: Poll interval for the writer thread when the queue is empty.
_WRITER_POLL_S = 0.05

#: Delay before the single ``register_chunk`` retry.
_REGISTER_RETRY_DELAY_S = 0.1

#: Gap/overlap corrections logged at INFO before falling back to DEBUG.
_MAX_GAP_LOGS = 5

#: Native-rate session PCM is resampled to 16 kHz in these windows at flush.
#: Large enough to avoid the per-block FFT discontinuity, small enough to
#: keep peak memory bounded on a long meeting.
SESSION_RESAMPLE_WINDOW_S = 30.0

#: How often the session JSON watermark is rewritten during capture.
_SESSION_META_EVERY_SAMPLES = 16000 * 5

#: Queue item that tells the writer thread to finalize and exit.
_SENTINEL = object()


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable with synthetic arrays)
# ---------------------------------------------------------------------------

def resample_to_16k(frames_int16: np.ndarray, src_rate: int) -> np.ndarray:
    """Resample mono int16 audio to 16 kHz mono int16.

    Call this once per finalized chunk, never per capture block:
    ``fft_resample`` treats its input as one period of a periodic signal, so
    resampling short blocks independently and concatenating them injects a
    discontinuity artifact at every boundary (measured THD+N on a 1 kHz tone,
    48 kHz -> 16 kHz: -87 dB whole-buffer vs -29 dB block-wise).

    Args:
        frames_int16: 1-D mono int16 samples at ``src_rate``.
        src_rate: Sample rate of ``frames_int16`` in Hz.

    Returns:
        1-D int16 samples at 16 kHz (the input itself when already 16 kHz).
    """
    frames = np.asarray(frames_int16)
    if frames.size == 0:
        return np.zeros(0, dtype=np.int16)
    if int(src_rate) == TARGET_RATE:
        return frames.astype(np.int16, copy=False)
    float_frames = frames.astype(np.float32) / 32768.0
    n_out = max(1, int(round(frames.size * TARGET_RATE / float(src_rate))))
    resampled = fft_resample(float_frames, n_out)
    resampled = np.clip(resampled, -1.0, 1.0)
    return (resampled * 32767.0).astype(np.int16)


def gap_fill_frames(expected_start_s: Optional[float], actual_start_s: float,
                    sample_rate: int = TARGET_RATE,
                    tolerance_s: float = GAP_TOLERANCE_S) -> np.ndarray:
    """Silence to insert when a block arrives later than the stream expects.

    Args:
        expected_start_s: Meeting time where the next sample should land
            (end of the previously appended audio), or None for the first
            block.
        actual_start_s: Meeting time of the incoming block's first frame.
        sample_rate: Sample rate of the stream being filled.
        tolerance_s: Gaps at or below this many seconds are absorbed as
            timing jitter.

    Returns:
        A (possibly empty) 1-D int16 array of zeros covering the gap.
    """
    if expected_start_s is None:
        return np.zeros(0, dtype=np.int16)
    gap_s = actual_start_s - expected_start_s
    if gap_s <= tolerance_s:
        return np.zeros(0, dtype=np.int16)
    return np.zeros(int(round(gap_s * sample_rate)), dtype=np.int16)


class _CutScanner:
    """Incremental quiet-window scanner over a growing pending buffer.

    Audio is folded into fixed ``_CUT_SCAN_HOP_S`` frames as it arrives and
    only the energy of each frame is kept, so a cut decision costs O(new
    audio) instead of rescanning the whole bounded buffer on every capture
    block.
    """

    def __init__(self, sample_rate: int, target_sec: float, max_sec: float,
                 quiet_rms: float = QUIET_RMS,
                 quiet_window_s: float = QUIET_WINDOW_S) -> None:
        """Args:
            sample_rate: Sample rate of the pushed audio in Hz.
            target_sec: Minimum chunk duration before scanning starts.
            max_sec: Hard maximum chunk duration.
            quiet_rms: RMS threshold in int16 units.
            quiet_window_s: Required quiet duration in seconds.
        """
        self._rate = int(sample_rate)
        self._frame_len = max(1, int(round(_CUT_SCAN_HOP_S * self._rate)))
        self._win_frames = max(
            1, int(round(quiet_window_s / _CUT_SCAN_HOP_S))
        )
        self._target_n = int(target_sec * self._rate)
        self._max_n = int(max_sec * self._rate)
        self._threshold = float(quiet_rms) ** 2
        self._prefix: List[float] = [0.0]  # prefix[i] = energy of frames [0, i)
        self._carry = np.zeros(0, dtype=np.int16)
        self._total = 0
        # First candidate window start, in frames (ceil(target_n / frame_len)).
        self._next_frame = -(-self._target_n // self._frame_len)

    @property
    def total_samples(self) -> int:
        """Samples pushed so far."""
        return self._total

    def push(self, frames: np.ndarray) -> None:
        """Fold newly buffered samples into the frame-energy prefix sums.

        Args:
            frames: 1-D int16 samples appended to the pending buffer.
        """
        if frames.size == 0:
            return
        self._total += int(frames.size)
        data = (np.concatenate((self._carry, frames))
                if self._carry.size else frames)
        n_full = data.size // self._frame_len
        if n_full:
            usable = data[:n_full * self._frame_len].astype(np.float64)
            energies = (usable * usable).reshape(
                n_full, self._frame_len
            ).sum(axis=1)
            self._prefix.extend(
                (np.cumsum(energies) + self._prefix[-1]).tolist()
            )
        self._carry = np.array(data[n_full * self._frame_len:], dtype=np.int16)

    def cut_index(self) -> Optional[int]:
        """Sample index to cut at, or None when no cut is due yet."""
        if self._total < self._target_n:
            return None
        win = self._win_frames
        n_frames = len(self._prefix) - 1
        limit = min(self._total, self._max_n)
        last_end_frame = min(n_frames, limit // self._frame_len)
        denom = float(win * self._frame_len)
        frame = self._next_frame
        while frame + win <= last_end_frame:
            mean_sq = (self._prefix[frame + win] - self._prefix[frame]) / denom
            if mean_sq < self._threshold:
                return (frame + win) * self._frame_len
            frame += 1
        self._next_frame = frame
        if self._total >= self._max_n:
            return self._max_n
        return None


def find_cut_point(buffer: np.ndarray, sample_rate: int, target_sec: float,
                   max_sec: float, quiet_rms: float = QUIET_RMS,
                   quiet_window_s: float = QUIET_WINDOW_S) -> Optional[int]:
    """Choose where to cut the next chunk out of a pending sample buffer.

    After ``target_sec`` of audio, the first window of ``quiet_window_s``
    whose RMS stays below ``quiet_rms`` marks a cut (at the window's end, so
    the chunk keeps its quiet tail and the next chunk starts past the
    silence). When no quiet window exists, the chunk is hard-cut at
    ``max_sec``. This one-shot form is the pure equivalent of what
    ``SpoolWriter`` runs incrementally.

    Args:
        buffer: 1-D int16 pending samples.
        sample_rate: Sample rate of ``buffer`` in Hz.
        target_sec: Minimum chunk duration before quiet-cut scanning starts.
        max_sec: Hard maximum chunk duration.
        quiet_rms: RMS threshold in int16 units.
        quiet_window_s: Required quiet duration in seconds.

    Returns:
        Sample index to cut at, or None when no cut is due yet.
    """
    scanner = _CutScanner(sample_rate, target_sec, max_sec,
                          quiet_rms, quiet_window_s)
    scanner.push(np.asarray(buffer))
    return scanner.cut_index()


def session_pcm_path(spool_dir: str, channel: str) -> str:
    """Native-rate session PCM path for one capture channel."""
    return os.path.join(spool_dir, f"{channel}_session.pcm")


def session_meta_path(spool_dir: str, channel: str) -> str:
    """Sidecar metadata for the native-rate session PCM."""
    return os.path.join(spool_dir, f"{channel}_session.json")


def session_wav_path(spool_dir: str, channel: str) -> str:
    """16 kHz session WAV path used by the offline ASR pass."""
    return os.path.join(spool_dir, f"{channel}_session.wav")


def write_session_meta(path: str, sample_rate: int, sample_count: int,
                       origin_s: float) -> None:
    """Atomically write session PCM watermark metadata.

    Args:
        path: Destination JSON path.
        sample_rate: Native sample rate of the PCM file.
        sample_count: Number of int16 samples written so far.
        origin_s: Meeting-clock time of the first sample.
    """
    payload = {
        "sample_rate": int(sample_rate),
        "sample_count": int(sample_count),
        "origin_s": float(origin_s),
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def load_session_meta(path: str) -> Optional[Dict[str, Any]]:
    """Return session PCM metadata, or None when missing/corrupt."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return {
            "sample_rate": int(data.get("sample_rate") or 0),
            "sample_count": int(data.get("sample_count") or 0),
            "origin_s": float(data.get("origin_s") or 0.0),
        }
    except (TypeError, ValueError):
        return None


def resample_pcm_file_to_16k(
    src_pcm: str,
    src_rate: int,
    dest_pcm: str,
    *,
    append: bool = False,
    window_s: float = SESSION_RESAMPLE_WINDOW_S,
) -> int:
    """Streaming-resample a native PCM file into 16 kHz PCM.

    Args:
        src_pcm: Native-rate packed int16 PCM.
        src_rate: Sample rate of ``src_pcm``.
        dest_pcm: Destination 16 kHz packed int16 PCM.
        append: When True, append to ``dest_pcm`` if it already exists.
        window_s: FFT window size in seconds.

    Returns:
        Number of 16 kHz samples written.
    """
    rate = max(1, int(src_rate))
    window_n = max(1, int(round(float(window_s) * rate)))
    written = 0
    mode = "ab" if append and os.path.isfile(dest_pcm) else "wb"
    os.makedirs(os.path.dirname(os.path.abspath(dest_pcm)) or ".", exist_ok=True)
    with open(src_pcm, "rb") as src, open(dest_pcm, mode) as dest:
        while True:
            raw = src.read(window_n * 2)
            if not raw:
                break
            frames = np.frombuffer(raw, dtype="<i2")
            if frames.size == 0:
                break
            out = resample_to_16k(frames, rate)
            dest.write(np.ascontiguousarray(out, dtype=np.int16).tobytes())
            written += int(out.size)
    return written


def pcm16k_to_wav(pcm_path: str, wav_path: str) -> None:
    """Wrap packed 16 kHz int16 PCM as a mono WAV (atomic replace)."""
    tmp_path = wav_path + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(wav_path)) or ".", exist_ok=True)
    with wave.open(tmp_path, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(TARGET_RATE)
        with open(pcm_path, "rb") as src:
            while True:
                raw = src.read(TARGET_RATE * 2 * 10)
                if not raw:
                    break
                wav_file.writeframes(raw)
    os.replace(tmp_path, wav_path)


def concat_channel_chunks_to_wav(
    chunks: Sequence[Dict[str, Any]],
    channel: str,
    output_path: str,
) -> Optional[float]:
    """Concatenate registered 16 kHz chunks for one channel into a WAV.

    Gaps on the meeting clock are filled with silence so timestamps stay
    aligned. Used when a session WAV is missing (crash / older meetings).

    Args:
        chunks: ``meeting_audio_chunks`` row dicts.
        channel: ``mic`` or ``loopback``.
        output_path: Destination 16 kHz mono WAV.

    Returns:
        Meeting-clock origin of sample 0, or None when nothing was written.
    """
    selected = [
        chunk for chunk in chunks
        if str(chunk.get("channel") or "") == channel
        and chunk.get("file_path")
    ]
    selected.sort(
        key=lambda chunk: (
            float(chunk.get("start_s") or 0.0),
            int(chunk.get("seq") or 0),
        )
    )
    if not selected:
        return None
    origin_s = float(selected[0].get("start_s") or 0.0)
    tmp_path = output_path + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    cursor_s = origin_s
    with wave.open(tmp_path, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(TARGET_RATE)
        for chunk in selected:
            start_s = float(chunk.get("start_s") or 0.0)
            gap_s = start_s - cursor_s
            if gap_s > 1.0 / TARGET_RATE:
                zeros = np.zeros(int(round(gap_s * TARGET_RATE)), dtype=np.int16)
                wav_file.writeframes(zeros.tobytes())
                cursor_s += zeros.size / float(TARGET_RATE)
            try:
                with wave.open(str(chunk["file_path"]), "rb") as source:
                    if (
                        source.getnchannels() != 1
                        or source.getsampwidth() != 2
                        or source.getframerate() != TARGET_RATE
                    ):
                        logger.warning(
                            "Skipping incompatible session-fallback chunk %s",
                            chunk.get("id"),
                        )
                        continue
                    frames = source.readframes(source.getnframes())
            except (OSError, wave.Error):
                logger.exception(
                    "Failed to load chunk %s for session fallback",
                    chunk.get("id"),
                )
                continue
            wav_file.writeframes(frames)
            cursor_s += (len(frames) // 2) / float(TARGET_RATE)
    os.replace(tmp_path, output_path)
    return origin_s


def resolve_session_wav(
    spool_dir: str,
    channel: str,
    chunks: Optional[Sequence[Dict[str, Any]]] = None,
) -> Optional[str]:
    """Return a 16 kHz session WAV, stitching chunks when the file is missing.

    Args:
        spool_dir: Meeting spool directory.
        channel: ``mic`` or ``loopback``.
        chunks: Optional registered chunk rows for the fallback concat.

    Returns:
        Path to a readable session WAV, or None.
    """
    wav_path = session_wav_path(spool_dir, channel)
    try:
        if os.path.isfile(wav_path) and os.path.getsize(wav_path) > 44:
            return wav_path
    except OSError:
        pass
    if not chunks:
        return None
    try:
        origin = concat_channel_chunks_to_wav(chunks, channel, wav_path)
    except Exception:
        logger.exception("Session-WAV chunk fallback failed for %s", channel)
        return None
    if origin is None:
        return None
    try:
        if os.path.isfile(wav_path) and os.path.getsize(wav_path) > 44:
            return wav_path
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# SpoolWriter
# ---------------------------------------------------------------------------

class SpoolWriter:
    """Chunked WAV spool for one channel of a meeting (``ChunkSpool``).

    ``feed()`` is audio-thread safe and never blocks; a daemon writer thread
    does the buffering, cutting, resampling, writing and registration.
    """

    def __init__(self, meeting_id: str, channel: str, spool_dir: str,
                 clock: MeetingClock, repository,
                 on_chunk: Callable[[SpooledChunk], None],
                 target_sec: float = DEFAULT_TARGET_SEC,
                 max_sec: float = DEFAULT_MAX_SEC,
                 quiet_window_s: float = QUIET_WINDOW_S,
                 queue_size: int = QUEUE_MAX_BLOCKS,
                 initial_seq: int = 0) -> None:
        """Args:
            meeting_id: Owning meeting session id.
            channel: ``mic`` or ``loopback``.
            spool_dir: Directory receiving this meeting's WAV chunks
                (created if missing).
            clock: The shared ``MeetingClock``; block timestamps are
                converted through it and blocks are dropped while paused.
            repository: ``MeetingRepository`` used to register chunks.
            on_chunk: Called from the writer thread with each finalized
                ``SpooledChunk``.
            target_sec: Preferred chunk duration before quiet-cut scanning.
            max_sec: Hard maximum chunk duration.
            quiet_window_s: Required quiet stretch before a cut.
            queue_size: Capture blocks buffered between the audio thread and
                the writer thread before blocks are dropped.
            initial_seq: First per-channel sequence number, used after a
                watchdog restart so files and database keys never collide.
        """
        self._meeting_id = meeting_id
        self._channel = channel
        self._spool_dir = spool_dir
        self._clock = clock
        self._repository = repository
        self._on_chunk = on_chunk
        self._target_sec = float(target_sec)
        self._max_sec = float(max_sec)
        self._quiet_window_s = float(quiet_window_s)

        self._queue: "queue.Queue" = queue.Queue(maxsize=max(1, int(queue_size)))
        self._flush_lock = threading.Lock()
        self._closed = False
        self._stop_requested = False
        self._dropped_blocks = 0
        self._feed_error_logged = False

        # Writer-thread-only state.
        self._rate: Optional[int] = None
        self._blocks: List[np.ndarray] = []
        self._pending = 0          # native samples buffered for this chunk
        self._consumed = 0         # native samples already cut into chunks
        self._origin_s = 0.0       # meeting time of the stream's first sample
        self._scanner: Optional[_CutScanner] = None
        self._seq = max(0, int(initial_seq))
        self._gap_logs = 0
        self._final_chunk: Optional[SpooledChunk] = None
        self._session_fp: Optional[Any] = None
        self._session_samples = 0
        self._session_rate: Optional[int] = None
        self._session_origin_s = 0.0
        self._session_meta_at = 0
        self._session_16k_pcm = os.path.join(
            spool_dir, f"{channel}_session.16k.pcm"
        )

        os.makedirs(spool_dir, exist_ok=True)

        self._thread = threading.Thread(
            target=self._writer_loop,
            name=f"meeting-spool-{channel}",
            daemon=True,
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # Audio thread
    # ------------------------------------------------------------------

    def feed(self, block: CaptureBlock) -> None:
        """Hand a captured block to the writer thread. Never blocks or raises.

        Blocks captured while the meeting clock is paused are discarded
        (meeting time stands still, so no gap results). Frames are queued at
        their native rate; resampling happens once per chunk on the writer
        thread.

        Args:
            block: The captured audio block (mono int16, native rate).
        """
        if self._closed:
            return
        try:
            clock = self._clock
            if clock.is_paused:
                return
            frames = block.frames
            if frames is None or len(frames) == 0:
                return
            t_meeting = clock.meeting_time(block.t_mono)
            self._queue.put_nowait(
                (frames, float(t_meeting), int(block.sample_rate))
            )
        except queue.Full:
            self._dropped_blocks += 1
            if self._dropped_blocks == 1:
                logger.error(
                    "Spool queue full on channel %s; dropping capture blocks "
                    "(writer thread is behind)", self._channel,
                )
        except Exception:
            if not self._feed_error_logged:
                self._feed_error_logged = True
                logger.exception("Spool feed failed on channel %s",
                                 self._channel)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def flush(self, timeout_s: float = FLUSH_TIMEOUT_S) -> Optional[SpooledChunk]:
        """Stop the writer, finalize the remainder, and join (end of meeting).

        Remainders shorter than ``MIN_FLUSH_S`` are discarded. Calling this
        more than once is safe; later calls return the same chunk.

        Args:
            timeout_s: Maximum time to wait for the writer thread to drain.

        Returns:
            The last finalized ``SpooledChunk``, or None when nothing (long
            enough) was pending or finalization failed.
        """
        with self._flush_lock:
            if not self._closed:
                self._closed = True
                self._stop_requested = True
                try:
                    self._queue.put_nowait(_SENTINEL)
                except queue.Full:
                    pass  # the writer exits via _stop_requested once drained
                self._thread.join(timeout=timeout_s)
                if self._thread.is_alive():
                    logger.error(
                        "Spool writer for channel %s did not finish within "
                        "%.0fs", self._channel, timeout_s,
                    )
                if self._dropped_blocks:
                    logger.warning("Spool dropped %d capture block(s) on "
                                   "channel %s", self._dropped_blocks,
                                   self._channel)
            return self._final_chunk

    # ------------------------------------------------------------------
    # Writer thread
    # ------------------------------------------------------------------

    def _writer_loop(self) -> None:
        """Drain the queue until flushed, then finalize the remainder."""
        try:
            while True:
                try:
                    item = self._queue.get(timeout=_WRITER_POLL_S)
                except queue.Empty:
                    if self._stop_requested:
                        break
                    continue
                if item is _SENTINEL:
                    break
                try:
                    self._process_block(*item)
                except Exception:
                    logger.exception("Spool writer failed on a block (%s)",
                                     self._channel)
        except Exception:
            logger.exception("Spool writer thread crashed (%s)", self._channel)
        try:
            self._finalize_remainder()
        except Exception:
            logger.exception("Spool remainder finalization failed (%s)",
                             self._channel)
        try:
            self._finalize_session()
        except Exception:
            logger.exception("Session WAV finalization failed (%s)",
                             self._channel)

    def _stream_end_s(self) -> float:
        """Meeting time just past the last buffered sample."""
        rate = self._rate or TARGET_RATE
        return self._origin_s + (self._consumed + self._pending) / float(rate)

    def _reset_stream(self, rate: int, origin_s: float) -> None:
        """Start a fresh native-rate stream (first block or rate change)."""
        self._rate = int(rate)
        self._origin_s = float(origin_s)
        self._consumed = 0
        self._pending = 0
        self._blocks = []
        self._scanner = self._new_scanner(self._rate)

    def _new_scanner(self, rate: int) -> _CutScanner:
        """Build a cut scanner using this spool's live chunking profile."""
        return _CutScanner(
            rate,
            self._target_sec,
            self._max_sec,
            quiet_window_s=self._quiet_window_s,
        )

    def _process_block(self, frames: np.ndarray, t_meeting: float,
                       src_rate: int) -> None:
        """Align one block onto the stream timeline and buffer it."""
        frames = np.asarray(frames, dtype=np.int16).reshape(-1)
        if frames.size == 0:
            return
        rate = int(src_rate) or TARGET_RATE

        if self._rate is None:
            self._reset_stream(rate, t_meeting)
        elif rate != self._rate:
            logger.warning("Capture rate changed on channel %s (%d -> %d Hz); "
                           "cutting the pending chunk", self._channel,
                           self._rate, rate)
            self._finalize_remainder()
            self._spill_session_pcm()
            self._reset_stream(rate, t_meeting)

        gap_s = t_meeting - self._stream_end_s()
        if gap_s > GAP_TOLERANCE_S:
            fill = gap_fill_frames(self._stream_end_s(), t_meeting, self._rate)
            if fill.size:
                self._log_timeline_fix("Filling %.3fs of silence on channel %s",
                                       gap_s)
                self._append(fill)
        elif gap_s < -GAP_TOLERANCE_S:
            # The stream already covers this span: a stalled source delivered
            # a burst of buffered audio stamped at delivery time. Trim the
            # overlap instead of pushing the whole channel permanently ahead.
            trim = min(frames.size, int(round(-gap_s * self._rate)))
            self._log_timeline_fix("Trimming %.3fs of overlap on channel %s",
                                   -gap_s)
            frames = frames[trim:]
            if frames.size == 0:
                return

        self._append(frames)
        self._cut_ready_chunks()

    def _log_timeline_fix(self, message: str, seconds: float) -> None:
        """Log a gap-fill/overlap-trim, throttled after the first few."""
        self._gap_logs += 1
        if self._gap_logs <= _MAX_GAP_LOGS:
            logger.info(message, abs(seconds), self._channel)
        else:
            logger.debug(message, abs(seconds), self._channel)

    def _append(self, frames: np.ndarray) -> None:
        """Buffer native-rate samples without concatenating per block."""
        self._blocks.append(frames)
        self._pending += int(frames.size)
        if self._scanner is not None:
            self._scanner.push(frames)
        self._append_session(frames)

    def _cut_ready_chunks(self) -> None:
        """Finalize as many chunks as the pending buffer supports."""
        while self._scanner is not None:
            cut = self._scanner.cut_index()
            if cut is None or cut <= 0:
                return
            buffered = self._concat_pending()
            chunk = buffered[:cut]
            rest = buffered[cut:]
            rate = self._rate or TARGET_RATE
            start_s = self._origin_s + self._consumed / float(rate)
            self._consumed += int(chunk.size)
            self._blocks = [rest] if rest.size else []
            self._pending = int(rest.size)
            self._scanner = self._new_scanner(rate)
            if rest.size:
                self._scanner.push(rest)
            self._emit_chunk(chunk, start_s, rate)

    def _concat_pending(self) -> np.ndarray:
        """Collapse the buffered native blocks into one contiguous array."""
        if not self._blocks:
            return np.zeros(0, dtype=np.int16)
        if len(self._blocks) == 1:
            return self._blocks[0]
        return np.concatenate(self._blocks)

    def _finalize_remainder(self) -> None:
        """Write whatever is still buffered (end of stream)."""
        if self._pending <= 0 or self._rate is None:
            self._blocks = []
            self._pending = 0
            return
        rate = self._rate
        buffered = self._concat_pending()
        self._blocks = []
        self._pending = 0
        start_s = self._origin_s + self._consumed / float(rate)
        self._consumed += int(buffered.size)
        self._scanner = self._new_scanner(rate)
        if buffered.size / float(rate) < MIN_FLUSH_S:
            logger.info("Dropping %.2fs sub-minimum spool remainder (%s)",
                        buffered.size / float(rate), self._channel)
            return
        self._emit_chunk(buffered, start_s, rate)

    def _append_session(self, frames: np.ndarray) -> None:
        """Append gap-filled native samples to the continuous session PCM."""
        if frames.size == 0 or self._rate is None:
            return
        if self._session_fp is None:
            pcm_path = session_pcm_path(self._spool_dir, self._channel)
            self._session_fp = open(pcm_path, "wb")
            self._session_rate = int(self._rate)
            self._session_origin_s = float(self._origin_s)
            self._session_samples = 0
            self._session_meta_at = 0
        payload = np.ascontiguousarray(frames, dtype=np.int16)
        self._session_fp.write(payload.tobytes())
        self._session_samples += int(payload.size)
        if (
            self._session_samples - self._session_meta_at
            >= _SESSION_META_EVERY_SAMPLES
        ):
            self._write_session_meta()
            self._session_meta_at = self._session_samples

    def _write_session_meta(self) -> None:
        """Flush the native PCM handle and rewrite its JSON watermark."""
        if self._session_fp is not None:
            try:
                self._session_fp.flush()
            except OSError:
                logger.exception("Session PCM flush failed (%s)", self._channel)
        if self._session_rate is None:
            return
        try:
            write_session_meta(
                session_meta_path(self._spool_dir, self._channel),
                self._session_rate,
                self._session_samples,
                self._session_origin_s,
            )
        except Exception:
            logger.exception("Session metadata write failed (%s)", self._channel)

    def _close_session_pcm(self) -> Optional[str]:
        """Close the native PCM handle and return its path when it has audio."""
        handle = self._session_fp
        self._session_fp = None
        if handle is not None:
            try:
                handle.flush()
                handle.close()
            except OSError:
                logger.exception("Session PCM close failed (%s)", self._channel)
        self._write_session_meta()
        pcm_path = session_pcm_path(self._spool_dir, self._channel)
        if self._session_samples <= 0 or not os.path.isfile(pcm_path):
            return None
        return pcm_path

    def _spill_session_pcm(self) -> None:
        """Resample the current native PCM onto the accumulating 16 kHz PCM."""
        pcm_path = self._close_session_pcm()
        rate = self._session_rate
        if pcm_path is None or rate is None:
            self._session_samples = 0
            return
        try:
            resample_pcm_file_to_16k(
                pcm_path,
                rate,
                self._session_16k_pcm,
                append=os.path.isfile(self._session_16k_pcm),
            )
        except Exception:
            logger.exception("Session PCM resample failed (%s)", self._channel)
        self._session_samples = 0
        self._session_rate = None

    def _finalize_session(self) -> None:
        """Resample native PCM to the durable 16 kHz session WAV."""
        self._spill_session_pcm()
        wav_path = session_wav_path(self._spool_dir, self._channel)
        if not os.path.isfile(self._session_16k_pcm):
            return
        try:
            pcm16k_to_wav(self._session_16k_pcm, wav_path)
        except Exception:
            logger.exception("Session WAV wrap failed (%s)", self._channel)
            return
        try:
            os.remove(self._session_16k_pcm)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Chunk output (writer thread)
    # ------------------------------------------------------------------

    def _emit_chunk(self, native_samples: np.ndarray, start_s: float,
                    rate: int) -> Optional[SpooledChunk]:
        """Resample, write atomically, register, then notify.

        The order is pinned: the WAV lands on disk, the row lands in SQLite
        as ``pending``, and only then does ``on_chunk`` fire -- so a crash at
        any point leaves work that recovery can find. Failures are logged and
        swallowed (the stream keeps running); returns None in that case.

        Args:
            native_samples: The chunk's samples at ``rate``.
            start_s: Exact meeting time of the chunk's first sample.
            rate: Native sample rate of ``native_samples``.

        Returns:
            The finalized ``SpooledChunk``, or None on failure.
        """
        samples = resample_to_16k(native_samples, rate)
        if samples.size == 0:
            return None
        seq = self._seq
        self._seq += 1
        duration_s = samples.size / float(TARGET_RATE)
        file_path = os.path.join(
            self._spool_dir, f"{self._channel}_{seq:05d}.wav"
        )
        tmp_path = file_path + ".tmp"
        try:
            with wave.open(tmp_path, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(TARGET_RATE)
                wav_file.writeframes(
                    np.ascontiguousarray(samples, dtype=np.int16).tobytes()
                )
            os.replace(tmp_path, file_path)
        except Exception:
            logger.exception("Failed to write spool chunk %s", file_path)
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return None

        chunk_id = self._register_chunk(file_path, seq, start_s, duration_s)
        if chunk_id is None:
            return None

        chunk = SpooledChunk(
            chunk_id=chunk_id,
            meeting_id=self._meeting_id,
            channel=self._channel,
            seq=seq,
            file_path=file_path,
            start_s=start_s,
            duration_s=duration_s,
            sample_rate=TARGET_RATE,
        )
        self._final_chunk = chunk
        logger.debug("Spooled chunk %s: %.2fs at %.2fs (%s)",
                     chunk_id, duration_s, start_s, self._channel)
        try:
            self._on_chunk(chunk)
        except Exception:
            logger.exception("on_chunk callback raised for chunk %s", chunk_id)
        return chunk

    def _register_chunk(self, file_path: str, seq: int, start_s: float,
                        duration_s: float) -> Optional[int]:
        """Register one written chunk, retrying once before orphaning it.

        Returns:
            The new chunk id, or None when registration failed twice (the
            WAV is then renamed ``.orphan`` so it is visibly, not silently,
            outside the pipeline).
        """
        for attempt in (1, 2):
            try:
                return self._repository.register_chunk(
                    meeting_id=self._meeting_id,
                    channel=self._channel,
                    seq=seq,
                    file_path=file_path,
                    start_s=start_s,
                    duration_s=duration_s,
                    sample_rate=TARGET_RATE,
                    asr_status="pending",
                )
            except Exception:
                logger.exception("Failed to register spool chunk %s "
                                 "(attempt %d)", file_path, attempt)
                if attempt == 1:
                    time.sleep(_REGISTER_RETRY_DELAY_S)
        orphan_path = file_path + ".orphan"
        try:
            os.replace(file_path, orphan_path)
        except OSError:
            logger.exception("Failed to rename orphaned chunk %s", file_path)
            orphan_path = file_path
        logger.error(
            "Chunk %s could not be registered; audio kept at %s but it will "
            "not be transcribed or recovered", file_path, orphan_path,
        )
        return None
