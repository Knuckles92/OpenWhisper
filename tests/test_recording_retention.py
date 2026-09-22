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
    resolve_max_saved_recordings_bytes,
)

MB = 1024 * 1024


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

    def test_size_limit_disables_count(self):
        """Folder-size mode should not also apply the count limit."""
        settings = {
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.SIZE_LIMIT,
            SettingsKey.MAX_SAVED_RECORDINGS: 5,
        }
        assert resolve_max_saved_recordings(settings) is None


class TestResolveMaxSavedRecordingsBytes:
    def test_default_has_no_size_limit(self):
        """Missing settings keep the count default, so no size cap applies."""
        assert resolve_max_saved_recordings_bytes({}) is None

    def test_other_modes_have_no_size_limit(self):
        for mode in (RecordingRetentionMode.KEEP_ALL, RecordingRetentionMode.CUSTOM):
            settings = {
                SettingsKey.RECORDING_RETENTION_MODE: mode,
                SettingsKey.MAX_SAVED_RECORDINGS_MB: 50,
            }
            assert resolve_max_saved_recordings_bytes(settings) is None

    def test_size_limit_converts_megabytes(self):
        settings = {
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.SIZE_LIMIT,
            SettingsKey.MAX_SAVED_RECORDINGS_MB: 250,
        }
        assert resolve_max_saved_recordings_bytes(settings) == 250 * MB

    def test_size_limit_defaults_to_config(self):
        settings = {
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.SIZE_LIMIT,
        }
        assert (
            resolve_max_saved_recordings_bytes(settings)
            == config.MAX_SAVED_RECORDINGS_MB * MB
        )

    def test_invalid_size_falls_back_to_config(self):
        settings = {
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.SIZE_LIMIT,
            SettingsKey.MAX_SAVED_RECORDINGS_MB: "big",
        }
        assert (
            resolve_max_saved_recordings_bytes(settings)
            == config.MAX_SAVED_RECORDINGS_MB * MB
        )

    def test_size_clamps_to_at_least_one_megabyte(self):
        settings = {
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.SIZE_LIMIT,
            SettingsKey.MAX_SAVED_RECORDINGS_MB: -3,
        }
        assert resolve_max_saved_recordings_bytes(settings) == MB


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

    def _touch_recording(self, stamp: str, size: int = 4) -> str:
        path = os.path.join(self.recordings_dir, f"recording_{stamp}.wav")
        with open(path, "wb") as handle:
            handle.write(b"R" * size)
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
    def test_set_retention_applies_immediately(self, _mock_db):
        """Lowering the limit via set_retention should rotate now."""
        manager = HistoryManager(
            recordings_folder=self.recordings_dir,
            max_recordings=None,
        )
        self._touch_recording("20260101_120000")
        self._touch_recording("20260102_120000")
        self._touch_recording("20260103_120000")

        manager.set_retention(1)

        remaining = os.listdir(self.recordings_dir)
        assert remaining == ["recording_20260103_120000.wav"]

    @patch("services.history_manager.db")
    def test_size_limit_keeps_newest_that_fit(self, mock_db):
        """A folder-size cap should delete oldest files until the rest fit."""
        manager = HistoryManager(
            recordings_folder=self.recordings_dir,
            max_bytes=250,
        )
        self._touch_recording("20260101_120000", size=100)
        self._touch_recording("20260102_120000", size=100)
        self._touch_recording("20260103_120000", size=100)
        self._touch_recording("20260104_120000", size=100)

        manager._rotate_recordings()

        remaining = sorted(os.listdir(self.recordings_dir))
        assert remaining == [
            "recording_20260103_120000.wav",
            "recording_20260104_120000.wav",
        ]
        cleared = sorted(
            call.args[0] for call in mock_db.clear_history_audio_file.call_args_list
        )
        assert cleared == [
            "recording_20260101_120000.wav",
            "recording_20260102_120000.wav",
        ]

    @patch("services.history_manager.db")
    def test_size_limit_exact_fit_keeps_everything(self, _mock_db):
        manager = HistoryManager(
            recordings_folder=self.recordings_dir,
            max_bytes=300,
        )
        self._touch_recording("20260101_120000", size=100)
        self._touch_recording("20260102_120000", size=100)
        self._touch_recording("20260103_120000", size=100)

        manager._rotate_recordings()

        assert len(os.listdir(self.recordings_dir)) == 3

    @patch("services.history_manager.db")
    def test_size_limit_always_keeps_newest(self, _mock_db):
        """A recording bigger than the cap should survive if it is the newest."""
        manager = HistoryManager(
            recordings_folder=self.recordings_dir,
            max_bytes=50,
        )
        self._touch_recording("20260101_120000", size=10)
        self._touch_recording("20260102_120000", size=200)

        manager._rotate_recordings()

        assert os.listdir(self.recordings_dir) == ["recording_20260102_120000.wav"]

    @patch("services.history_manager.db")
    def test_count_and_size_limits_both_apply(self, _mock_db):
        manager = HistoryManager(
            recordings_folder=self.recordings_dir,
            max_recordings=3,
            max_bytes=1000,
        )
        for day in range(1, 6):
            self._touch_recording(f"2026010{day}_120000", size=100)

        manager._rotate_recordings()

        assert sorted(os.listdir(self.recordings_dir)) == [
            "recording_20260103_120000.wav",
            "recording_20260104_120000.wav",
            "recording_20260105_120000.wav",
        ]

    @patch("services.history_manager.db")
    def test_set_retention_switches_to_size_limit(self, _mock_db):
        manager = HistoryManager(
            recordings_folder=self.recordings_dir,
            max_recordings=10,
        )
        self._touch_recording("20260101_120000", size=100)
        self._touch_recording("20260102_120000", size=100)
        self._touch_recording("20260103_120000", size=100)

        manager.set_retention(max_bytes=150)

        assert manager.max_recordings is None
        assert os.listdir(self.recordings_dir) == ["recording_20260103_120000.wav"]

    @patch("services.history_manager.db")
    def test_saving_rotates_by_size(self, _mock_db):
        manager = HistoryManager(
            recordings_folder=self.recordings_dir,
            max_bytes=150,
        )
        self._touch_recording("20260101_120000", size=100)
        source = os.path.join(self.temp_dir, "new.wav")
        with open(source, "wb") as handle:
            handle.write(b"N" * 100)

        saved = manager._save_recording(source)
        os.remove(source)

        assert os.listdir(self.recordings_dir) == [saved]

    @patch("services.history_manager.settings_manager")
    def test_explicit_limit_does_not_read_settings(self, mock_settings):
        manager = HistoryManager(
            recordings_folder=self.recordings_dir,
            max_recordings=4,
        )

        assert manager.max_recordings == 4
        assert manager.max_bytes is None
        mock_settings.load_all_settings.assert_not_called()

    @patch("services.history_manager.settings_manager")
    def test_omitted_limits_read_saved_size_policy(self, mock_settings):
        mock_settings.load_all_settings.return_value = {
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.SIZE_LIMIT,
            SettingsKey.MAX_SAVED_RECORDINGS_MB: 64,
        }

        manager = HistoryManager(recordings_folder=self.recordings_dir)

        assert manager.max_recordings is None
        assert manager.max_bytes == 64 * MB
        mock_settings.load_all_settings.assert_called_once()

    @patch("services.history_manager.db")
    def test_recordings_over_limit_previews_without_deleting(self, mock_db):
        """The preview should name what rotation deletes, and delete nothing."""
        manager = HistoryManager(
            recordings_folder=self.recordings_dir,
            max_recordings=None,
        )
        self._touch_recording("20260101_120000", size=100)
        self._touch_recording("20260102_120000", size=100)
        self._touch_recording("20260103_120000", size=100)

        preview = manager.recordings_over_limit(max_bytes=150)

        assert [rec.filename for rec in preview] == [
            "recording_20260102_120000.wav",
            "recording_20260101_120000.wav",
        ]
        assert len(os.listdir(self.recordings_dir)) == 3
        mock_db.clear_history_audio_file.assert_not_called()

        manager.set_retention(max_bytes=150)
        assert os.listdir(self.recordings_dir) == ["recording_20260103_120000.wav"]

    def test_recordings_usage_totals_saved_audio(self):
        manager = HistoryManager(
            recordings_folder=self.recordings_dir,
            max_recordings=None,
        )
        self._touch_recording("20260101_120000", size=30)
        self._touch_recording("20260102_120000", size=70)

        assert manager.get_recordings_usage() == (2, 100)

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

    def test_save_and_resolve_size_limit(self):
        """Persisted folder-size retention should resolve to a byte cap."""
        self.manager.save_all_settings({
            SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.SIZE_LIMIT,
            SettingsKey.MAX_SAVED_RECORDINGS: 15,
            SettingsKey.MAX_SAVED_RECORDINGS_MB: 512,
        })
        loaded = self.manager.load_all_settings()
        assert resolve_max_saved_recordings(loaded) is None
        assert resolve_max_saved_recordings_bytes(loaded) == 512 * MB

