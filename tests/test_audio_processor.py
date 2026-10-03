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

        preview = processor.preview_file(path, engine_splits=True)

        assert preview.needs_splitting is False
        assert preview.over_upload_limit is False
        assert preview.sample_rate == 44100
        assert preview.channels == 1
        assert preview.duration_seconds == pytest.approx(2.0, abs=0.05)
        assert preview.estimated_chunks == 1
        assert preview.chunk_durations == [preview.duration_seconds]

    def test_stereo_channel_count_comes_from_the_header(
        self, processor, tmp_path
    ):
        path = write_wav(tmp_path / "stereo.wav", seconds=1.0, channels=2)

        preview = processor.preview_file(path, engine_splits=False)

        assert preview.channels == 2
        assert preview.duration_seconds == pytest.approx(1.0, abs=0.05)

    def test_large_file_is_estimated_from_header_and_reports_chunks(
        self, processor, tmp_path, monkeypatch
    ):
        """Do not decode twice just to preview a file that will be split."""
        path = write_wav(tmp_path / "big.wav", seconds=3.0, rate=44100)
        monkeypatch.setattr(config, "MAX_FILE_SIZE_MB", 0.05)
        decoded = []
        original = processor._load_audio_metadata
        processor._load_audio_metadata = lambda p: (
            decoded.append(p) or original(p)
        )

        preview = processor.preview_file(path, engine_splits=True)

        assert decoded == []
        assert preview.needs_splitting is True
        assert preview.estimated_chunks >= 1
        assert sum(preview.chunk_durations) == pytest.approx(3.0, abs=0.05)

    def test_large_file_for_a_one_pass_engine_is_read_from_the_header(
        self, processor, tmp_path, monkeypatch
    ):
        """Only an engine that splits needs split points; the rest skip the decode."""
        path = write_wav(tmp_path / "big.wav", seconds=3.0, rate=44100)
        monkeypatch.setattr(config, "MAX_FILE_SIZE_MB", 0.05)

        def fail(*_):
            raise AssertionError("preview decoded a file no engine will split")

        processor._load_audio_metadata = fail

        preview = processor.preview_file(path, engine_splits=False)

        assert preview.over_upload_limit is True
        assert preview.needs_splitting is False
        assert preview.estimated_chunks == 1
        assert preview.duration_seconds == pytest.approx(3.0, abs=0.05)

    def test_header_without_duration_falls_back_to_decoding(
        self, processor, tmp_path, monkeypatch
    ):
        """A preview with no duration would be worse than a slow one."""
        import av

        path = write_wav(tmp_path / "clip.wav", seconds=1.5)
        decoded = []
        # Stubbed rather than wrapped: the fake ``av.open`` below intercepts
        # every call, including the fallback's own decode.
        processor._iter_audio_blocks = lambda p: iter([
            decoded.append(p)
            or (np.zeros(int(1.5 * 44100), dtype=np.int16), 44100, 1)
        ])

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
            processor.preview_file(str(tmp_path / "nope.wav"), engine_splits=False)

    def test_unreadable_file_raises_value_error(self, processor, tmp_path):
        """The Upload tab turns this into an inline notice, not a crash."""
        path = tmp_path / "broken.wav"
        path.write_bytes(b"not audio at all")

        with pytest.raises(ValueError):
            processor.preview_file(str(path), engine_splits=False)


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

    def test_pcm_waveform_survives_splitting_exactly(self, processor, tmp_path, monkeypatch):
        rate = 8000
        samples = np.tile(np.array([-32768, -20000, -1, 0, 1, 20000, 32767], dtype=np.int16), 4000)
        path = str(tmp_path / "pcm.wav")
        with wave.open(path, "wb") as handle:
            handle.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            handle.writeframes(samples.tobytes())
        monkeypatch.setattr(config, "MAX_FILE_SIZE_MB", 0.01)
        monkeypatch.setattr(config, "OVERLAP_DURATION_SEC", 0)
        chunks = processor.split_audio_file(path)
        decoded = []
        try:
            assert len(chunks) > 1
            for chunk in chunks:
                with wave.open(chunk, "rb") as handle:
                    assert handle.getnchannels() == 1
                    assert handle.getframerate() == rate
                    decoded.append(np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2"))
            np.testing.assert_array_equal(np.concatenate(decoded), samples)
        finally:
            processor.cleanup_temp_files()

    def test_overlap_and_header_never_exceed_upload_limit(self, processor, tmp_path, monkeypatch):
        path = write_wav(tmp_path / "overlap.wav", seconds=5, rate=8000)
        monkeypatch.setattr(config, "MAX_FILE_SIZE_MB", 0.02)
        monkeypatch.setattr(config, "OVERLAP_DURATION_SEC", 2)
        chunks = processor.split_audio_file(path)
        try:
            assert len(chunks) > 1
            assert all(os.path.getsize(chunk) <= int(0.02 * 1024 * 1024) for chunk in chunks)
            arrays = []
            for chunk in chunks:
                with wave.open(chunk, "rb") as handle:
                    arrays.append(np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2"))
            _, overlap = processor._chunk_limits(8000)
            for previous, current in zip(arrays, arrays[1:]):
                np.testing.assert_array_equal(previous[-2 * overlap:], current[:2 * overlap])
        finally:
            processor.cleanup_temp_files()

    def test_streaming_writes_before_reading_entire_input(self, processor, monkeypatch):
        """File length cannot increase the analysis/decode working buffer."""
        monkeypatch.setattr(config, "MAX_FILE_SIZE_MB", 0.02)
        monkeypatch.setattr(config, "MIN_CHUNK_DURATION_SEC", 0.1)
        monkeypatch.setattr(config, "OVERLAP_DURATION_SEC", 0.05)
        rate = 8000
        max_samples, _ = processor._chunk_limits(rate)
        block = np.full(1000, 8000, dtype=np.int16)
        saved = []
        examined = []
        original = processor._chunk_boundary

        def source(*_):
            for index in range(500):
                if index == 20:
                    assert saved, "decoder accumulated the input before writing"
                yield block, rate, 1

        def boundary(samples, *args):
            examined.append(len(samples))
            return original(samples, *args)

        monkeypatch.setattr(processor, "_iter_audio_blocks", source)
        monkeypatch.setattr(processor, "_chunk_boundary", boundary)
        monkeypatch.setattr(processor, "_save_audio_chunk", lambda samples, *_: saved.append(len(samples)))
        monkeypatch.setattr(processor, "_load_audio_metadata", lambda *_: pytest.fail("full decode"))
        processor.split_audio_file("stream")
        try:
            assert len(saved) > 40
            assert max(saved) <= max_samples
            assert examined and max(examined) <= max_samples
        finally:
            processor.cleanup_temp_files()

    def test_exact_chunk_size_does_not_emit_overlap_only_tail(self, processor, monkeypatch):
        monkeypatch.setattr(config, "MAX_FILE_SIZE_MB", 0.02)
        max_samples, _ = processor._chunk_limits(8000)
        samples = np.zeros(max_samples, dtype=np.int16)
        monkeypatch.setattr(processor, "_iter_audio_blocks", lambda *_: (item for item in [(samples, 8000, 1)]))
        chunks = processor.split_audio_file("stream")
        try:
            assert len(chunks) == 1
        finally:
            processor.cleanup_temp_files()

    def test_silence_boundary_keeps_every_sample_with_overlap(self, processor, tmp_path, monkeypatch):
        rate = 1000
        samples = np.full(9000, 12000, dtype=np.int16)
        samples[3800:4600] = 0
        path = str(tmp_path / "silence.wav")
        with wave.open(path, "wb") as handle:
            handle.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            handle.writeframes(samples.tobytes())
        monkeypatch.setattr(config, "MAX_FILE_SIZE_MB", 0.01)
        monkeypatch.setattr(config, "MIN_CHUNK_DURATION_SEC", 1)
        monkeypatch.setattr(config, "OVERLAP_DURATION_SEC", 0.1)
        monkeypatch.setattr(config, "SILENCE_DURATION_SEC", 0.3)
        chunks = processor.split_audio_file(path)
        try:
            assert len(chunks) == 2
            decoded = []
            for chunk in chunks:
                with wave.open(chunk, "rb") as handle:
                    decoded.append(np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2"))
            overlap = 100
            assert 3800 < len(decoded[0]) - overlap < 4600
            np.testing.assert_array_equal(np.concatenate([decoded[0][:-overlap], decoded[1][overlap:]]), samples)
        finally:
            processor.cleanup_temp_files()

    def test_small_minimum_duration_still_advances_past_overlap(self, processor, monkeypatch):
        monkeypatch.setattr(config, "MAX_FILE_SIZE_MB", 0.01)
        monkeypatch.setattr(config, "MIN_CHUNK_DURATION_SEC", 0)
        monkeypatch.setattr(config, "OVERLAP_DURATION_SEC", 2)
        monkeypatch.setattr(config, "SILENCE_DURATION_SEC", 0.1)
        samples = np.zeros(20000, dtype=np.int16)
        monkeypatch.setattr(processor, "_iter_audio_blocks", lambda *_: (item for item in [(samples, 1000, 1)]))
        chunks = processor.split_audio_file("stream")
        try:
            assert 1 < len(chunks) < 20
            assert all(os.path.getsize(chunk) <= int(0.01 * 1024 * 1024) for chunk in chunks)
        finally:
            processor.cleanup_temp_files()

    @pytest.mark.parametrize("cancel", [False, True])
    def test_failure_or_cancel_removes_partial_files_and_closes_decoder(
        self, processor, tmp_path, monkeypatch, cancel
    ):
        monkeypatch.setattr(config, "MAX_FILE_SIZE_MB", 0.02)
        root = tmp_path / "chunks"
        root.mkdir()
        monkeypatch.setattr("services.audio_processor.tempfile.mkdtemp", lambda **_: str(root))
        closed = []
        stop = []

        def source(*_):
            try:
                while True:
                    yield np.zeros(1000, dtype=np.int16), 8000, 1
            finally:
                closed.append(True)

        def write(_samples, _rate, filename):
            with open(filename, "wb") as handle:
                handle.write(b"partial")
            if cancel:
                stop.append(True)
            else:
                raise OSError("disk full")

        monkeypatch.setattr(processor, "_iter_audio_blocks", source)
        monkeypatch.setattr(processor, "_save_audio_chunk", write)
        with pytest.raises((OSError, RuntimeError), match="disk full|canceled"):
            processor.split_audio_file("stream", should_cancel=lambda: bool(stop))
        assert closed == [True]
        assert not root.exists()
        assert processor.temp_files == []


class TestPcmConversion:
    def test_real_stereo_keeps_duration_and_source_channels(self, processor, tmp_path):
        path = write_wav(tmp_path / "stereo.wav", seconds=1, rate=8000, channels=2)
        samples, rate, channels = processor._load_audio_metadata(path)
        assert len(samples) == rate == 8000
        assert channels == 2
        assert samples.dtype == np.int16
        # Equal stereo channels must retain their waveform after downmixing.
        t = np.arange(rate) / rate
        expected = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
        np.testing.assert_allclose(samples, expected, atol=2)

    @pytest.mark.parametrize("format_name", ["s16", "s16p", "s32", "flt", "fltp", "u8"])
    @pytest.mark.parametrize("channels", [1, 2])
    def test_integer_float_and_packed_planar_formats(
        self, processor, monkeypatch, format_name, channels
    ):
        import av

        values = np.array([-16384, -8192, 0, 8192, 16384], dtype=np.int16)
        if format_name.startswith("s16"):
            data = values
        elif format_name.startswith("s32"):
            data = values.astype(np.int32) * 65536
        elif format_name.startswith("flt"):
            data = values.astype(np.float32) / 32768
        else:
            data = (values.astype(np.int32) // 256 + 128).astype(np.uint8)
        planar = np.stack([data] if channels == 1 else [data, data[::-1]])
        shaped = planar if format_name.endswith("p") else planar.T.reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(shaped, format=format_name, layout="mono" if channels == 1 else "stereo")
        frame.sample_rate = 8000
        closed = []

        class Container:
            streams = SimpleNamespace(audio=[SimpleNamespace(rate=8000, channels=channels)])

            def __enter__(self):
                return self

            def __exit__(self, *_):
                closed.append(True)

            def decode(self, **_):
                yield frame

        monkeypatch.setattr(av, "open", lambda *_: Container())
        samples, rate, source_channels = processor._load_audio_metadata("test")
        assert rate == 8000 and source_channels == channels
        assert len(samples) == len(values)
        expected = values if channels == 1 else np.zeros_like(values)
        np.testing.assert_array_equal(samples, expected)
        assert closed == [True]


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

    @pytest.mark.parametrize("size,window", [
        (size, window)
        for window in [1, 2, 3, 4, 7, 50, 101, 999, 4410]
        for size in [1, 2, 5, 17, 100, 1000, 4096, 50000]
        if window <= size
    ])
    def test_matches_numpy_convolve(self, size, window):
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

    def test_split_points_are_unchanged_by_the_faster_smoothing(self, processor, monkeypatch):
        """End to end: the same audio must still split in the same places."""
        rate = 44100
        rng = np.random.default_rng(3)
        loud = (rng.standard_normal(rate * 40) * 6000).astype(np.int16)
        quiet = np.zeros(rate, dtype=np.int16)
        audio = np.concatenate([loud, quiet, loud, quiet, loud])
        monkeypatch.setattr(config, "MAX_FILE_SIZE_MB", 5)

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

        points = processor._find_split_points(audio, rate)
        assert points
        assert points == with_convolve(audio, rate)
