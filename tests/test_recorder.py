import pytest
import tempfile
import os
import wave
import numpy as np
from unittest.mock import patch, MagicMock

from services.recorder import AudioRecorder
from config import config


class TestAudioRecorder:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_audio_file = os.path.join(self.temp_dir, "test_audio.wav")

        # Mock sounddevice to avoid actual audio hardware
        self.sd_patcher = patch('services.recorder.sd.InputStream')
        self.mock_sd_stream = self.sd_patcher.start()

        self.recorder = AudioRecorder()

    @pytest.fixture(autouse=True)
    def _teardown(self):
        yield
        self.sd_patcher.stop()

        if os.path.exists(self.test_audio_file):
            os.remove(self.test_audio_file)
        os.rmdir(self.temp_dir)

        if hasattr(self.recorder, 'cleanup'):
            self.recorder.cleanup()

    def test_initialization(self):
        assert not self.recorder.is_recording
        assert not self.recorder.has_recording_data()
        assert self.recorder.chunk == config.CHUNK_SIZE
        assert self.recorder.channels == config.CHANNELS
        assert self.recorder.rate == config.SAMPLE_RATE
        assert self.recorder.dtype == config.AUDIO_FORMAT

    def test_start_recording(self):
        result = self.recorder.start_recording()
        assert result
        assert self.recorder.is_recording
        assert not self.recorder.has_recording_data()

    def test_start_recording_already_recording(self):
        self.recorder.is_recording = True
        result = self.recorder.start_recording()
        assert not result

    def test_start_recording_fails_when_stream_cannot_open(self):
        """Start must fail before Recording if the input stream cannot open."""
        self.mock_sd_stream.side_effect = Exception("Error querying device -1")
        result = self.recorder.start_recording()
        assert not result
        assert not self.recorder.is_recording
        assert self.recorder.last_start_error == "No audio device available"
        assert self.recorder.stream is None

    def test_stop_recording(self):
        self.recorder.start_recording()

        result = self.recorder.stop_recording()
        assert result
        # Note: is_recording may still be True briefly during post-roll

    def test_stop_recording_not_recording(self):
        result = self.recorder.stop_recording()
        assert not result

    def test_has_recording_data(self):
        assert not self.recorder.has_recording_data()

        audio = np.arange(16, dtype=np.int16)
        self.recorder._audio_callback(audio, len(audio), None, None)
        assert self.recorder.has_recording_data()

    def test_clear_recording_data(self):
        audio = np.arange(16, dtype=np.int16)
        self.recorder._audio_callback(audio, len(audio), None, None)

        self.recorder.clear_recording_data()
        assert not self.recorder.has_recording_data()

    def test_get_recording_duration(self):
        assert self.recorder.get_recording_duration() == 0.0

        audio = np.arange(1000, dtype=np.int16)
        self.recorder._audio_callback(audio, len(audio), None, None)
        expected_duration = len(audio) / config.SAMPLE_RATE
        assert self.recorder.get_recording_duration() == expected_duration

    def test_save_recording_no_data(self):
        result = self.recorder.save_recording(self.test_audio_file)
        assert not result
        assert not os.path.exists(self.test_audio_file)

    def test_save_recording_with_data(self):
        audio = np.arange(500, dtype=np.int16)
        self.recorder._audio_callback(audio, len(audio), None, None)

        result = self.recorder.save_recording(self.test_audio_file)

        assert result
        assert os.path.exists(self.test_audio_file)

        with wave.open(self.test_audio_file, 'rb') as wf:
            assert wf.getnchannels() == config.CHANNELS
            assert wf.getframerate() == config.SAMPLE_RATE
            assert wf.getsampwidth() == np.dtype(config.AUDIO_FORMAT).itemsize
            assert wf.readframes(len(audio)) == audio.tobytes()

    def test_save_recording_default_filename(self):
        audio = np.arange(32, dtype=np.int16)
        self.recorder._audio_callback(audio, len(audio), None, None)

        result = self.recorder.save_recording()

        assert result
        assert os.path.exists(config.RECORDED_AUDIO_FILE)

        if os.path.exists(config.RECORDED_AUDIO_FILE):
            os.remove(config.RECORDED_AUDIO_FILE)

    def test_audio_level_callback(self):
        callback_values = []

        def test_callback(level):
            callback_values.append(level)

        self.recorder.set_audio_level_callback(test_callback)
        assert self.recorder.audio_level_callback == test_callback

        test_data = np.array([1000, -1000, 2000, -2000], dtype=np.int16)
        self.recorder._calculate_and_report_level(test_data)

        assert len(callback_values) == 1
        assert isinstance(callback_values[0], float)
        assert callback_values[0] >= 0.0
        assert callback_values[0] <= 1.0

    def test_audio_callback(self):
        fake_audio = np.array([100, -100, 200, -200], dtype=np.int16)

        self.recorder._audio_callback(fake_audio, len(fake_audio), None, None)

        assert self.recorder.has_recording_data()
        assert self.recorder._recorded_bytes == len(fake_audio.tobytes())
        self.recorder._audio_spool.seek(0)
        assert self.recorder._audio_spool.read() == fake_audio.tobytes()

    def test_long_recording_spills_to_disk_and_streams_to_wav(self):
        with patch("services.recorder.SPOOL_MEMORY_LIMIT_BYTES", 64):
            recorder = AudioRecorder(output_file=self.test_audio_file)
            audio = np.arange(256, dtype=np.int16)
            recorder._audio_callback(audio, len(audio), None, None)

            assert recorder._audio_spool._rolled is True
            assert recorder.save_recording()

            with wave.open(self.test_audio_file, "rb") as wf:
                assert wf.readframes(len(audio)) == audio.tobytes()
            recorder.cleanup()


if __name__ == '__main__':
    unittest.main()
