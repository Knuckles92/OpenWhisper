import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from config import config
from ui_qt.dialogs.batch_relation_dialog import BatchRelationDialog

_APP = QApplication.instance() or QApplication([])


class TestBatchRelationDialog:
    def test_prefills_the_remembered_description_and_choice(self):
        dialog = BatchRelationDialog(["a.wav"], "Two halves.", True)

        assert dialog.instructions_text() == "Two halves."
        assert dialog.combine_checked() is True
        assert dialog.objectName() == "batchRelationDialog"
        assert dialog.isModal()

    def test_accessors_strip_and_cap_the_text(self):
        dialog = BatchRelationDialog(["a.wav"], "", False)
        dialog.instructions_edit.setPlainText(
            "  " + "x" * (config.MAX_TRANSCRIPT_BATCH_INSTRUCTION_CHARS + 20) + "  "
        )

        text = dialog.instructions_text()
        assert len(text) == config.MAX_TRANSCRIPT_BATCH_INSTRUCTION_CHARS
        assert not text.startswith(" ")
        assert dialog.combine_checked() is False

    def test_lists_the_queued_files_and_collapses_the_rest(self):
        names = [f"part{i}.mp3" for i in range(8)]
        dialog = BatchRelationDialog(names, "", False)

        rows = [
            label.text()
            for label in dialog.findChildren(QLabel)
            if label.objectName() == "batchRelationFileName"
        ]
        assert rows[:6] == names[:6]
        assert rows[-1] == "+2 more"
        assert len(rows) == 7

    def test_primary_button_accepts(self):
        dialog = BatchRelationDialog(["a.wav"], "", False)
        dialog.combine_check.setChecked(True)

        dialog.use_btn.click()

        assert dialog.result() == 1
        assert dialog.combine_checked() is True
