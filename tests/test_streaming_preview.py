"""Unit tests for streaming preview helpers."""

import unittest

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


if __name__ == "__main__":
    unittest.main()
