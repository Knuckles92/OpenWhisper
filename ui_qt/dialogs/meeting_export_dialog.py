"""Dialog exporting one, some, or all past meetings to disk.

Scope and criteria are picked here; document assembly lives in
:mod:`meeting.export.bulk`. The shared layout and the worker thread live in
:class:`~ui_qt.dialogs.export_dialog_base.ExportDialogBase`; collection reads
the repository once per meeting on that worker, since those reads grow with
meeting count.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtWidgets import QCheckBox

from config import config
from meeting.content import fallback_meeting_title
from meeting.export.bulk import (
    filter_export_meetings,
    render_export_document,
    write_per_meeting_files,
)
from meeting.time_utils import as_local_time
from ui_qt.dialogs.export_dialog_base import ExportDialogBase


def _default_entry_loader() -> Callable[[str], Optional[Dict[str, Any]]]:
    """Meeting-id -> entry loader backed by one shared repository."""
    from meeting.export.bulk import collect_meeting_export
    from meeting.persist.repository import SqlMeetingRepository

    repository = SqlMeetingRepository()

    def load(meeting_id: str) -> Optional[Dict[str, Any]]:
        return collect_meeting_export(repository, meeting_id)

    return load


class MeetingExportDialog(ExportDialogBase):
    """Pick past meetings, criteria, and a format, then export to disk."""

    _NAME_PREFIX = "meetingExport"
    _TITLE = "Export Past Meetings"
    _SUBTITLE = (
        "Choose which meetings to export, what each document includes, "
        "and where the files are written."
    )
    _LOAD_ERROR = "Past meetings could not be loaded"
    _LOAD_LOG = "Could not load past meetings for export"
    _EMPTY_TEXT = "No past meetings to export yet."
    _SCOPE_EYEBROW = "MEETINGS"
    _ALL_LABEL = "All past meetings"
    _SELECTED_LABEL = "Selected meetings"
    _SEARCH_PLACEHOLDER = "Search meetings…"
    _NOUN = "meeting"
    _NOUN_PLURAL = "meetings"
    _MARKDOWN_HINT = (
        "Transcript and intelligence sections apply to Markdown exports; "
        "other formats always include everything available."
    )
    _FAILED_TEXT = "Could not export past meetings"
    _FAILED_LOG = "Past meetings export failed"
    _DEFAULT_DIR_NAME = "meetings_export"
    _DEFAULT_FILE_STEM = "openwhisper_meetings"
    _THREAD_NAME = "meeting-export"

    def __init__(
        self,
        parent=None,
        meeting_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        entry_loader: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    ):
        if meeting_provider is None:
            from meeting.export.bulk import list_export_meetings

            meeting_provider = list_export_meetings
        super().__init__(parent, meeting_provider)
        self._entry_loader = entry_loader

    def _build_criteria_check(self) -> QCheckBox:
        self.transcript_only_check = QCheckBox("Only meetings with a transcript")
        return self.transcript_only_check

    def _build_include_checks(self) -> tuple[QCheckBox, ...]:
        self.include_transcript_check = QCheckBox("Include transcript")
        self.include_intelligence_check = QCheckBox(
            "Include meeting intelligence (notes, decisions, actions)"
        )
        return self.include_transcript_check, self.include_intelligence_check

    def _include_options(self) -> Dict[str, bool]:
        return {
            "include_transcript": self.include_transcript_check.isChecked(),
            "include_intelligence": self.include_intelligence_check.isChecked(),
        }

    def _item_label(self, meeting: Dict[str, Any]) -> str:
        started = as_local_time(meeting.get("started_at"))
        started_text = (
            started.strftime("%Y-%m-%d %H:%M") if started else "unknown date"
        )
        return f"{fallback_meeting_title(meeting)} — {started_text}"

    def _export_root(self) -> str:
        return os.path.dirname(os.path.abspath(config.MEETINGS_FOLDER))

    def _filter_items(
        self,
        items: List[Dict[str, Any]],
        from_dt: Optional[datetime],
        to_dt: Optional[datetime],
    ) -> List[Dict[str, Any]]:
        return filter_export_meetings(
            items,
            from_dt=from_dt,
            to_dt=to_dt,
            only_with_transcript=self.transcript_only_check.isChecked(),
        )

    def _entry_collector(self) -> Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]:
        loader = self._entry_loader or _default_entry_loader()
        return lambda meeting: loader(str(meeting.get("id") or "")) or None

    @staticmethod
    def _progress_title(meeting: Dict[str, Any]) -> str:
        return fallback_meeting_title(meeting)

    @staticmethod
    def _write_per_item_files(entries, fmt: str, output: str, **options) -> None:
        write_per_meeting_files(entries, fmt, output, **options)

    @staticmethod
    def _render_document(entries, fmt: str, **options) -> str:
        return render_export_document(entries, fmt, **options)
