"""File-size checks and silence-aware audio splitting."""
import os
import wave
import numpy as np
import tempfile
import logging
import shutil
from dataclasses import dataclass, field
from typing import Callable, List, Tuple, Optional, Dict, Any
from pathlib import Path
from config import config
from services.format_utils import format_audio_duration

logger = logging.getLogger(__name__)

INT16_MIN = -32768
INT16_MAX = 32767


@dataclass
class AudioFilePreview:
    """Preview information for an audio file."""
    file_path: str
    file_name: str
    file_size_mb: float
    duration_seconds: float
    sample_rate: int
    channels: int
    needs_splitting: bool
    estimated_chunks: int
    chunk_durations: List[float] = field(default_factory=list)

    @property
    def duration_formatted(self) -> str:
        """Get duration as formatted string (e.g., '2m 30s' or '3.7s')."""
        return format_audio_duration(self.duration_seconds)

    @property
    def file_size_formatted(self) -> str:
        """Get file size as formatted string."""
        if self.file_size_mb >= 1:
            return f"{self.file_size_mb:.1f} MB"
        return f"{self.file_size_mb * 1024:.0f} KB"


#: Samples per cumulative-sum block. Bounds the float64 working set to about
#: 32 MB no matter how long the recording is; a whole-signal cumsum would need
#: 8 bytes a sample (1.3 GB for an hour).
_SMOOTH_BLOCK_SAMPLES = 1 << 22


def _moving_average(samples: np.ndarray, window: int) -> np.ndarray:
    """Centered boxcar mean — ``np.convolve(x, ones(w)/w, "same")``, in O(n).

    ``np.convolve`` is a direct O(n·w) sum, and the 0.1 s window here is 4410
    taps: smoothing a ten-minute recording measured 8.8 s, and it runs once on
    drop and again on transcribe. Differencing a cumulative sum gives the same
    values in one pass — measured 156 ms for that file, 57x faster, agreeing
    with ``np.convolve`` to 3e-8. That is nine orders of magnitude under
    ``SILENCE_THRESHOLD``, so split points do not move.

    Edges match ``mode="same"``: the window is zero-padded past either end.
    """
    n = samples.size
    if window <= 1 or n == 0:
        return samples.astype(np.float32, copy=False)

    left, right = window // 2, (window - 1) // 2
    span = left + right + 1
    out = np.empty(n, dtype=np.float32)
    step = max(_SMOOTH_BLOCK_SAMPLES, window)

    for start in range(0, n, step):
        stop = min(start + step, n)
        # Each block carries the halo its own windows reach into.
        lo, hi = max(0, start - left), min(n, stop + right)
        block = np.empty(hi - lo + 1, dtype=np.float64)
        block[0] = 0.0
        np.cumsum(samples[lo:hi], dtype=np.float64, out=block[1:])

        # Where the halo was clipped by an end of the signal, extend the sum
        # flat: zeros before the start, the final total after it.
        pad_lo, pad_hi = left - (start - lo), right - (hi - stop)
        if pad_lo or pad_hi:
            block = np.concatenate((
                np.zeros(pad_lo, dtype=np.float64),
                block,
                np.full(pad_hi, block[-1], dtype=np.float64),
            ))

        width = stop - start
        out[start:stop] = (block[span:span + width] - block[:width]) / window

    return out


