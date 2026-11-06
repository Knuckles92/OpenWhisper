"""Ownership regressions shared by all native clipboard backends."""
import os
import sys
import threading
import unittest

from services.native_desktop.clipboard import MARKER, NativeClipboard, Snapshot


class MemoryClipboard:
    def __init__(self, formats=None, complete=True):
        self.formats = dict(formats or {})
        self.complete = complete
        self.generation = 0
        self.fail = False

    def read(self):
        return Snapshot(dict(self.formats), self.complete)

    def write(self, formats):
        if self.fail:
            raise OSError("clipboard busy")
        self.formats = dict(formats)
        self.generation += 1

    def text_formats(self, text, html=""):
        result = {"text/plain": text.encode()}
        if html:
            result["text/html"] = html.encode()
        return result

    def text(self, snapshot):
        return snapshot.formats.get("text/plain", b"").decode()

    def sequence(self):
        return self.generation


class ClipboardOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.backend = MemoryClipboard({"text/plain": b"a user's previous copy", "image/png": b"\x89PNG\x00binary"})
        self.clipboard = NativeClipboard(self.backend)

    def tearDown(self):
        self.clipboard.cleanup()

    def test_stage_restores_all_formats(self):
        before = self.backend.read()
        stage = self.clipboard.stage_text("dictation", "<b>dictation</b>")
        self.assertTrue(stage.written)
        self.assertIsNotNone(stage.lease)
        self.assertEqual(self.backend.formats["text/html"], b"<b>dictation</b>")
        self.clipboard._restore_pending()
        self.assertEqual(self.backend.read(), before)

    def test_user_copy_prevents_delayed_restore(self):
        stage = self.clipboard.stage_text("dictation")
        self.backend.write({"text/plain": b"newer user copy"})
        self.assertFalse(self.clipboard.schedule_restore(stage.lease, 0))
        self.clipboard._restore_pending()
        self.assertEqual(self.backend.formats, {"text/plain": b"newer user copy"})

    def test_identical_user_copy_still_releases_ownership(self):
        stage = self.clipboard.stage_text("same words")
        self.backend.write({"text/plain": b"same words"})
        self.assertFalse(self.clipboard.commit_text(stage.lease, "same words"))
        self.clipboard._restore_pending()
        self.assertEqual(self.backend.formats, {"text/plain": b"same words"})

    def test_failed_write_never_claims_paste_ready(self):
        self.backend.fail = True
        stage = self.clipboard.stage_text("dictation")
        self.assertFalse(stage.written)
        self.assertIsNone(stage.lease)

    def test_unrepresentable_formats_report_restore_unavailable(self):
        self.backend.complete = False
        stage = self.clipboard.stage_text("dictation")
        self.assertTrue(stage.written)
        self.assertTrue(stage.restore_unavailable)
        self.assertIsNone(stage.lease)

    def test_restore_off_leaves_rich_transcript(self):
        stage = self.clipboard.stage_text("dictation", "<b>dictation</b>", restore=False)
        self.assertTrue(stage.written)
        self.assertIsNone(stage.lease)
        self.assertEqual(self.backend.formats, {"text/plain": b"dictation", "text/html": b"<b>dictation</b>"})

    def test_commit_after_paste_failure_keeps_rich_text(self):
        stage = self.clipboard.stage_text("dictation", "<b>dictation</b>")
        self.assertTrue(self.clipboard.commit_transcript_clipboard(stage, "dictation"))
        self.assertNotIn(MARKER, self.backend.formats)
        self.clipboard._restore_pending()
        self.assertEqual(self.backend.formats["text/html"], b"<b>dictation</b>")

    def test_following_paste_keeps_original_snapshot(self):
        original = self.backend.read()
        self.clipboard.stage_text("first")
        second = self.clipboard.stage_text("second")
        self.assertEqual(second.lease.snapshot, original)
        self.clipboard._restore_pending()
        self.assertEqual(self.backend.read(), original)

    def test_new_copy_cancels_previous_paste_restore(self):
        self.clipboard.stage_text("dictation")
        self.assertTrue(self.clipboard.write_text("manual copy"))
        self.clipboard._restore_pending()
        self.assertEqual(self.backend.formats, {"text/plain": b"manual copy"})

    def test_prefetched_snapshot_is_discarded_after_user_copy(self):
        self.clipboard._prefetch = (self.backend.sequence(), self.backend.read())
        self.backend.write({"text/plain": b"new copy during recording"})
        stage = self.clipboard.stage_text("dictation")
        self.assertEqual(stage.lease.snapshot.formats, {"text/plain": b"new copy during recording"})

    def test_selection_capture_restores_before_callback(self):
        before = self.backend.read()
        result, ready = [], threading.Event()
        def callback(text):
            result.append((text, self.backend.read()))
            ready.set()
        self.clipboard.capture_selection(callback, send_copy=lambda: self.backend.write({"text/plain": "selected café".encode()}))
        self.assertTrue(ready.wait(2))
        self.assertEqual(result, [("selected café", before)])

    def test_selection_does_not_overwrite_uncapturable_clipboard(self):
        self.backend.complete = False
        called, ready, result = [], threading.Event(), []
        def callback(text):
            result.append(text)
            ready.set()
        self.clipboard.capture_selection(callback, send_copy=lambda: called.append(True))
        self.assertTrue(ready.wait(2))
        self.assertFalse(called)
        self.assertEqual(result, [None])


