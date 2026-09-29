"""Dialog exporting one, some, or all transcription history entries to disk.

Scope and criteria are picked here; document assembly lives in
:mod:`services.history_export`. The shared layout and the worker thread live
in :class:`~ui_qt.dialogs.export_dialog_base.ExportDialogBase`.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtWidgets import QCheckBox

from config import config
from services.history_export import (
    filter_export_entries,
    render_export_document,
    write_per_entry_files,
)
from ui_qt.dialogs.export_dialog_base import ExportDialogBase


class HistoryExportDialog(ExportDialogBase):
    """Pick history entries, criteria, and a format, then export to disk."""

    _NAME_PREFIX = "historyExport"
    _TITLE = "Export History"
    _SUBTITLE = (
        "Choose which transcriptions to export, what each document "
        "includes, and where the files are written."
    )
    _LOAD_ERROR = "History could not be loaded"
    _LOAD_LOG = "Could not load transcription history for export"
    _EMPTY_TEXT = "No transcriptions to export yet."
    _SCOPE_EYEBROW = "TRANSCRIPTIONS"
    _ALL_LABEL = "All history"
    _SELECTED_LABEL = "Selected transcriptions"
    _SEARCH_PLACEHOLDER = "Search transcriptions…"
    _NOUN = "transcription"
    _NOUN_PLURAL = "transcriptions"
    _MARKDOWN_HINT = (
        "Cleaned and raw sections apply to Markdown exports; other "
        "formats always include everything available."
    )
    _FAILED_TEXT = "Could not export history"
    _FAILED_LOG = "History export failed"
    _DEFAULT_DIR_NAME = "history_export"
    _DEFAULT_FILE_STEM = "openwhisper_history"
    _THREAD_NAME = "history-export"

    def __init__(
        self,
        parent=None,
        entry_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    ):
        if entry_provider is None:
            from services.history_export import list_export_entries

            entry_provider = list_export_entries
        super().__init__(parent, entry_provider)

    def _build_criteria_check(self) -> QCheckBox:
        self.audio_only_check = QCheckBox("Only entries with saved audio")
        return self.audio_only_check

    def _build_include_checks(self) -> tuple[QCheckBox, ...]:
        self.include_cleaned_check = QCheckBox("Include cleaned transcript")
        self.include_raw_check = QCheckBox("Include raw transcript")
        return self.include_cleaned_check, self.include_raw_check

    def _include_options(self) -> Dict[str, bool]:
        return {
            "include_cleaned": self.include_cleaned_check.isChecked(),
            "include_raw": self.include_raw_check.isChecked(),
        }

    def _item_label(self, entry: Dict[str, Any]) -> str:
        stamp = entry.get("formatted_timestamp") or "unknown date"
        preview = entry.get("preview_text") or "Empty transcript"
        return f"{stamp} — {preview}"

    def _export_root(self) -> str:
        return os.path.dirname(os.path.abspath(config.RECORDINGS_FOLDER))

    def _filter_items(
        self,
        items: List[Dict[str, Any]],
        from_dt: Optional[datetime],
        to_dt: Optional[datetime],
    ) -> List[Dict[str, Any]]:
        return filter_export_entries(
            items,
            from_dt=from_dt,
            to_dt=to_dt,
            only_with_audio=self.audio_only_check.isChecked(),
        )

    def _entry_collector(self) -> Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]:
        return lambda entry: entry

    def _progress_title(self, entry: Dict[str, Any]) -> str:
        return entry.get("preview_text") or ""

    def _write_per_item_files(self, entries, fmt: str, output: str, **options) -> None:
        write_per_entry_files(entries, fmt, output, **options)

    def _render_document(self, entries, fmt: str, **options) -> str:
        return render_export_document(entries, fmt, **options)
