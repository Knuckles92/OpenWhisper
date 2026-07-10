"""Unit tests for streaming preview helpers and settings migration."""

import unittest

from services.settings import SettingsKey, is_streaming_overlay_enabled
from services.streaming_transcriber import append_preview_text, typing_delta


class TestAppendPreviewText(unittest.TestCase):
    def test_appends_with_space(self):
        self.assertEqual(append_preview_text("hello", "world"), "hello world")

    def test_ignores_empty_chunk(self):
        self.assertEqual(append_preview_text("hello", "  "), "hello")
        self.assertEqual(append_preview_text("hello", ""), "hello")

    def test_starts_from_empty(self):
        self.assertEqual(append_preview_text("", "hello"), "hello")
        self.assertEqual(append_preview_text(None, "hello"), "hello")


class TestTypingDelta(unittest.TestCase):
    def test_returns_suffix_for_prefix_growth(self):
        self.assertEqual(typing_delta("hello", "hello world"), " world")

    def test_returns_empty_when_unchanged(self):
        self.assertEqual(typing_delta("hello", "hello"), "")

    def test_returns_none_when_rewritten(self):
        self.assertIsNone(typing_delta("hello world", "hello there"))

    def test_types_full_text_from_empty(self):
        self.assertEqual(typing_delta("", "hello"), "hello")


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