@unittest.skipUnless(os.environ.get("OPENWHISPER_TEST_X11") == "1", "requires isolated X11 test display")
class X11ClipboardTests(unittest.TestCase):
    def test_large_binary_format_survives_incremental_restore(self):
        from services.native_desktop.clipboard import _X11Clipboard

        owner = _X11Clipboard()
        reader = NativeClipboard(_X11Clipboard())
        image = b"\x89PNG" + bytes(range(256)) * 2000
        try:
            owner.write({"image/png": image, "UTF8_STRING": b"image caption"})
            stage = reader.stage_text("dictation")
            self.assertTrue(stage.written)
            self.assertFalse(stage.restore_unavailable)
            reader._restore_pending()
            self.assertEqual(owner.read().formats["image/png"], image)
        finally:
            owner.close()
            reader.cleanup()

    def test_two_native_owners_preserve_mime_and_respect_foreign_copy(self):
        from services.native_desktop.clipboard import _X11Clipboard

        first = NativeClipboard(_X11Clipboard())
        other = NativeClipboard(_X11Clipboard())
        try:
            self.assertTrue(first.write_text("previous café", "<b>previous café</b>"))
            stage = other.stage_text("dictation")
            self.assertTrue(stage.written)
            other._restore_pending()
            self.assertEqual(first._os().text(first._os().read()), "previous café")
            self.assertEqual(first._os().read().formats["text/html"], "<b>previous café</b>".encode())
            stage = other.stage_text("another dictation")
            self.assertTrue(first.write_text("new user copy"))
            other._restore_pending()
            self.assertEqual(first._os().text(first._os().read()), "new user copy")
        finally:
            first.cleanup()
            other.cleanup()


@unittest.skipUnless(os.environ.get("OPENWHISPER_TEST_OS_CLIPBOARD") == "1" and sys.platform in ("win32", "darwin"), "requires isolated native desktop clipboard")
class DesktopClipboardTests(unittest.TestCase):
    def test_rendered_unicode_rich_text_and_owner_safe_restore(self):
        from services.native_desktop.clipboard import _MacClipboard, _WindowsClipboard

        backend = _WindowsClipboard() if sys.platform == "win32" else _MacClipboard()
        saved = backend.read()
        clipboard = NativeClipboard(backend)
        try:
            self.assertTrue(clipboard.write_text("previous café — 日本語", "<b>previous café — 日本語</b>"))
            previous = backend.read()
            stage = clipboard.stage_text("dictated words", "<em>dictated words</em>")
            self.assertTrue(stage.written)
            self.assertFalse(stage.restore_unavailable)
            clipboard._restore_pending()
            self.assertEqual(backend.text(backend.read()), "previous café — 日本語")
            # The originally rendered rich-text format must be retained too.
            for kind, content in previous.formats.items():
                self.assertEqual(backend.read().formats.get(kind), content)
            stage = clipboard.stage_text("next dictation")
            backend.write(backend.text_formats("newer user copy"))
            clipboard._restore_pending()
            self.assertEqual(backend.text(backend.read()), "newer user copy")
        finally:
            clipboard.cleanup()
            if saved.complete:
                backend.write(saved.formats)


@unittest.skipUnless(os.environ.get("OPENWHISPER_TEST_WAYLAND") == "1", "requires isolated Wayland test compositor")
class WaylandClipboardTests(unittest.TestCase):
    def test_native_data_control_preserves_rich_text_binary_and_foreign_copy(self):
        first, other = NativeClipboard(), NativeClipboard()
        try:
            first.copy_to_clipboard("Wayland café", "<b>Wayland café</b>")
            self.assertEqual(type(first._os()).__name__, "_WaylandNativeClipboard")
            previous = first._os().read().formats
            previous["image/png"] = b"\x89PNG" + bytes(range(256)) * 2000
            first._os().write(previous)
            stage = other.stage_text("native dictation", "<em>native dictation</em>")
            self.assertTrue(stage.written)
            self.assertIsNotNone(stage.lease)
            self.assertFalse(stage.restore_unavailable)
            other._restore_pending()
            self.assertEqual(first._os().read().formats, previous)
            other.stage_text("next dictation")
            first.copy_to_clipboard("newer user copy")
            other._restore_pending()
            self.assertEqual(first._os().text(first._os().read()), "newer user copy")
        finally:
            first._os().write({})
            first.close()
            other.close()
            first._backend.process.wait(timeout=3)
            other._backend.process.wait(timeout=3)

    def test_committed_copy_survives_service_exit_until_replaced(self):
        first, other = NativeClipboard(), NativeClipboard()
        try:
            self.assertTrue(first.copy_to_clipboard("kept after closing OpenWhisper", "<b>kept after closing OpenWhisper</b>"))
            first.close()
            self.assertEqual(other._os().text(other._os().read()), "kept after closing OpenWhisper")
            self.assertIn("text/html", other._os().read().formats)
            self.assertTrue(other.copy_to_clipboard("replacement copy"))
            first._backend.process.wait(timeout=3)
        finally:
            other._os().write({})
            first.close()
            other.close()
            first._backend.process.wait(timeout=3)
            other._backend.process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
