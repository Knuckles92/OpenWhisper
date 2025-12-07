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


if __name__ == '__main__':
    unittest.main() 