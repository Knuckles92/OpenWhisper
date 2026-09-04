"""Tests for AudioProcessor previews and splitting.

``preview_file`` runs on every dropped file, so what it costs matters: it is
the difference between a card that appears at once and one that waits out a
full decode of a long recording.
"""
import os
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from config import config
from services.audio_processor import (
    AudioFilePreview,
    AudioProcessor,
    _SMOOTH_BLOCK_SAMPLES,
    _moving_average,
)


def write_wav(path, seconds=2.0, rate=44100, channels=1):
    """Write a real WAV so PyAV parses a genuine container header."""
    frames = int(seconds * rate)
    t = np.linspace(0.0, seconds, frames, endpoint=False)
    tone = (np.sin(2 * np.pi * 440.0 * t) * 8000).astype(np.int16)
    if channels > 1:
        tone = np.repeat(tone[:, None], channels, axis=1).reshape(-1)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(tone.tobytes())
    return str(path)


@pytest.fixture
def processor():
    return AudioProcessor()


class TestPreviewCost:
    def test_small_file_is_previewed_from_the_header_only(
        self, processor, tmp_path
    ):
        """A file that needs no splitting must never be decoded.

        Duration, sample rate and channel count all live in the container
        header. Decoding a long recording to read three numbers is the cost
        this guards against.
        """
        path = write_wav(tmp_path / "clip.wav", seconds=2.0, rate=44100)

        def fail(*_):
            raise AssertionError("preview decoded a file that needs no split")

        processor._load_audio_metadata = fail

        preview = processor.preview_file(path)

        assert preview.needs_splitting is False
        assert preview.sample_rate == 44100
        assert preview.channels == 1
        assert preview.duration_seconds == pytest.approx(2.0, abs=0.05)
        assert preview.estimated_chunks == 1
        assert preview.chunk_durations == [preview.duration_seconds]

    def test_stereo_channel_count_comes_from_the_header(
        self, processor, tmp_path
    ):
        path = write_wav(tmp_path / "stereo.wav", seconds=1.0, channels=2)

        preview = processor.preview_file(path)

        assert preview.channels == 2
        assert preview.duration_seconds == pytest.approx(1.0, abs=0.05)

    def test_large_file_is_decoded_and_reports_chunks(
        self, processor, tmp_path, monkeypatch
    ):
        """Split points still need the samples, so this file is decoded."""
        path = write_wav(tmp_path / "big.wav", seconds=3.0, rate=44100)
        monkeypatch.setattr(config, "MAX_FILE_SIZE_MB", 0.05)
        decoded = []
        original = processor._load_audio_metadata
        processor._load_audio_metadata = lambda p: (
            decoded.append(p) or original(p)
        )

        preview = processor.preview_file(path)

        assert decoded == [path]
        assert preview.needs_splitting is True
        assert preview.estimated_chunks >= 1
        assert sum(preview.chunk_durations) == pytest.approx(3.0, abs=0.05)

    def test_header_without_duration_falls_back_to_decoding(
        self, processor, tmp_path, monkeypatch
    ):
        """A preview with no duration would be worse than a slow one."""
        import av

        path = write_wav(tmp_path / "clip.wav", seconds=1.5)
        decoded = []
        # Stubbed rather than wrapped: the fake ``av.open`` below intercepts
        # every call, including the fallback's own decode.
        processor._load_audio_metadata = lambda p: (
            decoded.append(p)
            or (np.zeros(int(1.5 * 44100), dtype=np.int16), 44100, 1)
        )

        class _Stream:
            rate = 44100
            channels = 1
            duration = None
            time_base = None

        class _Container:
            duration = None
            streams = SimpleNamespace(audio=[_Stream()])

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        monkeypatch.setattr(av, "open", lambda *_: _Container())

        duration, rate, channels = processor._probe_audio_header(path)

        assert decoded == [path], "a duration-less header must fall back"
        assert duration == pytest.approx(1.5, abs=0.05)
        assert rate == 44100
        assert channels == 1


class TestPreviewErrors:
    def test_missing_file_raises_file_not_found(self, processor, tmp_path):
        with pytest.raises(FileNotFoundError):
            processor.preview_file(str(tmp_path / "nope.wav"))

    def test_unreadable_file_raises_value_error(self, processor, tmp_path):
        """The Upload tab turns this into an inline notice, not a crash."""
        path = tmp_path / "broken.wav"
        path.write_bytes(b"not audio at all")

        with pytest.raises(ValueError):
            processor.preview_file(str(path))