class AudioProcessor:
    """Handles audio file processing including size checking and smart splitting."""

    def __init__(self):
        self.temp_files: List[str] = []

    def check_file_size(self, audio_path: str) -> Tuple[bool, float]:
        """Return whether the file needs splitting and its size in MiB."""
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        file_size_bytes = os.path.getsize(audio_path)
        file_size_mb = file_size_bytes / (1024 * 1024)

        needs_splitting = file_size_mb > config.MAX_FILE_SIZE_MB

        logger.info(f"Audio file size: {file_size_mb:.2f} MB (limit: {config.MAX_FILE_SIZE_MB} MB)")
        if needs_splitting:
            logger.info("File exceeds size limit, splitting will be required")

        return needs_splitting, file_size_mb

    def preview_file(self, audio_path: str) -> AudioFilePreview:
        """Return metadata and estimated chunks without creating files.

        Only a file large enough to need splitting is decoded. Everything the
        card shows for a normal file — duration, sample rate, channels — is in
        the container header, so decoding one to read three numbers buys
        nothing and is paid on every drop.

        The saving tracks the codec, not the file size: a ten-minute AAC
        recording measured 604 ms to decode against 3.4 ms to read the header,
        and that gap grows with duration. WAV is only about 2x, being little
        more than a PCM copy, but voice memos arriving as m4a/mp3 are the
        common case.
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        file_name = os.path.basename(audio_path)
        file_size_bytes = os.path.getsize(audio_path)
        file_size_mb = file_size_bytes / (1024 * 1024)
        needs_splitting = file_size_mb > config.MAX_FILE_SIZE_MB

        chunk_durations = []
        if needs_splitting:
            try:
                audio_data, sample_rate, channels = self._load_audio_metadata(
                    audio_path
                )
            except Exception as e:
                raise ValueError(f"Failed to read audio file: {e}")

            duration_seconds = len(audio_data) / sample_rate
            split_points = self._find_split_points(audio_data, sample_rate)

            if not split_points:
                split_points = self._generate_time_based_splits(len(audio_data), sample_rate)

            start_idx = 0
            for end_idx in split_points + [len(audio_data)]:
                chunk_samples = end_idx - start_idx
                chunk_duration = chunk_samples / sample_rate
                chunk_durations.append(chunk_duration)
                start_idx = end_idx

            estimated_chunks = len(chunk_durations)
        else:
            try:
                duration_seconds, sample_rate, channels = self._probe_audio_header(
                    audio_path
                )
            except Exception as e:
                raise ValueError(f"Failed to read audio file: {e}")

            estimated_chunks = 1
            chunk_durations = [duration_seconds]

        logger.info(f"Audio preview: {file_name}, {file_size_mb:.2f} MB, "
                    f"{duration_seconds:.1f}s, {estimated_chunks} chunk(s)")

        return AudioFilePreview(
            file_path=audio_path,
            file_name=file_name,
            file_size_mb=file_size_mb,
            duration_seconds=duration_seconds,
            sample_rate=sample_rate,
            channels=channels,
            needs_splitting=needs_splitting,
            estimated_chunks=estimated_chunks,
            chunk_durations=chunk_durations
        )

    def split_audio_file(self, audio_path: str, progress_callback: Optional[Callable[[str], None]] = None) -> List[str]:
        """Split an audio file at silence points, with time-based fallback."""
        try:
            if progress_callback:
                progress_callback("Loading audio file...")

            audio_data, sample_rate = self._load_audio_data(audio_path)

            if progress_callback:
                progress_callback("Analyzing audio for optimal split points...")

            split_points = self._find_split_points(audio_data, sample_rate)

            if not split_points:
                logger.warning("No suitable silence points found, using time-based splitting")
                if progress_callback:
                    progress_callback("Generating time-based splits...")
                split_points = self._generate_time_based_splits(len(audio_data), sample_rate)

            if progress_callback:
                progress_callback(f"Creating {len(split_points)} audio chunks...")

            chunk_files = self._create_chunks(audio_data, sample_rate, split_points, audio_path)

            logger.info(f"Successfully split audio into {len(chunk_files)} chunks")
            return chunk_files

        except Exception as e:
            logger.error(f"Failed to split audio file: {e}")
            self.cleanup_temp_files()
            raise

    def _load_audio_data(self, audio_path: str) -> Tuple[np.ndarray, int]:
        audio_data, sample_rate, _ = self._load_audio_metadata(audio_path)
        return audio_data, sample_rate

    def _probe_audio_header(self, audio_path: str) -> Tuple[float, int, int]:
        """Return ``(duration_s, sample_rate, channels)`` without decoding.

        Prefers the audio stream's own duration and falls back to the
        container's; a stream that reports neither is decoded, because a
        preview with no duration is worse than a slow one.
        """
        import av

        with av.open(audio_path) as container:
            if not container.streams.audio:
                raise ValueError("No audio stream found in file")

            stream = container.streams.audio[0]
            sample_rate = stream.rate
            channels = stream.channels

            duration_seconds = 0.0
            if stream.duration is not None and stream.time_base:
                duration_seconds = float(stream.duration * stream.time_base)
            elif container.duration is not None:
                duration_seconds = float(container.duration) / av.time_base

        if duration_seconds > 0 and sample_rate:
            return duration_seconds, sample_rate, channels

        logger.info(
            "No duration in the header of %s; falling back to a full decode",
            os.path.basename(audio_path),
        )
        audio_data, sample_rate, channels = self._load_audio_metadata(audio_path)
        return len(audio_data) / sample_rate, sample_rate, channels

    def _load_audio_metadata(self, audio_path: str) -> Tuple[np.ndarray, int, int]:
        import av

        container = av.open(audio_path)

        if not container.streams.audio:
            raise ValueError("No audio stream found in file")

        stream = container.streams.audio[0]
        sample_rate = stream.rate
        channels = stream.channels

        frames = []
        for frame in container.decode(audio=0):
            arr = frame.to_ndarray()
            frames.append(arr)

        container.close()

        if not frames:
            raise ValueError("No audio frames found in file")

        # PyAV returns shape (channels, samples) for planar formats
        audio_float = np.concatenate(frames, axis=1 if len(frames[0].shape) > 1 else 0)

        if len(audio_float.shape) > 1 and audio_float.shape[0] > 1:
            audio_float = np.mean(audio_float, axis=0)
        elif len(audio_float.shape) > 1:
            audio_float = audio_float[0]

        audio_data = (audio_float * INT16_MAX).clip(INT16_MIN, INT16_MAX).astype(np.int16)

        return audio_data, sample_rate, channels

    def _find_split_points(self, audio_data: np.ndarray, sample_rate: int) -> List[int]:
        max_chunk_samples = int((config.MAX_FILE_SIZE_MB * 1024 * 1024) / 2)
        min_chunk_samples = int(config.MIN_CHUNK_DURATION_SEC * sample_rate)
        silence_samples = int(config.SILENCE_DURATION_SEC * sample_rate)

        audio_abs = np.abs(audio_data.astype(np.float32)) / 32767.0

        window_size = int(0.1 * sample_rate)
        audio_smooth = _moving_average(audio_abs, window_size)

        split_points = []
        last_split = 0

        search_start = min_chunk_samples
        while search_start < len(audio_data):
            search_end = min(search_start + max_chunk_samples - min_chunk_samples, len(audio_data))

            best_split = self._find_best_silence(audio_smooth, search_start, search_end,
                                               silence_samples, sample_rate)

            if best_split is not None:
                split_points.append(best_split)
                last_split = best_split
                search_start = best_split + min_chunk_samples
            else:
                forced_split = min(last_split + max_chunk_samples, len(audio_data) - 1)
                split_points.append(forced_split)
                last_split = forced_split
                search_start = forced_split + min_chunk_samples

        return split_points

    def _find_best_silence(self, audio_smooth: np.ndarray, start: int, end: int,
                          silence_samples: int, sample_rate: int) -> Optional[int]:
        # Search from the end of the range backwards to prefer later splits
        search_range = range(end - silence_samples, start, -int(0.1 * sample_rate))

        best_silence_start = None
        best_silence_quality = float('inf')

        for i in search_range:
            if i + silence_samples >= len(audio_smooth):
                continue

            silence_region = audio_smooth[i:i + silence_samples]
            max_level = np.max(silence_region)
            avg_level = np.mean(silence_region)

            if max_level < config.SILENCE_THRESHOLD:
                silence_quality = avg_level + (max_level * 0.1)

                if silence_quality < best_silence_quality:
                    best_silence_quality = silence_quality
                    best_silence_start = i + silence_samples // 2

        return best_silence_start

    def _generate_time_based_splits(self, total_samples: int, sample_rate: int) -> List[int]:
        # Target duration per chunk (slightly less than max to account for overhead)
        target_duration = (config.MAX_FILE_SIZE_MB * 0.8) * 1024 * 1024 / (2 * sample_rate)
        target_samples = int(target_duration * sample_rate)

        split_points = []
        current_pos = 0

        while current_pos + target_samples < total_samples:
            current_pos += target_samples
            split_points.append(current_pos)

        return split_points

    def _create_chunks(self, audio_data: np.ndarray, sample_rate: int,
                      split_points: List[int], original_file: str) -> List[str]:
        chunk_files = []
        overlap_samples = int(config.OVERLAP_DURATION_SEC * sample_rate)

        temp_dir = tempfile.mkdtemp(prefix="audio_chunks_")

        start_idx = 0
        for i, end_idx in enumerate(split_points + [len(audio_data)]):
            chunk_start = max(0, start_idx - (overlap_samples if i > 0 else 0))
            chunk_end = min(len(audio_data), end_idx + overlap_samples)

            chunk_data = audio_data[chunk_start:chunk_end]

            chunk_filename = os.path.join(temp_dir, f"chunk_{i:03d}.wav")

            self._save_audio_chunk(chunk_data, sample_rate, chunk_filename)

            chunk_files.append(chunk_filename)
            self.temp_files.append(chunk_filename)

            start_idx = end_idx

            logger.info(f"Created chunk {i+1}: {chunk_filename} "
                        f"({len(chunk_data)/sample_rate:.1f}s, "
                        f"{os.path.getsize(chunk_filename)/(1024*1024):.1f}MB)")

        self.temp_files.append(temp_dir)
        return chunk_files

    def _save_audio_chunk(self, audio_data: np.ndarray, sample_rate: int, filename: str):
        with wave.open(filename, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_data.tobytes())

    def cleanup_temp_files(self):
        """Clean up temporary files created during splitting."""
        for temp_path in self.temp_files:
            try:
                if os.path.isfile(temp_path):
                    os.remove(temp_path)
                elif os.path.isdir(temp_path):
                    shutil.rmtree(temp_path)
            except Exception as e:
                logger.warning(f"Failed to cleanup temp file {temp_path}: {e}")

        self.temp_files.clear()
        logger.info("Temporary files cleaned up")

    def combine_transcriptions(self, transcriptions: List[str]) -> str:
        """Combine non-empty chunk transcripts with normalized spacing."""
        if not transcriptions:
            return ""

        valid_transcriptions = [t.strip() for t in transcriptions if t.strip()]

        if not valid_transcriptions:
            return ""

        combined = ""
        for i, transcription in enumerate(valid_transcriptions):
            if i > 0:
                if not combined.endswith(" ") and not transcription.startswith(" "):
                    combined += " "

            combined += transcription

        while "  " in combined:
            combined = combined.replace("  ", " ")

        return combined.strip()

audio_processor = AudioProcessor()