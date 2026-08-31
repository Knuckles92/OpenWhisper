"""
Unit tests for saved-recording retention settings and rotation.
"""
import pytest
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from config import config
from services.history_manager import HistoryManager
from services.settings import (
    RecordingRetentionMode,
    SettingsKey,
    SettingsManager,
    resolve_max_saved_recordings,
)


class TestResolveMaxSavedRecordings:
    def test_default_is_custom_config_limit(self):
        """Missing settings should use the config custom default."""
        assert resolve_max_saved_recordings({}) == config.MAX_SAVED_RECORDINGS

    def test_keep_all_returns_none(self):
        """Keep-all mode should disable the retention limit."""
        settings = {
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.KEEP_ALL,
            SettingsKey.MAX_SAVED_RECORDINGS: 5,
        }
        assert resolve_max_saved_recordings(settings) is None

    def test_custom_uses_count(self):
        """Custom mode should use the configured count."""
        settings = {
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.CUSTOM,
            SettingsKey.MAX_SAVED_RECORDINGS: 7,
        }
        assert resolve_max_saved_recordings(settings) == 7

    def test_custom_clamps_to_at_least_one(self):
        """Custom counts below 1 should clamp to 1."""
        settings = {
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.CUSTOM,
            SettingsKey.MAX_SAVED_RECORDINGS: 0,
        }
        assert resolve_max_saved_recordings(settings) == 1

    def test_invalid_count_falls_back_to_config(self):
        """Non-integer custom counts should fall back to config."""
        settings = {
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.CUSTOM,
            SettingsKey.MAX_SAVED_RECORDINGS: "nope",
        }
        assert resolve_max_saved_recordings(settings) == config.MAX_SAVED_RECORDINGS


class TestRecordingRotation:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.temp_dir = tempfile.mkdtemp()
        self.recordings_dir = os.path.join(self.temp_dir, "recordings")
        os.makedirs(self.recordings_dir)

    @pytest.fixture(autouse=True)
    def _teardown(self):
        yield
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
        assert remaining == ["recording_20260102_120000.wav", "recording_20260103_120000.wav"]

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

        assert len(os.listdir(self.recordings_dir)) == 3

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
        assert remaining == ["recording_20260103_120000.wav"]

    @patch("services.history_manager.db")
    @patch("services.history_manager.datetime")
    def test_same_second_recordings_never_overwrite_each_other(
        self, mock_datetime, _mock_db
    ):
        mock_datetime.now.return_value.strftime.return_value = "20260103_120000"
        manager = HistoryManager(
            recordings_folder=self.recordings_dir,
            max_recordings=None,
        )
        first_source = os.path.join(self.temp_dir, "first.wav")
        second_source = os.path.join(self.temp_dir, "second.wav")
        with open(first_source, "wb") as handle:
            handle.write(b"first")
        with open(second_source, "wb") as handle:
            handle.write(b"second")

        first_name = manager._save_recording(first_source)
        second_name = manager._save_recording(second_source)

        assert first_name == "recording_20260103_120000.wav"
        assert second_name == "recording_20260103_120000-2.wav"
        with open(os.path.join(self.recordings_dir, first_name), "rb") as handle:
            assert handle.read() == b"first"
        with open(os.path.join(self.recordings_dir, second_name), "rb") as handle:
            assert handle.read() == b"second"
        os.remove(first_source)
        os.remove(second_source)


class TestHistoryEntryDeletion:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.temp_dir = tempfile.mkdtemp()
        self.recordings_dir = os.path.join(self.temp_dir, "recordings")
        os.makedirs(self.recordings_dir)
        self.audio_filename = "recording_20260101_120000.wav"
        self.audio_path = os.path.join(self.recordings_dir, self.audio_filename)
        with open(self.audio_path, "wb") as handle:
            handle.write(b"RIFF")

    @pytest.fixture(autouse=True)
    def _teardown(self):
        yield
        if os.path.exists(self.audio_path):
            os.remove(self.audio_path)
        os.rmdir(self.recordings_dir)
        os.rmdir(self.temp_dir)

    @patch("services.history_manager.db")
    def test_delete_entry_keeps_audio_by_default(self, mock_db):
        manager = HistoryManager(
            recordings_folder=self.recordings_dir,
            max_recordings=None,
        )
        mock_db.delete_history_entry.return_value = True

        assert manager.delete_entry("entry-test-id")

        assert os.path.exists(self.audio_path)
        mock_db.get_history_entry_by_id.assert_not_called()
        mock_db.clear_history_audio_file.assert_not_called()

    @patch("services.history_manager.db")
    def test_delete_entry_can_delete_attached_audio(self, mock_db):
        manager = HistoryManager(
            recordings_folder=self.recordings_dir,
            max_recordings=None,
        )
        mock_db.get_history_entry_by_id.return_value = SimpleNamespace(
            audio_file=self.audio_filename
        )
        mock_db.delete_history_entry.return_value = True

        assert manager.delete_entry(
                "entry-test-id",
                delete_audio_file=True,
            )

        assert not os.path.exists(self.audio_path)
        mock_db.clear_history_audio_file.assert_called_once_with(
            self.audio_filename
        )


class TestRecordingRetentionPersistence:
    """Settings file round-trip for retention keys."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.temp_dir = tempfile.mkdtemp()
        self.settings_file = os.path.join(self.temp_dir, "settings.json")
        self.manager = SettingsManager(self.settings_file)

    @pytest.fixture(autouse=True)
    def _teardown(self):
        yield
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
        assert resolve_max_saved_recordings(loaded) == 15

    def test_save_and_resolve_keep_all(self):
        """Persisted keep-all retention should resolve to None."""
        self.manager.save_all_settings({
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.KEEP_ALL,
            SettingsKey.MAX_SAVED_RECORDINGS: 15,
        })
        loaded = self.manager.load_all_settings()
        assert resolve_max_saved_recordings(loaded) is None

