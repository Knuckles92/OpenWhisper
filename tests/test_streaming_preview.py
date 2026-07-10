"""Unit tests for streaming preview helpers and settings migration."""

import unittest

from services.settings import SettingsKey, is_streaming_overlay_enabled
from services.streaming_transcriber import append_preview_text


class TestAppendPreviewText(unittest.TestCase):
    def test_appends_with_space(self):
        self.assertEqual(append_preview_text("hello", "world"), "hello world")

    def test_ignores_empty_chunk(self):
        self.assertEqual(append_preview_text("hello", "  "), "hello")
        self.assertEqual(append_preview_text("hello", ""), "hello")

    def test_starts_from_empty(self):
        self.assertEqual(append_preview_text("", "hello"), "hello")
        self.assertEqual(append_preview_text(None, "hello"), "hello")


class TestStreamingOverlaySettingMigration(unittest.TestCase):
    def test_prefers_new_key(self):
        settings = {
            SettingsKey.STREAMING_OVERLAY_ENABLED: True,
            SettingsKey.STREAMING_PASTE_ENABLED: False,
        }
        self.assertTrue(is_streaming_overlay_enabled(settings))

    def test_falls_back_to_legacy_key(self):
        settings = {SettingsKey.STREAMING_PASTE_ENABLED: True}
        self.assertTrue(is_streaming_overlay_enabled(settings))

    def test_defaults_false(self):
        self.assertFalse(is_streaming_overlay_enabled({}))


if __name__ == "__main__":
    unittest.main()
