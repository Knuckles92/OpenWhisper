"""Qt tests for the learned cleanup rule confirm/edit dialog."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton

from ui_qt.dialogs.cleanup_rule_dialog import CleanupRuleDialog


class _QtTestCase:
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
        assert "Use Polished (Recommended)" in labels
        assert "Use Exactly as Typed" in labels
        assert "Save Rule" not in labels
        assert dialog._offer_choice
        assert dialog.rule_edit.toPlainText() == "Always spell the name as Alex."

    def test_use_exactly_as_typed_accepts_original(self):
        dialog = CleanupRuleDialog(
            "Always spell the name as Alex.",
            original="always spell my name Alex",
        )
        for btn in dialog.findChildren(QPushButton):
            if btn.text() == "Use Exactly as Typed":
                btn.click()
                break
        assert dialog.result() == dialog.DialogCode.Accepted
        assert dialog.rule_text() == "always spell my name Alex"

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
        assert dialog.result() == dialog.DialogCode.Accepted
        assert dialog.rule_text() == "Custom polished edit"

    def test_polish_error_falls_back_to_single_save(self):
        dialog = CleanupRuleDialog(
            "always spell my name Alex",
            original="always spell my name Alex",
            notice="AI polish unavailable — your wording will be saved as written.",
        )
        labels = self._labels(dialog)
        assert "Save Rule" in labels
        assert "Use Polished (Recommended)" not in labels
        assert not dialog._offer_choice

    def test_identical_texts_skip_choice(self):
        dialog = CleanupRuleDialog(
            "Keep acronyms uppercase.",
            original="Keep acronyms uppercase.",
        )
        assert not dialog._offer_choice
        assert "Save Rule" in self._labels(dialog)

    def test_edit_mode_has_no_choice(self):
        dialog = CleanupRuleDialog("Existing rule text")
        assert not dialog._offer_choice
        assert "Save Rule" in self._labels(dialog)
        assert dialog.windowTitle() == "Edit Learned Rule"
