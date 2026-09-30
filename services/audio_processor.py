"""File-size checks and silence-aware audio splitting."""
import os
import wave
import numpy as np
import tempfile
import logging
import shutil
from contextlib import closing
from dataclasses import dataclass, field
from typing import Callable, List, Tuple, Optional
from config import config
from services.format_utils import format_audio_duration

logger = logging.getLogger(__name__)

@dataclass
class AudioFilePreview:
    """Preview information for an audio file."""
    file_path: str
    file_name: str
    file_size_mb: float
    duration_seconds: float
    sample_rate: int
    channels: int
    #: Whether the engine this preview was read for will split the file.
    needs_splitting: bool
    estimated_chunks: int
    chunk_durations: List[float] = field(default_factory=list)

    @property
    def over_upload_limit(self) -> bool:
        """Whether the file is bigger than one API upload may be."""
        return self.file_size_mb > config.MAX_FILE_SIZE_MB

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
    taps: smoothing a ten-minute recording measured 8.8 s in the old full-file
    path. Differencing a cumulative sum gives the same
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

    def preview_file(
        self, audio_path: str, *, engine_splits: bool
    ) -> AudioFilePreview:
        """Return metadata and estimated chunks without creating files.

        Args:
            audio_path: The file to read.
            engine_splits: Whether the engine that will transcribe the file
                splits one over the upload limit (only the OpenAI API does).

        Use the header for every engine, including files that need splitting.
        Chunk counts are estimates based on decoded PCM size; silence-aware
        boundaries are chosen only when transcription actually starts. A
        missing duration requires a streaming count, never a full-file array.
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        file_name = os.path.basename(audio_path)
        file_size_bytes = os.path.getsize(audio_path)
        file_size_mb = file_size_bytes / (1024 * 1024)
        needs_splitting = engine_splits and file_size_mb > config.MAX_FILE_SIZE_MB

        try:
            duration_seconds, sample_rate, channels = self._probe_audio_header(
                audio_path
            )
        except Exception as e:
            raise ValueError(f"Failed to read audio file: {e}") from e

        if needs_splitting:
            total_samples = round(duration_seconds * sample_rate)
            split_points = self._generate_time_based_splits(total_samples, sample_rate)
            chunk_durations = []
            start_idx = 0
            for end_idx in split_points + [total_samples]:
                chunk_samples = end_idx - start_idx
                chunk_duration = chunk_samples / sample_rate
                chunk_durations.append(chunk_duration)
                start_idx = end_idx

            estimated_chunks = len(chunk_durations)
        else:
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

    def split_audio_file(
        self, audio_path: str,
        progress_callback: Optional[Callable[[str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> List[str]:
        """Decode once, retaining at most one upload window plus one frame.

        The buffer includes both overlaps and the WAV header in its size
        budget. One extra sample provides lookahead so an exact-sized final
        chunk does not create an extra chunk containing only overlap.
        """
        def check_cancel():
            if should_cancel is not None and should_cancel():
                raise RuntimeError("Transcription canceled")

        chunk_files = []
        temp_dir = None
        try:
            check_cancel()
            if progress_callback:
                progress_callback("Decoding and splitting audio...")
            temp_dir = tempfile.mkdtemp(prefix="audio_chunks_")
            # Register immediately: a failed write can leave a partial file.
            self.temp_files.append(temp_dir)

            def save_chunk(samples, rate):
                check_cancel()
                filename = os.path.join(temp_dir, f"chunk_{len(chunk_files):03d}.wav")
                self._save_audio_chunk(samples, rate, filename)
                chunk_files.append(filename)
                if progress_callback:
                    progress_callback(f"Created audio chunk {len(chunk_files)}...")

            pending = None
            count = prefix = 0
            with closing(self._iter_audio_blocks(audio_path, should_cancel)) as blocks:
                for samples, sample_rate, _channels in blocks:
                    check_cancel()
                    if pending is None:
                        max_samples, overlap = self._chunk_limits(sample_rate)
                        pending = np.empty(max_samples + 1, dtype=np.int16)
                    cursor = 0
                    while cursor < len(samples):
                        check_cancel()
                        take = min(len(pending) - count, len(samples) - cursor)
                        pending[count:count + take] = samples[cursor:cursor + take]
                        count += take
                        cursor += take
                        if count <= max_samples:
                            continue
                        boundary = self._chunk_boundary(
                            pending[:max_samples], sample_rate, prefix, overlap, should_cancel
                        )
                        save_chunk(pending[:boundary + overlap], sample_rate)
                        retain = boundary - overlap
                        remaining = count - retain
                        pending[:remaining] = pending[retain:count].copy()
                        count = remaining
                        prefix = overlap
                if count:
                    save_chunk(pending[:count], sample_rate)
            check_cancel()

            logger.info(f"Successfully split audio into {len(chunk_files)} chunks")
            return chunk_files

        except Exception as e:
            logger.error(f"Failed to split audio file: {e}")
            # Only this operation's files are owned here; another completed
            # operation may still be uploading files from the same processor.
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)
                if temp_dir in self.temp_files:
                    self.temp_files.remove(temp_dir)
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
            "No duration in the header of %s; counting decoded samples",
            os.path.basename(audio_path),
        )
        total_samples = 0
        for samples, sample_rate, channels in self._iter_audio_blocks(audio_path):
            total_samples += len(samples)
        return total_samples / sample_rate, sample_rate, channels

    def _iter_audio_blocks(self, audio_path: str, should_cancel=None):
        """Yield native-rate mono int16 PCM with the source's channel count.

        Explicit conversion handles packed/planar layouts and integer/float
        formats. Multiplying decoded integer PCM by 32767 corrupts samples;
        flattening packed stereo doubles duration. FFmpeg handles both here.
        """
        import av

        found_audio = False
        with av.open(audio_path) as container:
            if not container.streams.audio:
                raise ValueError("No audio stream found in file")
            stream = container.streams.audio[0]
            sample_rate = stream.rate
            channels = stream.channels
            if not sample_rate or sample_rate <= 0:
                raise ValueError("Invalid audio sample rate")
            converter = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
            for frame in container.decode(audio=0):
                if should_cancel is not None and should_cancel():
                    raise RuntimeError("Transcription canceled")
                frame.pts = None
                for converted in converter.resample(frame):
                    samples = converted.to_ndarray().reshape(-1)
                    if samples.size:
                        found_audio = True
                        yield samples, sample_rate, channels
            for converted in converter.resample(None):
                if should_cancel is not None and should_cancel():
                    raise RuntimeError("Transcription canceled")
                samples = converted.to_ndarray().reshape(-1)
                if samples.size:
                    found_audio = True
                    yield samples, sample_rate, channels
        if not found_audio:
            raise ValueError("No audio frames found in file")

    def _load_audio_metadata(self, audio_path: str) -> Tuple[np.ndarray, int, int]:
        """Compatibility helper; preview and splitting use bounded streaming."""
        pieces = []
        for samples, sample_rate, channels in self._iter_audio_blocks(audio_path):
            pieces.append(samples)
        return np.concatenate(pieces), sample_rate, channels

    def _chunk_limits(self, sample_rate: int) -> Tuple[int, int]:
        # A mono PCM WAV written by wave has a 44-byte header.
        max_samples = (int(config.MAX_FILE_SIZE_MB * 1024 * 1024) - 44) // 2
        if max_samples < 4:
            raise ValueError("Upload size limit is too small for a WAV file")
        overlap = min(max(0, int(config.OVERLAP_DURATION_SEC * sample_rate)), max_samples // 4)
        return max_samples, overlap

    def _chunk_boundary(self, samples, sample_rate, prefix, overlap, should_cancel=None):
        """Pick a silence inside one bounded window or use its last safe cut."""
        end = len(samples) - overlap
        # Leave room to advance even if the configured minimum duration is
        # longer than a permitted upload (e.g. very high sample rates).
        minimum = max(1, int(config.MIN_CHUNK_DURATION_SEC * sample_rate))
        start = max(overlap + 1, prefix + 1, min(prefix + minimum, end))
        silence_samples = max(1, int(config.SILENCE_DURATION_SEC * sample_rate))
        if end - start >= silence_samples:
            absolute = np.abs(samples.astype(np.float32)) / 32768.0
            smooth = _moving_average(absolute, max(1, int(0.1 * sample_rate)))
            boundary = self._find_best_silence(
                smooth, start, end, silence_samples, sample_rate, should_cancel
            )
            if boundary is not None:
                return boundary
        return end

    def _find_split_points(self, audio_data: np.ndarray, sample_rate: int) -> List[int]:
        """Array compatibility helper using the same bounded split windows."""
        max_samples, overlap = self._chunk_limits(sample_rate)
        split_points = []
        start = prefix = 0
        while len(audio_data) - start > max_samples:
            boundary = start + self._chunk_boundary(
                audio_data[start:start + max_samples], sample_rate, prefix, overlap
            )
            split_points.append(boundary)
            start = boundary - overlap
            prefix = overlap
        return split_points

    def _find_best_silence(self, audio_smooth: np.ndarray, start: int, end: int,
                          silence_samples: int, sample_rate: int,
                          should_cancel=None) -> Optional[int]:
        # Search from the end of the range backwards to prefer later splits
        search_range = range(end - silence_samples, start, -max(1, int(0.1 * sample_rate)))

        best_silence_start = None
        best_silence_quality = float('inf')

        for i in search_range:
            if should_cancel is not None and should_cancel():
                raise RuntimeError("Transcription canceled")
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
        max_samples, overlap = self._chunk_limits(sample_rate)
        split_points = []
        current_pos = 0
        while total_samples - current_pos > max_samples:
            boundary = current_pos + max_samples - overlap
            split_points.append(boundary)
            current_pos = boundary - overlap
        return split_points

    def _create_chunks(self, audio_data: np.ndarray, sample_rate: int,
                      split_points: List[int], original_file: str) -> List[str]:
        chunk_files = []
        _max_samples, overlap_samples = self._chunk_limits(sample_rate)

        temp_dir = tempfile.mkdtemp(prefix="audio_chunks_")
        self.temp_files.append(temp_dir)

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
