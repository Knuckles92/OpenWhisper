"""
Unit tests for the settings module.
"""
import unittest
import tempfile
import os
import json
from unittest.mock import patch

from services.settings import SettingsManager
from config import config


class TestSettingsManager(unittest.TestCase):
    """Test cases for the SettingsManager class."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.test_settings_file = os.path.join(self.temp_dir, "test_settings.json")
        self.settings_manager = SettingsManager(self.test_settings_file)

    def tearDown(self):
        """Clean up test fixtures."""
        if os.path.exists(self.test_settings_file):
            os.remove(self.test_settings_file)
        os.rmdir(self.temp_dir)

    def test_load_hotkey_settings_default(self):
        """Test loading default hotkey settings when file doesn't exist."""
        hotkeys = self.settings_manager.load_hotkey_settings()
        self.assertEqual(hotkeys, config.DEFAULT_HOTKEYS)

    def test_save_and_load_hotkey_settings(self):
        """Test saving and loading hotkey settings."""
        test_hotkeys = {
            'record_toggle': 'f1',
            'cancel': 'f2',
            'enable_disable': 'ctrl+f3'
        }

        # Save settings
        self.settings_manager.save_hotkey_settings(test_hotkeys)

        # Load settings
        loaded_hotkeys = self.settings_manager.load_hotkey_settings()
        self.assertEqual(loaded_hotkeys, test_hotkeys)

    def test_load_hotkey_settings_partial(self):
        """Test loading hotkey settings with partial data."""
        # Create partial settings file
        partial_settings = {
            'hotkeys': {
                'record_toggle': 'f1'
                # Missing other keys
            }
        }

        with open(self.test_settings_file, 'w') as f:
            json.dump(partial_settings, f)

        # Should return the partial data, not defaults
        loaded_hotkeys = self.settings_manager.load_hotkey_settings()
        self.assertEqual(loaded_hotkeys, {'record_toggle': 'f1'})

    def test_save_hotkey_settings_invalid_file(self):
        """Test saving hotkey settings with invalid file path."""
        invalid_manager = SettingsManager("/invalid/path/settings.json")

        with self.assertRaises(Exception):
            invalid_manager.save_hotkey_settings({'test': 'value'})

    def test_load_all_settings(self):
        """Test loading all settings from file."""
        test_settings = {
            'hotkeys': {'record_toggle': 'f1'},
            'other_setting': 'value'
        }

        with open(self.test_settings_file, 'w') as f:
            json.dump(test_settings, f)

        loaded_settings = self.settings_manager.load_all_settings()
        self.assertEqual(loaded_settings, test_settings)

    def test_load_all_settings_empty(self):
        """Test loading all settings when file doesn't exist."""
        loaded_settings = self.settings_manager.load_all_settings()
        self.assertEqual(loaded_settings, {})

    def test_save_all_settings(self):
        """Test saving all settings."""
        test_settings = {
            'hotkeys': {'record_toggle': 'f1'},
            'window_size': '400x300'
        }

        self.settings_manager.save_all_settings(test_settings)

        # Verify file was created and contains correct data
        with open(self.test_settings_file, 'r') as f:
            saved_data = json.load(f)

        self.assertEqual(saved_data, test_settings)

    def test_is_hf_hub_offline_env_set(self):
        """Env helper should reflect the externally supplied HF_HUB_OFFLINE."""
        from services.settings import is_hf_hub_offline_env_set

        previous = os.environ.pop("HF_HUB_OFFLINE", None)
        try:
            self.assertFalse(is_hf_hub_offline_env_set())
            os.environ["HF_HUB_OFFLINE"] = "1"
            self.assertTrue(is_hf_hub_offline_env_set())
            os.environ["HF_HUB_OFFLINE"] = "0"
            self.assertFalse(is_hf_hub_offline_env_set())
        finally:
            if previous is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = previous

    def test_hf_access_policy_defaults_to_ask(self):
        """A new installation (no settings file) should default to 'ask'."""
        from services.settings import HuggingFaceAccessPolicy

        policy = self.settings_manager.load_hf_access_policy()
        self.assertEqual(policy, HuggingFaceAccessPolicy.ASK)
        # No settings file should be created just by reading the default
        self.assertFalse(os.path.exists(self.test_settings_file))

    def test_hf_access_policy_migrates_legacy_offline_true_to_never(self):
        """Legacy hf_hub_offline=true should migrate to the 'never' policy."""
        from services.settings import HuggingFaceAccessPolicy, SettingsKey

        self.settings_manager.save_all_settings({SettingsKey.HF_HUB_OFFLINE: True})
        policy = self.settings_manager.load_hf_access_policy()
        self.assertEqual(policy, HuggingFaceAccessPolicy.NEVER)

        # Migration should be persisted and the legacy key removed
        stored = self.settings_manager.load_all_settings()
        self.assertEqual(
            stored.get(SettingsKey.HF_ACCESS_POLICY), HuggingFaceAccessPolicy.NEVER
        )
        self.assertNotIn(SettingsKey.HF_HUB_OFFLINE, stored)

    def test_hf_access_policy_migrates_legacy_offline_false_to_ask(self):
        """Legacy hf_hub_offline=false should migrate to 'ask' for existing installs."""
        from services.settings import HuggingFaceAccessPolicy, SettingsKey

        self.settings_manager.save_all_settings({SettingsKey.HF_HUB_OFFLINE: False})
        policy = self.settings_manager.load_hf_access_policy()
        self.assertEqual(policy, HuggingFaceAccessPolicy.ASK)

        stored = self.settings_manager.load_all_settings()
        self.assertEqual(
            stored.get(SettingsKey.HF_ACCESS_POLICY), HuggingFaceAccessPolicy.ASK
        )
        self.assertNotIn(SettingsKey.HF_HUB_OFFLINE, stored)

    def test_hf_access_policy_invalid_value_falls_back_to_ask(self):
        """A corrupted policy value should fall back to 'ask' and be repaired."""
        from services.settings import HuggingFaceAccessPolicy, SettingsKey

        self.settings_manager.save_all_settings(
            {SettingsKey.HF_ACCESS_POLICY: "yolo"}
        )
        policy = self.settings_manager.load_hf_access_policy()
        self.assertEqual(policy, HuggingFaceAccessPolicy.ASK)
        stored = self.settings_manager.load_all_settings()
        self.assertEqual(
            stored.get(SettingsKey.HF_ACCESS_POLICY), HuggingFaceAccessPolicy.ASK
        )

    def test_save_hf_access_policy_roundtrip_and_legacy_cleanup(self):
        """Saving a policy should persist it and drop the legacy key."""
        from services.settings import HuggingFaceAccessPolicy, SettingsKey

        self.settings_manager.save_all_settings({SettingsKey.HF_HUB_OFFLINE: True})
        self.settings_manager.save_hf_access_policy(HuggingFaceAccessPolicy.ALWAYS)

        self.assertEqual(
            self.settings_manager.load_hf_access_policy(),
            HuggingFaceAccessPolicy.ALWAYS,
        )
        stored = self.settings_manager.load_all_settings()
        self.assertNotIn(SettingsKey.HF_HUB_OFFLINE, stored)

    def test_save_hf_access_policy_rejects_invalid_value(self):
        """Saving an unrecognized policy value should raise ValueError."""
        with self.assertRaises(ValueError):
            self.settings_manager.save_hf_access_policy("sometimes")


if __name__ == '__main__':
    unittest.main()
