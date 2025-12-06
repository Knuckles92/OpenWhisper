"""
Unit tests for the recording service module.
"""
import unittest
import tempfile
import os
import wave
import numpy as np
from unittest.mock import patch, MagicMock

from services.recording_service import RecordingService
from config import config


class TestRecordingService(unittest.TestCase):
    """Test cases for the RecordingService class."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.test_audio_file = os.path.join(self.temp_dir, "test_audio.wav")

        # Mock sounddevice to avoid actual audio hardware
        self.sd_patcher = patch('services.recording_service.sd.InputStream')
        self.mock_sd_stream = self.sd_patcher.start()

        # Create service instance
        self.service = RecordingService()

    def tearDown(self):
        """Clean up test fixtures."""
        self.sd_patcher.stop()

        if os.path.exists(self.test_audio_file):
            os.remove(self.test_audio_file)
        if os.path.exists(self.temp_dir):
            os.rmdir(self.temp_dir)

        if hasattr(self.service, 'cleanup'):
            self.service.cleanup()

    def test_initialization(self):
        """Test service initialization."""
        self.assertFalse(self.service.is_recording)
        self.assertEqual(self.service._frames, [])
        self.assertEqual(self.service._chunk, config.CHUNK_SIZE)
        self.assertEqual(self.service._channels, config.CHANNELS)
        self.assertEqual(self.service._rate, config.SAMPLE_RATE)
        self.assertEqual(self.service._dtype, config.AUDIO_FORMAT)

    def test_start_recording(self):
        """Test starting recording."""
        result = self.service.start_recording()
        self.assertTrue(result)
        self.assertTrue(self.service.is_recording)
        self.assertEqual(self.service._frames, [])

    def test_start_recording_already_recording(self):
        """Test starting recording when already recording."""
        self.service._is_recording = True
        result = self.service.start_recording()
        self.assertFalse(result)

    def test_stop_recording_not_recording(self):
        """Test stopping recording when not recording."""
        result = self.service.stop_recording()
        self.assertFalse(result)

    def test_get_recording_duration(self):
        """Test getting recording duration."""
        # No data initially
        self.assertEqual(self.service.get_recording_duration(), 0.0)

        # Add fake frames
        self.service._frames = [b'x' * 100] * 10  # 10 frames
        expected_duration = (10 * config.CHUNK_SIZE) / config.SAMPLE_RATE
        self.assertEqual(self.service.get_recording_duration(), expected_duration)

    def test_save_recording_no_data(self):
        """Test saving recording with no data."""
        result = self.service._save_recording(self.test_audio_file)
        self.assertFalse(result)
        self.assertFalse(os.path.exists(self.test_audio_file))

    def test_save_recording_with_data(self):
        """Test saving recording with data."""
        # Add fake audio data
        fake_data = b'fake_audio_data_chunk'
        self.service._frames = [fake_data] * 5

        # Save to actual file
        result = self.service._save_recording(self.test_audio_file)

        self.assertTrue(result)
        self.assertTrue(os.path.exists(self.test_audio_file))

        # Verify WAV file parameters
        with wave.open(self.test_audio_file, 'rb') as wf:
            self.assertEqual(wf.getnchannels(), config.CHANNELS)
            self.assertEqual(wf.getframerate(), config.SAMPLE_RATE)
            self.assertEqual(wf.getsampwidth(), np.dtype(config.AUDIO_FORMAT).itemsize)

    def test_audio_callback(self):
        """Test the audio callback function."""
        # Create fake numpy audio data
        fake_audio = np.array([100, -100, 200, -200], dtype=np.int16)

        # Call the audio callback
        self.service._audio_callback(fake_audio, len(fake_audio), None, None)

        # Should have stored one frame
        self.assertEqual(len(self.service._frames), 1)
        self.assertEqual(self.service._frames[0], fake_audio.tobytes())

    def test_cancel_recording(self):
        """Test canceling recording."""
        # Start recording
        self.service._is_recording = True
        self.service._frames = [b'some_data']

        # Cancel
        result = self.service.cancel_recording()

        self.assertTrue(result)
        self.assertEqual(self.service._frames, [])

    def test_cancel_recording_not_recording(self):
        """Test canceling when not recording."""
        result = self.service.cancel_recording()
        self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()
