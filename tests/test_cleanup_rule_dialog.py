"""Qt tests for the learned cleanup rule confirm/edit dialog."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton

from ui_qt.dialogs.cleanup_rule_dialog import CleanupRuleDialog


class _QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


class TestCleanupRuleDialogChoice(_QtTestCase):
    """Confirm flow offers polished vs exactly-as-typed when polish succeeds."""

    def _labels(self, dialog):
        return [b.text() for b in dialog.findChildren(QPushButton)]

    def test_polish_success_offers_both_choices(self):
        dialog = CleanupRuleDialog(
            "Always spell the name as Alex.",
            original="always spell my name Alex",
        )
        labels = self._labels(dialog)
        self.assertIn("Use Polished (Recommended)", labels)
        self.assertIn("Use Exactly as Typed", labels)
        self.assertNotIn("Save Rule", labels)
        self.assertTrue(dialog._offer_choice)
        self.assertEqual(
            dialog.rule_edit.toPlainText(),
            "Always spell the name as Alex.",
        )

    def test_use_exactly_as_typed_accepts_original(self):
        dialog = CleanupRuleDialog(
            "Always spell the name as Alex.",
            original="always spell my name Alex",
        )
        for btn in dialog.findChildren(QPushButton):
            if btn.text() == "Use Exactly as Typed":
                btn.click()
                break
        self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
        self.assertEqual(dialog.rule_text(), "always spell my name Alex")

    def test_use_polished_keeps_edits(self):
        dialog = CleanupRuleDialog(
            "Always spell the name as Alex.",
            original="always spell my name Alex",
        )
        dialog.rule_edit.setPlainText("Custom polished edit")
        for btn in dialog.findChildren(QPushButton):
            if btn.text() == "Use Polished (Recommended)":
                btn.click()
                break
        self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
        self.assertEqual(dialog.rule_text(), "Custom polished edit")

    def test_polish_error_falls_back_to_single_save(self):
        dialog = CleanupRuleDialog(
            "always spell my name Alex",
            original="always spell my name Alex",
            notice="AI polish unavailable — your wording will be saved as written.",
        )
        labels = self._labels(dialog)
        self.assertIn("Save Rule", labels)
        self.assertNotIn("Use Polished (Recommended)", labels)
        self.assertFalse(dialog._offer_choice)

    def test_identical_texts_skip_choice(self):
        dialog = CleanupRuleDialog(
            "Keep acronyms uppercase.",
            original="Keep acronyms uppercase.",
        )
        self.assertFalse(dialog._offer_choice)
        self.assertIn("Save Rule", self._labels(dialog))

    def test_edit_mode_has_no_choice(self):
        dialog = CleanupRuleDialog("Existing rule text")
        self.assertFalse(dialog._offer_choice)
        self.assertIn("Save Rule", self._labels(dialog))
        self.assertEqual(dialog.windowTitle(), "Edit Learned Rule")
