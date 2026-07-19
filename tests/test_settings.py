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


class TestTranscriptCleanupRules(unittest.TestCase):
    """Learned-rules resolution and prompt composition."""

    def setUp(self):
        from services.settings import (
            SettingsKey,
            compose_transcript_cleanup_prompt,
            resolve_transcript_cleanup_rules,
        )
        self.key = SettingsKey.TRANSCRIPT_CLEANUP_RULES
        self.resolve = resolve_transcript_cleanup_rules
        self.compose = compose_transcript_cleanup_prompt

    def test_missing_key_returns_empty_list(self):
        self.assertEqual(self.resolve({}), [])

    def test_non_list_value_returns_empty_list(self):
        self.assertEqual(self.resolve({self.key: "not a list"}), [])
        self.assertEqual(self.resolve({self.key: {"a": 1}}), [])
        self.assertEqual(self.resolve({self.key: None}), [])

    def test_entries_are_filtered_and_stripped(self):
        rules = self.resolve(
            {self.key: ["  Rule one  ", "", "   ", 42, None, "Rule two"]}
        )
        self.assertEqual(rules, ["Rule one", "Rule two"])

    def test_rules_capped_at_config_limit(self):
        rules = self.resolve(
            {self.key: [f"Rule {i}" for i in range(config.MAX_TRANSCRIPT_CLEANUP_RULES + 10)]}
        )
        self.assertEqual(len(rules), config.MAX_TRANSCRIPT_CLEANUP_RULES)

    def test_compose_without_rules_returns_base_unchanged(self):
        self.assertEqual(self.compose("Base prompt.", []), "Base prompt.")

    def test_compose_appends_numbered_rules(self):
        result = self.compose(
            "Base prompt.", ['Spell "Alex Rivera".', "Expand SCWA."]
        )
        self.assertTrue(result.startswith("Base prompt.\n\n"))
        self.assertIn("Additional user-taught rules (always apply):", result)
        self.assertIn('1. Spell "Alex Rivera".', result)
        self.assertIn("2. Expand SCWA.", result)

    def test_rules_round_trip_through_settings_file(self):
        temp_dir = tempfile.mkdtemp()
        path = os.path.join(temp_dir, "settings.json")
        try:
            manager = SettingsManager(path)
            rules = ['Always spell my name "Alex Rivera".', "Use bullet lists."]
            manager.save_setting(self.key, rules)
            self.assertEqual(self.resolve(manager.load_all_settings()), rules)
        finally:
            if os.path.exists(path):
                os.remove(path)
            os.rmdir(temp_dir)


if __name__ == '__main__':
    unittest.main()