class TestSplitting:
    def test_split_produces_files_and_cleanup_removes_them(
        self, processor, tmp_path, monkeypatch
    ):
        path = write_wav(tmp_path / "big.wav", seconds=3.0, rate=44100)
        monkeypatch.setattr(config, "MAX_FILE_SIZE_MB", 0.05)

        chunks = processor.split_audio_file(path)

        assert chunks
        assert all(os.path.exists(chunk) for chunk in chunks)

        processor.cleanup_temp_files()

        assert not any(os.path.exists(chunk) for chunk in chunks)
        assert processor.temp_files == []


class TestPreviewShape:
    def test_preview_keeps_its_documented_constructor(self):
        """The Upload tab's tests build one from exactly these fields."""
        preview = AudioFilePreview(
            file_path="/tmp/a.wav",
            file_name="a.wav",
            file_size_mb=1.0,
            duration_seconds=60.0,
            sample_rate=44100,
            channels=2,
            needs_splitting=False,
            estimated_chunks=1,
        )

        assert preview.chunk_durations == []
        assert preview.duration_formatted
        assert preview.file_size_formatted


class TestMovingAverage:
    """The boxcar that replaced ``np.convolve`` in ``_find_split_points``.

    ``np.convolve`` is the reference implementation here, so these compare
    against it directly rather than against recorded values: the point is that
    split points cannot move, not that the numbers are any particular figure.
    """

    @pytest.mark.parametrize("size", [1, 2, 5, 17, 100, 1000, 4096, 50000])
    @pytest.mark.parametrize("window", [1, 2, 3, 4, 7, 50, 101, 999, 4410])
    def test_matches_numpy_convolve(self, size, window):
        if window > size:
            pytest.skip("window longer than the signal")
        rng = np.random.default_rng(size * 1000 + window)
        samples = rng.random(size).astype(np.float32)

        expected = np.convolve(samples, np.ones(window) / window, mode="same")
        actual = _moving_average(samples, window)

        assert actual.shape == expected.shape
        assert actual.dtype == np.float32
        assert np.allclose(actual, expected, atol=1e-6)

    def test_matches_across_block_boundaries(self):
        """The blockwise halo must not leave a seam every 4M samples."""
        window = 4410
        rng = np.random.default_rng(7)
        samples = rng.random(_SMOOTH_BLOCK_SAMPLES * 2 + 12345).astype(np.float32)

        expected = np.convolve(samples, np.ones(window) / window, mode="same")
        actual = _moving_average(samples, window)

        assert np.allclose(actual, expected, atol=1e-6)

    def test_stays_far_below_the_silence_threshold(self):
        """What the accuracy has to be good enough for."""
        window = 4410
        rng = np.random.default_rng(11)
        samples = (np.abs(rng.standard_normal(200000)) / 4).astype(np.float32)

        expected = np.convolve(samples, np.ones(window) / window, mode="same")
        drift = float(np.max(np.abs(_moving_average(samples, window) - expected)))

        assert drift < config.SILENCE_THRESHOLD / 1000

    def test_degenerate_inputs_are_passed_through(self):
        assert _moving_average(np.zeros(0, dtype=np.float32), 4410).size == 0
        one = np.array([0.25], dtype=np.float32)
        assert _moving_average(one, 1)[0] == pytest.approx(0.25)

    def test_split_points_are_unchanged_by_the_faster_smoothing(self, processor):
        """End to end: the same audio must still split in the same places."""
        rate = 44100
        rng = np.random.default_rng(3)
        loud = (rng.standard_normal(rate * 40) * 6000).astype(np.int16)
        quiet = np.zeros(rate, dtype=np.int16)
        audio = np.concatenate([loud, quiet, loud, quiet, loud])

        original = processor._find_split_points

        def with_convolve(data, sample_rate):
            import services.audio_processor as module

            saved = module._moving_average
            module._moving_average = lambda x, w: (
                np.convolve(x, np.ones(w) / w, mode="same").astype(np.float32)
                if w > 1 else x
            )
            try:
                return original(data, sample_rate)
            finally:
                module._moving_average = saved

        assert processor._find_split_points(audio, rate) == with_convolve(audio, rate)
