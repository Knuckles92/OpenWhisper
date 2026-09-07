"""Dialog exporting one, some, or all past meetings to disk.

Scope and criteria are picked here; document assembly lives in
:mod:`meeting.export.bulk`. Collection and rendering run on a worker thread
(the repository reads grow with meeting count); every UI update arrives via
signals so nothing touches widgets off the Qt thread. Cancel is cooperative
and checked between meetings — nothing is written until collection finishes.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, time
from typing import Any, Callable, Dict, List, Optional

from config import config
from meeting.content import fallback_meeting_title
from meeting.export.bulk import (
    FORMAT_JSON,
    FORMAT_MARKDOWN,
    FORMAT_TXT,
    filter_export_meetings,
    render_export_document,
    write_per_meeting_files,
)
from meeting.time_utils import as_local_time

from PyQt6.QtCore import (
    QDate,
    Qt,
)
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui_qt.dialogs.export_dialog_base import ExportDialogBase
from ui_qt.widgets import (
    AnimatedProgressBar,
    Button,
    ElidingComboBox,
    WrappedLabel,
)
from ui_qt.widgets.no_wheel import NoWheelDateEdit

logger = logging.getLogger(__name__)

_FORMAT_LABELS = {
    FORMAT_MARKDOWN: "Markdown",
    FORMAT_TXT: "Plain transcript",
    FORMAT_JSON: "JSON",
}
_FORMAT_EXTENSIONS = {
    FORMAT_MARKDOWN: "md",
    FORMAT_TXT: "txt",
    FORMAT_JSON: "json",
}
_SELECTED_LIST_HEIGHT = 180


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

    def __init__(
        self,
        parent=None,
        meeting_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        entry_loader: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    ):
        super().__init__(parent)
        self.setObjectName("meetingExportDialog")
        self.setWindowTitle("Export Past Meetings")

        if meeting_provider is None:
            from meeting.export.bulk import list_export_meetings

            meeting_provider = list_export_meetings
        self._meeting_provider = meeting_provider
        self._entry_loader = entry_loader
        self._meetings: List[Dict[str, Any]] = []
        self._load_meetings()
        self._setup_ui()

    def _load_meetings(self) -> None:
        try:
            self._meetings = list(self._meeting_provider() or [])
            self._load_error = ""
        except Exception as exc:
            logger.exception("Could not load past meetings for export")
            self._meetings = []
            self._load_error = str(exc)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(14)

        title = QLabel("Export Past Meetings")
        title.setObjectName("modelManagerTitle")
        layout.addWidget(title)
        subtitle = WrappedLabel(
            "Choose which meetings to export, what each document includes, "
            "and where the files are written."
        )
        subtitle.setObjectName("modelManagerSubtitle")
        layout.addWidget(subtitle)

        if self._load_error:
            error = WrappedLabel(
                f"Past meetings could not be loaded:\n{self._load_error}"
            )
            error.setObjectName("infoLabel")
            layout.addWidget(error)
            layout.addStretch()
            close_btn = Button("Close")
            close_btn.clicked.connect(self.reject)
            row = QHBoxLayout()
            row.addStretch()
            row.addWidget(close_btn)
            layout.addLayout(row)
            return

        if not self._meetings:
            empty = WrappedLabel("No past meetings to export yet.")
            empty.setObjectName("infoLabel")
            layout.addWidget(empty)
            layout.addStretch()
            close_btn = Button("Close")
            close_btn.clicked.connect(self.reject)
            row = QHBoxLayout()
            row.addStretch()
            row.addWidget(close_btn)
            layout.addLayout(row)
            return

        self.meetings_card = self._build_meetings_card()
        layout.addWidget(self.meetings_card)
        self.content_card = self._build_content_card()
        layout.addWidget(self.content_card)
        self.output_card = self._build_output_card()
        layout.addWidget(self.output_card)
        layout.addLayout(self._build_progress_row())
        layout.addLayout(self._build_button_row())
        self.path_edit.setText(self._default_path())
        self._refresh_export_enabled()

    def _card(self, eyebrow: str) -> tuple[QVBoxLayout, QFrame]:
        frame = QFrame(self)
        frame.setObjectName("meetingExportCard")
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(16, 12, 16, 14)
        inner.setSpacing(10)
        label = QLabel(eyebrow)
        label.setObjectName("meetingExportEyebrow")
        inner.addWidget(label)
        return inner, frame

    def _build_meetings_card(self) -> QWidget:
        inner, frame = self._card("MEETINGS")

        scope_row = QHBoxLayout()
        scope_row.setSpacing(20)
        self.all_radio = QRadioButton("All past meetings")
        self.all_radio.setChecked(True)
        self.selected_radio = QRadioButton("Selected meetings")
        group = QButtonGroup(self)
        group.addButton(self.all_radio)
        group.addButton(self.selected_radio)
        self.all_radio.toggled.connect(self._on_scope_changed)
        scope_row.addWidget(self.all_radio)
        scope_row.addWidget(self.selected_radio)
        scope_row.addStretch()
        inner.addLayout(scope_row)

        inner.addWidget(self._build_filters_panel())
        inner.addWidget(self._build_selected_panel())
        self.selected_panel.setVisible(False)
        return frame

    def _build_filters_panel(self) -> QWidget:
        self.filters_panel = QWidget()
        self.filters_panel.setObjectName("meetingExportPane")
        panel_layout = QVBoxLayout(self.filters_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(8)

        self.date_range_check = QCheckBox("Limit to a date range")
        self.date_range_check.toggled.connect(self._on_date_range_toggled)
        panel_layout.addWidget(self.date_range_check)

        self.date_range_row = QWidget()
        self.date_range_row.setObjectName("meetingExportPane")
        date_row = QHBoxLayout(self.date_range_row)
        date_row.setContentsMargins(28, 0, 0, 0)
        date_row.setSpacing(8)
        from_label = QLabel("From")
        from_label.setObjectName("meetingExportMeta")
        self.from_date = NoWheelDateEdit()
        self.from_date.setDate(QDate.currentDate().addMonths(-1))
        to_label = QLabel("To")
        to_label.setObjectName("meetingExportMeta")
        self.to_date = NoWheelDateEdit()
        self.to_date.setDate(QDate.currentDate())
        date_row.addWidget(from_label)
        date_row.addWidget(self.from_date, 1)
        date_row.addWidget(to_label)
        date_row.addWidget(self.to_date, 1)
        self.date_range_row.setVisible(False)
        panel_layout.addWidget(self.date_range_row)

        self.transcript_only_check = QCheckBox("Only meetings with a transcript")
        panel_layout.addWidget(self.transcript_only_check)
        return self.filters_panel

    def _build_selected_panel(self) -> QWidget:
        self.selected_panel = QWidget()
        self.selected_panel.setObjectName("meetingExportPane")
        panel_layout = QVBoxLayout(self.selected_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(8)

        self.selected_search = QLineEdit()
        self.selected_search.setPlaceholderText("Search meetings…")
        self.selected_search.setClearButtonEnabled(True)
        self.selected_search.textChanged.connect(self._filter_selected_list)
        panel_layout.addWidget(self.selected_search)

        self.selected_list = QListWidget()
        self.selected_list.setObjectName("meetingExportList")
        self.selected_list.setMinimumHeight(_SELECTED_LIST_HEIGHT)
        self.selected_list.setMaximumHeight(_SELECTED_LIST_HEIGHT)
        self.selected_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.selected_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.selected_list.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.selected_list.setUniformItemSizes(True)
        for meeting in self._meetings:
            self.selected_list.addItem(self._make_list_item(meeting))
        self.selected_list.itemChanged.connect(
            lambda _item: self._refresh_export_enabled()
        )
        panel_layout.addWidget(self.selected_list)

        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(8)
        select_all_btn = Button("Select all")
        select_all_btn.setObjectName("meetingExportMiniBtn")
        select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        clear_btn = Button("Clear")
        clear_btn.setObjectName("meetingExportMiniBtn")
        clear_btn.clicked.connect(lambda: self._set_all_checked(False))
        toggle_row.addWidget(select_all_btn)
        toggle_row.addWidget(clear_btn)
        toggle_row.addStretch()
        self.selected_count_label = QLabel("")
        self.selected_count_label.setObjectName("meetingExportMeta")
        toggle_row.addWidget(self.selected_count_label)
        panel_layout.addLayout(toggle_row)
        return self.selected_panel

    def _make_list_item(self, meeting: Dict[str, Any]) -> QListWidgetItem:
        started = as_local_time(meeting.get("started_at"))
        started_text = (
            started.strftime("%Y-%m-%d %H:%M") if started else "unknown date"
        )
        label = f"{fallback_meeting_title(meeting)} — {started_text}"
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, meeting.get("id") or "")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Unchecked)
        item.setToolTip(label)
        return item

    def _build_content_card(self) -> QWidget:
        inner, frame = self._card("CONTENT")
        format_caption = QLabel("Format")
        format_caption.setObjectName("textModelFieldLabel")
        self.format_combo = ElidingComboBox()
        self.format_combo.setMinimumHeight(36)
        for fmt, text in _FORMAT_LABELS.items():
            self.format_combo.addItem(text, fmt)
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        field = QVBoxLayout()
        field.setSpacing(5)
        field.addWidget(format_caption)
        field.addWidget(self.format_combo)
        inner.addLayout(field)

        self.per_meeting_check = QCheckBox("One file per meeting")
        self.per_meeting_check.toggled.connect(self._on_per_meeting_toggled)
        inner.addWidget(self.per_meeting_check)

        self.include_transcript_check = QCheckBox("Include transcript")
        self.include_transcript_check.setChecked(True)
        inner.addWidget(self.include_transcript_check)

        self.include_intelligence_check = QCheckBox(
            "Include meeting intelligence (notes, decisions, actions)"
        )
        self.include_intelligence_check.setChecked(True)
        inner.addWidget(self.include_intelligence_check)

        self.markdown_only_hint = WrappedLabel(
            "Transcript and intelligence sections apply to Markdown exports; "
            "other formats always include everything available."
        )
        self.markdown_only_hint.setObjectName("infoLabel")
        self.markdown_only_hint.setVisible(False)
        inner.addWidget(self.markdown_only_hint)
        return frame

    def _build_output_card(self) -> QWidget:
        inner, frame = self._card("OUTPUT")
        output_row = QHBoxLayout()
        output_row.setSpacing(8)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Choose where to save…")
        self.path_edit.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.path_edit.textChanged.connect(self._on_path_changed)
        browse_btn = Button("Browse…")
        browse_btn.setObjectName("meetingExportMiniBtn")
        browse_btn.clicked.connect(self._on_browse)
        output_row.addWidget(self.path_edit, 1)
        output_row.addWidget(browse_btn)
        inner.addLayout(output_row)

        self.output_hint = WrappedLabel(
            "Everything is written to a single file. Turn on \u201cOne file "
            "per meeting\u201d to fill a folder instead."
        )
        self.output_hint.setObjectName("infoLabel")
        inner.addWidget(self.output_hint)
        return frame

    def _build_progress_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self.progress_bar = AnimatedProgressBar()
        self.progress_bar.reset()
        self.progress_bar.setVisible(False)
        self.progress_label = WrappedLabel("")
        self.progress_label.setObjectName("meetingExportProgress")
        row.addWidget(self.progress_bar, 1)
        row.addWidget(self.progress_label, 1)
        return row

    def _on_format_changed(self, _index: int) -> None:
        markdown = self._current_format() == FORMAT_MARKDOWN
        self.include_transcript_check.setEnabled(markdown)
        self.include_intelligence_check.setEnabled(markdown)
        self._refresh_default_path()
        self._animate_sections(
            show=() if markdown else (self.markdown_only_hint,),
            hide=(self.markdown_only_hint,) if markdown else (),
        )

    def _sections(self) -> tuple[QWidget, ...]:
        names = (
            "date_range_row",
            "markdown_only_hint",
            "filters_panel",
            "selected_panel",
            "meetings_card",
            "content_card",
            "output_card",
        )
        return tuple(
            widget
            for widget in (getattr(self, name, None) for name in names)
            if widget is not None
        )

    def _on_per_meeting_toggled(self, checked: bool) -> None:
        self.output_hint.setText(
            "Each meeting becomes its own file inside the chosen folder."
            if checked
            else "Everything is written to a single file. Turn on \u201cOne "
            "file per meeting\u201d to fill a folder instead."
        )
        self._refresh_default_path()

    def _default_paths(self) -> set[str]:
        folder = os.path.dirname(os.path.abspath(config.MEETINGS_FOLDER))
        paths = {os.path.join(folder, "meetings_export")}
        for extension in _FORMAT_EXTENSIONS.values():
            paths.add(os.path.join(folder, f"openwhisper_meetings.{extension}"))
        return paths

    def _default_path(self) -> str:
        folder = os.path.dirname(os.path.abspath(config.MEETINGS_FOLDER))
        if self.per_meeting_check.isChecked():
            return os.path.join(folder, "meetings_export")
        extension = _FORMAT_EXTENSIONS.get(self._current_format(), "md")
        return os.path.join(folder, f"openwhisper_meetings.{extension}")

    def _current_format(self) -> str:
        return self.format_combo.currentData() or FORMAT_MARKDOWN

    def _on_browse(self) -> None:
        if self.per_meeting_check.isChecked():
            out_dir = QFileDialog.getExistingDirectory(
                self, "Choose Export Folder", self.path_edit.text()
            )
            if out_dir:
                self.path_edit.setText(out_dir)
            return
        extension = _FORMAT_EXTENSIONS.get(self._current_format(), "md")
        label = _FORMAT_LABELS.get(self._current_format(), "File")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Past Meetings",
            self._default_path(),
            f"{label} (*.{extension});;All Files (*)",
        )
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path = f"{path}.{extension}"
        self.path_edit.setText(path)

    def _resolve_targets(self) -> List[Dict[str, Any]]:
        # Filters only accompany the "all meetings" scope; an explicit
        # selection is exported exactly as checked.
        if self.selected_radio.isChecked():
            checked = set(self._checked_ids())
            return [
                meeting
                for meeting in self._meetings
                if str(meeting.get("id") or "") in checked
            ]
        from_dt = None
        to_dt = None
        if self.date_range_check.isChecked():
            from_dt = datetime.combine(
                self.from_date.date().toPyDate(), time.min
            )
            to_dt = datetime.combine(
                self.to_date.date().toPyDate(), time.max.replace(microsecond=0)
            )
        return filter_export_meetings(
            list(self._meetings),
            from_dt=from_dt,
            to_dt=to_dt,
            only_with_transcript=self.transcript_only_check.isChecked(),
        )

    def _on_export(self) -> None:
        if self._worker is not None:
            return
        targets = self._resolve_targets()
        if not targets:
            QMessageBox.information(
                self,
                "Export Past Meetings",
                "No meetings match the selected criteria.",
            )
            return
        params = {
            "targets": targets,
            "fmt": self._current_format(),
            "per_meeting": self.per_meeting_check.isChecked(),
            "output": self.path_edit.text().strip(),
            "include_transcript": self.include_transcript_check.isChecked(),
            "include_intelligence": self.include_intelligence_check.isChecked(),
        }
        self._cancel_requested = False
        self._set_busy(True)
        self.progress.emit(0, len(targets), "")
        self._worker = threading.Thread(
            target=self._export_worker,
            args=(params,),
            name="meeting-export",
            daemon=True,
        )
        self._worker.start()

    def _export_worker(self, params: Dict[str, Any]) -> None:
        try:
            loader = self._entry_loader or _default_entry_loader()
            entries: List[Dict[str, Any]] = []
            targets = params["targets"]
            total = len(targets)
            for index, meeting in enumerate(targets):
                if self._cancel_requested:
                    self.export_canceled.emit()
                    return
                entry = loader(str(meeting.get("id") or ""))
                if entry:
                    entries.append(entry)
                self.progress.emit(
                    index + 1, total, fallback_meeting_title(meeting)
                )
            if self._cancel_requested:
                self.export_canceled.emit()
                return
            fmt = params["fmt"]
            output = params["output"]
            include_transcript = params["include_transcript"]
            include_intelligence = params["include_intelligence"]
            if params["per_meeting"]:
                write_per_meeting_files(
                    entries,
                    fmt,
                    output,
                    include_transcript=include_transcript,
                    include_intelligence=include_intelligence,
                )
                summary = output
            else:
                document = render_export_document(
                    entries,
                    fmt,
                    include_transcript=include_transcript,
                    include_intelligence=include_intelligence,
                )
                parent = os.path.dirname(output)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(output, "w", encoding="utf-8") as handle:
                    handle.write(document)
                summary = output
            self.export_finished.emit(len(entries), summary)
        except Exception as exc:
            logger.exception("Past meetings export failed")
            self.export_failed.emit(str(exc))

    def _on_export_finished(self, count: int, summary: str) -> None:
        self._worker = None
        self._set_busy(False)
        noun = "meeting" if count == 1 else "meetings"
        QMessageBox.information(
            self,
            "Export Past Meetings",
            f"Exported {count} {noun} to:\n{summary}",
        )

    def _on_export_failed(self, message: str) -> None:
        self._worker = None
        self._set_busy(False)
        QMessageBox.warning(
            self,
            "Export Failed",
            f"Could not export past meetings:\n{message}",
        )
