"""
Unit tests for the hotkey manager debounce behavior.
"""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "hotkey_manager.py"
MODULE_SPEC = importlib.util.spec_from_file_location("test_hotkey_manager_module", MODULE_PATH)
hotkey_manager_module = importlib.util.module_from_spec(MODULE_SPEC)
assert MODULE_SPEC.loader is not None
keyboard_stub = types.SimpleNamespace(
    KEY_DOWN="down",
    hook=lambda *args, **kwargs: None,
    is_pressed=lambda *args, **kwargs: False,
    unhook_all=lambda: None,
)
config_stub = types.SimpleNamespace(
    config=types.SimpleNamespace(
        DEFAULT_HOTKEYS={
            "record_toggle": "kp *",
            "cancel": "kp -",
            "enable_disable": "ctrl+alt+kp *",
        },
        HOTKEY_DEBOUNCE_MS=300,
    )
)
with patch.dict(sys.modules, {"keyboard": keyboard_stub, "config": config_stub}):
    MODULE_SPEC.loader.exec_module(hotkey_manager_module)

HotkeyManager = hotkey_manager_module.HotkeyManager


class TestHotkeyManager(unittest.TestCase):
    """Test cases for the HotkeyManager class."""

    @patch.object(HotkeyManager, "_setup_keyboard_hook")
    def test_record_toggle_uses_monotonic_time(self, _mock_setup_keyboard_hook):
        """Record toggle debounce should not depend on wall-clock jumps."""
        manager = HotkeyManager()

        with patch.object(hotkey_manager_module.time, "monotonic", side_effect=[100.0, 100.05, 100.4]):
            self.assertTrue(manager._should_trigger_record_toggle())
            self.assertFalse(manager._should_trigger_record_toggle())
            self.assertTrue(manager._should_trigger_record_toggle())

    @patch.object(HotkeyManager, "_setup_keyboard_hook")
    def test_enable_toggle_clears_debounce_state(self, _mock_setup_keyboard_hook):
        """Enable/disable should allow the next record toggle immediately."""
        manager = HotkeyManager()

        with patch.object(hotkey_manager_module.time, "monotonic", return_value=100.0):
            self.assertTrue(manager._should_trigger_record_toggle())

        manager._toggle_program_enabled()
        self.assertIsNone(manager._last_trigger_time)

        with patch.object(hotkey_manager_module.time, "monotonic", return_value=100.01):
            self.assertTrue(manager._should_trigger_record_toggle())


    @patch.object(HotkeyManager, "_setup_keyboard_hook")
    def test_rehook_preserves_state(self, _mock_setup):
        """rehook() should re-register hook without changing hotkeys or enabled state."""
        manager = HotkeyManager()
        manager.program_enabled = False
        original_hotkeys = manager.hotkeys.copy()
        callback = lambda: None
        manager.on_record_toggle = callback

        with patch.object(manager, 'cleanup') as mock_cleanup:
            manager.rehook()
            mock_cleanup.assert_called_once()

        # _setup_keyboard_hook: once in __init__, once in rehook
        self.assertEqual(_mock_setup.call_count, 2)
        # State preserved
        self.assertFalse(manager.program_enabled)
        self.assertEqual(manager.hotkeys, original_hotkeys)
        self.assertIs(manager.on_record_toggle, callback)


if __name__ == "__main__":
    unittest.main()
