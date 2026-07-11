"""
Unit tests for saved-recording retention settings and rotation.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from config import config
from services.history_manager import HistoryManager
from services.settings import (
    RecordingRetentionMode,
    SettingsKey,
    SettingsManager,
    resolve_max_saved_recordings,
)


class TestResolveMaxSavedRecordings(unittest.TestCase):
    """Tests for resolve_max_saved_recordings()."""

    def test_default_is_custom_config_limit(self):
        """Missing settings should use the config custom default."""
        self.assertEqual(resolve_max_saved_recordings({}), config.MAX_SAVED_RECORDINGS)

    def test_keep_all_returns_none(self):
        """Keep-all mode should disable the retention limit."""
        settings = {
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.KEEP_ALL,
            SettingsKey.MAX_SAVED_RECORDINGS: 5,
        }
        self.assertIsNone(resolve_max_saved_recordings(settings))

    def test_custom_uses_count(self):
        """Custom mode should use the configured count."""
        settings = {
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.CUSTOM,
            SettingsKey.MAX_SAVED_RECORDINGS: 7,
        }
        self.assertEqual(resolve_max_saved_recordings(settings), 7)

    def test_custom_clamps_to_at_least_one(self):
        """Custom counts below 1 should clamp to 1."""
        settings = {
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.CUSTOM,
            SettingsKey.MAX_SAVED_RECORDINGS: 0,
        }
        self.assertEqual(resolve_max_saved_recordings(settings), 1)

    def test_invalid_count_falls_back_to_config(self):
        """Non-integer custom counts should fall back to config."""
        settings = {
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.CUSTOM,
            SettingsKey.MAX_SAVED_RECORDINGS: "nope",
        }
        self.assertEqual(resolve_max_saved_recordings(settings), config.MAX_SAVED_RECORDINGS)


class TestRecordingRotation(unittest.TestCase):
    """Tests for HistoryManager recording rotation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.recordings_dir = os.path.join(self.temp_dir, "recordings")
        os.makedirs(self.recordings_dir)

    def tearDown(self):
        for name in os.listdir(self.recordings_dir):
            os.remove(os.path.join(self.recordings_dir, name))
        os.rmdir(self.recordings_dir)
        os.rmdir(self.temp_dir)

    def _touch_recording(self, stamp: str) -> str:
        path = os.path.join(self.recordings_dir, f"recording_{stamp}.wav")
        with open(path, "wb") as handle:
            handle.write(b"RIFF")
        return path

    @patch("services.history_manager.db")
    def test_rotate_keeps_newest_n(self, _mock_db):
        """Custom limit should delete oldest files beyond the max."""
        manager = HistoryManager(
            recordings_folder=self.recordings_dir,
            max_recordings=2,
        )
        self._touch_recording("20260101_120000")
        self._touch_recording("20260102_120000")
        self._touch_recording("20260103_120000")

        manager._rotate_recordings()

        remaining = sorted(os.listdir(self.recordings_dir))
        self.assertEqual(
            remaining,
            ["recording_20260102_120000.wav", "recording_20260103_120000.wav"],
        )

    @patch("services.history_manager.db")
    def test_keep_all_skips_rotation(self, _mock_db):
        """Unlimited retention should leave every recording on disk."""
        manager = HistoryManager(
            recordings_folder=self.recordings_dir,
            max_recordings=None,
        )
        self._touch_recording("20260101_120000")
        self._touch_recording("20260102_120000")
        self._touch_recording("20260103_120000")

        manager._rotate_recordings()

        self.assertEqual(len(os.listdir(self.recordings_dir)), 3)

    @patch("services.history_manager.db")
    def test_set_max_recordings_applies_immediately(self, _mock_db):
        """Lowering the limit via set_max_recordings should rotate now."""
        manager = HistoryManager(
            recordings_folder=self.recordings_dir,
            max_recordings=None,
        )
        self._touch_recording("20260101_120000")
        self._touch_recording("20260102_120000")
        self._touch_recording("20260103_120000")

        manager.set_max_recordings(1)

        remaining = os.listdir(self.recordings_dir)
        self.assertEqual(remaining, ["recording_20260103_120000.wav"])


class TestRecordingRetentionPersistence(unittest.TestCase):
    """Settings file round-trip for retention keys."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.settings_file = os.path.join(self.temp_dir, "settings.json")
        self.manager = SettingsManager(self.settings_file)

    def tearDown(self):
        if os.path.exists(self.settings_file):
            os.remove(self.settings_file)
        os.rmdir(self.temp_dir)

    def test_save_and_resolve_custom(self):
        """Persisted custom retention should resolve to the saved count."""
        self.manager.save_all_settings({
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.CUSTOM,
            SettingsKey.MAX_SAVED_RECORDINGS: 15,
        })
        loaded = self.manager.load_all_settings()
        self.assertEqual(resolve_max_saved_recordings(loaded), 15)

    def test_save_and_resolve_keep_all(self):
        """Persisted keep-all retention should resolve to None."""
        self.manager.save_all_settings({
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.KEEP_ALL,
            SettingsKey.MAX_SAVED_RECORDINGS: 15,
        })
        loaded = self.manager.load_all_settings()
        self.assertIsNone(resolve_max_saved_recordings(loaded))


if __name__ == "__main__":
    unittest.main()
