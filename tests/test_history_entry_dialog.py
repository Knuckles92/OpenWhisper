"""Qt tests for the history transcription viewer dialog."""

import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from services.models import FAILED_RECORDING_KIND, TranscriptionHistory
from ui_qt.dialogs.history_entry_dialog import HistoryEntryDialog
from ui_qt.widgets.history_sidebar import HistoryItemWidget


def _make_entry(
    text: str = "Fixed transcript text",
    raw_text: str | None = None,
    model: str = "local_whisper (turbo | cuda (float16))",
    audio_file: str | None = None,
    audio_duration: float | None = 12.5,
    transcription_time: float | None = 1.2,
    file_size: int | None = 2048,
    cleanup_provider: str | None = None,
    cleanup_model: str | None = None,
) -> TranscriptionHistory:
    return TranscriptionHistory(
        id="entry-test-id",
        text=text,
        raw_text=raw_text,
        timestamp="2026-07-17T14:30:00",
        model=model,
        audio_file=audio_file,
        transcription_time=transcription_time,
        audio_duration=audio_duration,
        file_size=file_size,
        cleanup_provider=cleanup_provider,
        cleanup_model=cleanup_model,
    )


class TestHistoryEntryDialog:
    """Rendering, Fixed/Raw toggle, and action requests."""

    def test_shows_full_transcript_text(self):
        entry = _make_entry(text="Full transcript body")
        dialog = HistoryEntryDialog(entry)
        assert dialog.isModal()
        assert dialog.transcript_text.toPlainText() == "Full transcript body"
        assert dialog.version_toggle.isHidden()
        assert dialog.copy_raw_button.isHidden()

    def test_fixed_raw_toggle_swaps_text(self):
        entry = _make_entry(
            text="Cleaned version",
            raw_text="Raw ASR version",
            cleanup_provider="openai",
            cleanup_model="gpt-4o-mini",
        )
        dialog = HistoryEntryDialog(entry)
        assert not dialog.version_toggle.isHidden()
        assert not dialog.copy_raw_button.isHidden()
        assert dialog.transcript_text.toPlainText() == "Cleaned version"

        dialog.raw_btn.click()
        assert dialog.transcript_text.toPlainText() == "Raw ASR version"

        dialog.fixed_btn.click()
        assert dialog.transcript_text.toPlainText() == "Cleaned version"

    def test_copy_puts_shown_text_on_clipboard(self):
        entry = _make_entry(
            text="Fixed copy target",
            raw_text="Raw copy target",
        )
        dialog = HistoryEntryDialog(entry)
        copied = []
        dialog.copied.connect(lambda: copied.append(True))

        clipboard = QApplication.clipboard()
        clipboard.clear()
        dialog.copy_button.click()
        assert clipboard.text() == "Fixed copy target"
        assert len(copied) == 1

        dialog.raw_btn.click()
        dialog.copy_button.click()
        assert clipboard.text() == "Raw copy target"
        assert len(copied) == 2

    def test_copy_raw_button_copies_raw_text(self):
        entry = _make_entry(text="Fixed", raw_text="Only raw")
        dialog = HistoryEntryDialog(entry)
        clipboard = QApplication.clipboard()
        clipboard.clear()
        dialog.copy_raw_button.click()
        assert clipboard.text() == "Only raw"

    def test_delete_emits_after_confirmation(self):
        entry = _make_entry()
        dialog = HistoryEntryDialog(entry)
        deleted = []
        dialog.delete_requested.connect(deleted.append)

        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            dialog.delete_button.click()

        assert deleted == ["entry-test-id"]

    def test_retranscribe_visible_when_audio_exists(self):
        entry = _make_entry(audio_file="clip.wav")
        with patch(
            "ui_qt.dialogs.history_entry_dialog.history_manager.get_recording_path",
            return_value=r"C:\recordings\clip.wav",
        ):
            dialog = HistoryEntryDialog(entry)

        assert not dialog.retranscribe_button.isHidden()
        requests = []
        dialog.retranscribe_requested.connect(requests.append)
        dialog.retranscribe_button.click()
        assert requests == [r"C:\recordings\clip.wav"]
        assert dialog.retranscribe_button.menu() is None

    def test_failed_recording_can_be_retried_from_the_card_or_dialog(self, tmp_path):
        recording = tmp_path / "clip.wav"
        recording.write_bytes(b"RIFF")
        entry = _make_entry(text="", audio_file=recording.name)
        entry.entry_kind = FAILED_RECORDING_KIND
        with patch(
            "ui_qt.dialogs.history_entry_dialog.history_manager.get_recording_path",
            return_value=str(recording),
        ):
            dialog = HistoryEntryDialog(entry)
            card = HistoryItemWidget(entry)

        assert card.kind_chip.text() == "Transcription failed"
        assert card.retranscribe_btn.text() == "Retry transcription"
        assert dialog.retranscribe_button.text() == "Retry transcription"
        assert dialog.copy_button.isHidden()
        assert "Audio saved" in dialog.transcript_text.toPlainText()
        requests = []
        dialog.retranscribe_requested.connect(requests.append)
        card.retranscribe_requested.connect(requests.append)
        card.retranscribe_btn.click()
        dialog.retranscribe_button.click()
        assert requests == [str(recording), str(recording)]

    def test_metadata_facts_render_when_present(self):
        entry = _make_entry(
            audio_duration=65.0,
            transcription_time=2.5,
            file_size=1536,
        )
        dialog = HistoryEntryDialog(entry)
        assert dialog.fact_labels["Audio duration"].text() == "1m 5s"
        assert dialog.fact_labels["Transcription time"].text() == "2.5s"
        assert dialog.fact_labels["File size"].text() == "1.5 KB"


