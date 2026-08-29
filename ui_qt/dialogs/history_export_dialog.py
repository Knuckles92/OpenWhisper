"""Dialog exporting one, some, or all transcription history entries to disk.

Scope and criteria are picked here; document assembly lives in
:mod:`services.history_export`. Rendering and writing run on a worker
thread; every UI update arrives via signals so nothing touches widgets off
the Qt thread. Cancel is cooperative — nothing is written until collection
finishes.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, time
from typing import Any, Callable, Dict, List, Optional

from config import config
from services.history_export import (
    FORMAT_JSON,
    FORMAT_MARKDOWN,
    FORMAT_TXT,
    filter_export_entries,
    render_export_document,
    write_per_entry_files,
)

from PyQt6.QtCore import (
    QDate,
    QParallelAnimationGroup,
    QPropertyAnimation,
    QRect,
    Qt,
    pyqtSignal,
)
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui_qt.utils.collapse_animation import (
    SECTION_COLLAPSE_DURATION_MS,
    SECTION_COLLAPSE_EASING,
    UNLIMITED_HEIGHT,
    create_max_height_animation,
)
from ui_qt.widgets import (
    AnimatedProgressBar,
    Button,
    ElidingComboBox,
    PrimaryButton,
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


class HistoryExportDialog(QDialog):
    """Pick history entries, criteria, and a format, then export to disk."""

    progress = pyqtSignal(int, int, str)
    export_finished = pyqtSignal(int, str)
    export_failed = pyqtSignal(str)
    export_canceled = pyqtSignal()

    def __init__(
        self,
        parent=None,
        entry_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    ):
        super().__init__(parent)
        self.setObjectName("historyExportDialog")
        self.setWindowTitle("Export History")
        self.setMinimumWidth(600)
        self.setMaximumWidth(680)
        self.setModal(True)

        if entry_provider is None:
            from services.history_export import list_export_entries

            entry_provider = list_export_entries
        self._entry_provider = entry_provider
        self._entries: List[Dict[str, Any]] = []
        self._worker: Optional[threading.Thread] = None
        self._cancel_requested = False
        self._anim_group: Optional[QParallelAnimationGroup] = None

        self.progress.connect(self._on_progress)
        self.export_finished.connect(self._on_export_finished)
        self.export_failed.connect(self._on_export_failed)
        self.export_canceled.connect(self._on_export_canceled)

        self._load_entries()
        self._setup_ui()

    def _load_entries(self) -> None:
        try:
            self._entries = list(self._entry_provider() or [])
            self._load_error = ""
        except Exception as exc:
            logger.exception("Could not load transcription history for export")
            self._entries = []
            self._load_error = str(exc)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(14)

        title = QLabel("Export History")
        title.setObjectName("modelManagerTitle")
        layout.addWidget(title)
        subtitle = WrappedLabel(
            "Choose which transcriptions to export, what each document "
            "includes, and where the files are written."
        )
        subtitle.setObjectName("modelManagerSubtitle")
        layout.addWidget(subtitle)

        if self._load_error:
            error = WrappedLabel(
                f"History could not be loaded:\n{self._load_error}"
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

        if not self._entries:
            empty = WrappedLabel("No transcriptions to export yet.")
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

        self.entries_card = self._build_entries_card()
        layout.addWidget(self.entries_card)
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
        frame.setObjectName("historyExportCard")
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(16, 12, 16, 14)
        inner.setSpacing(10)
        label = QLabel(eyebrow)
        label.setObjectName("historyExportEyebrow")
        inner.addWidget(label)
        return inner, frame

    def _build_entries_card(self) -> QWidget:
        inner, frame = self._card("TRANSCRIPTIONS")

        scope_row = QHBoxLayout()
        scope_row.setSpacing(20)
        self.all_radio = QRadioButton("All history")
        self.all_radio.setChecked(True)
        self.selected_radio = QRadioButton("Selected transcriptions")
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
        self.filters_panel.setObjectName("historyExportPane")
        panel_layout = QVBoxLayout(self.filters_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(8)

        self.date_range_check = QCheckBox("Limit to a date range")
        self.date_range_check.toggled.connect(self._on_date_range_toggled)
        panel_layout.addWidget(self.date_range_check)

        self.date_range_row = QWidget()
        self.date_range_row.setObjectName("historyExportPane")
        date_row = QHBoxLayout(self.date_range_row)
        date_row.setContentsMargins(28, 0, 0, 0)
        date_row.setSpacing(8)
        from_label = QLabel("From")
        from_label.setObjectName("historyExportMeta")
        self.from_date = NoWheelDateEdit()
        self.from_date.setDate(QDate.currentDate().addMonths(-1))
        to_label = QLabel("To")
        to_label.setObjectName("historyExportMeta")
        self.to_date = NoWheelDateEdit()
        self.to_date.setDate(QDate.currentDate())
        date_row.addWidget(from_label)
        date_row.addWidget(self.from_date, 1)
        date_row.addWidget(to_label)
        date_row.addWidget(self.to_date, 1)
        self.date_range_row.setVisible(False)
        panel_layout.addWidget(self.date_range_row)

        self.audio_only_check = QCheckBox("Only entries with saved audio")
        panel_layout.addWidget(self.audio_only_check)
        return self.filters_panel

    def _build_selected_panel(self) -> QWidget:
        self.selected_panel = QWidget()
        self.selected_panel.setObjectName("historyExportPane")
        panel_layout = QVBoxLayout(self.selected_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(8)

        self.selected_search = QLineEdit()
        self.selected_search.setPlaceholderText("Search transcriptions…")
        self.selected_search.setClearButtonEnabled(True)
        self.selected_search.textChanged.connect(self._filter_selected_list)
        panel_layout.addWidget(self.selected_search)

        self.selected_list = QListWidget()
        self.selected_list.setObjectName("historyExportList")
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
        for entry in self._entries:
            self.selected_list.addItem(self._make_list_item(entry))
        self.selected_list.itemChanged.connect(
            lambda _item: self._refresh_export_enabled()
        )
        panel_layout.addWidget(self.selected_list)

        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(8)
        select_all_btn = Button("Select all")
        select_all_btn.setObjectName("historyExportMiniBtn")
        select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        clear_btn = Button("Clear")
        clear_btn.setObjectName("historyExportMiniBtn")
        clear_btn.clicked.connect(lambda: self._set_all_checked(False))
        toggle_row.addWidget(select_all_btn)
        toggle_row.addWidget(clear_btn)
        toggle_row.addStretch()
        self.selected_count_label = QLabel("")
        self.selected_count_label.setObjectName("historyExportMeta")
        toggle_row.addWidget(self.selected_count_label)
        panel_layout.addLayout(toggle_row)
        return self.selected_panel

    def _make_list_item(self, entry: Dict[str, Any]) -> QListWidgetItem:
        stamp = entry.get("formatted_timestamp") or "unknown date"
        preview = entry.get("preview_text") or "Empty transcript"
        label = f"{stamp} — {preview}"
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, entry.get("id") or "")
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

        self.per_entry_check = QCheckBox("One file per transcription")
        self.per_entry_check.toggled.connect(self._on_per_entry_toggled)
        inner.addWidget(self.per_entry_check)

        self.include_cleaned_check = QCheckBox("Include cleaned transcript")
        self.include_cleaned_check.setChecked(True)
        inner.addWidget(self.include_cleaned_check)

        self.include_raw_check = QCheckBox("Include raw transcript")
        self.include_raw_check.setChecked(True)
        inner.addWidget(self.include_raw_check)

        self.markdown_only_hint = WrappedLabel(
            "Cleaned and raw sections apply to Markdown exports; other "
            "formats always include everything available."
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
        browse_btn.setObjectName("historyExportMiniBtn")
        browse_btn.clicked.connect(self._on_browse)
        output_row.addWidget(self.path_edit, 1)
        output_row.addWidget(browse_btn)
        inner.addLayout(output_row)

        self.output_hint = WrappedLabel(
            "Everything is written to a single file. Turn on \u201cOne file "
            "per transcription\u201d to fill a folder instead."
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
        self.progress_label.setObjectName("historyExportProgress")
        row.addWidget(self.progress_bar, 1)
        row.addWidget(self.progress_label, 1)
        return row

    def _build_button_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch()
        self.cancel_work_btn = Button("Cancel")
        self.cancel_work_btn.clicked.connect(self._on_cancel_work)
        self.cancel_work_btn.setVisible(False)
        self.close_btn = Button("Close")
        self.close_btn.clicked.connect(self.reject)
        self.export_btn = PrimaryButton("Export")
        self.export_btn.setDefault(True)
        self.export_btn.clicked.connect(self._on_export)
        row.addWidget(self.cancel_work_btn)
        row.addWidget(self.close_btn)
        row.addWidget(self.export_btn)
        return row

    def _on_scope_changed(self, _checked: bool) -> None:
        selected = self.selected_radio.isChecked()
        self._refresh_export_enabled()
        self._animate_sections(
            show=(self.selected_panel,) if selected else (self.filters_panel,),
            hide=(self.filters_panel,) if selected else (self.selected_panel,),
        )

    def _filter_selected_list(self, text: str) -> None:
        query = text.strip().lower()
        for row in range(self.selected_list.count()):
            item = self.selected_list.item(row)
            item.setHidden(bool(query) and query not in item.text().lower())

    def _set_all_checked(self, checked: bool) -> None:
        state = (
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        )
        for row in range(self.selected_list.count()):
            item = self.selected_list.item(row)
            if item.isHidden():
                continue
            item.setCheckState(state)

    def _on_date_range_toggled(self, checked: bool) -> None:
        self._animate_sections(
            show=(self.date_range_row,) if checked else (),
            hide=() if checked else (self.date_range_row,),
        )

    def _on_format_changed(self, _index: int) -> None:
        markdown = self._current_format() == FORMAT_MARKDOWN
        self.include_cleaned_check.setEnabled(markdown)
        self.include_raw_check.setEnabled(markdown)
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
            "entries_card",
            "content_card",
            "output_card",
        )
        return tuple(
            widget
            for widget in (getattr(self, name, None) for name in names)
            if widget is not None
        )

    def _content_height(self) -> int:
        layout = self.layout()
        constraint = layout.sizeConstraint()
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        try:
            for widget in self._sections():
                if widget.layout() is not None:
                    widget.layout().invalidate()
                    widget.layout().activate()
                widget.updateGeometry()
                widget.adjustSize()
            layout.invalidate()
            layout.activate()
            self.updateGeometry()
            return self.sizeHint().height()
        finally:
            layout.setSizeConstraint(constraint)

    def _resize_to_content(self) -> None:
        self.resize(self.width(), self._content_height())

    def _target_geometry(self, origin: QRect, height: int) -> QRect:
        screen = (
            QApplication.screenAt(self.frameGeometry().center())
            or QApplication.primaryScreen()
        )
        if screen is not None:
            height = min(height, screen.availableGeometry().height())
        return QRect(origin.x(), origin.y(), origin.width(), height)

    @staticmethod
    def _natural_height(widget: QWidget) -> int:
        widget.setMaximumHeight(UNLIMITED_HEIGHT)
        if widget.layout() is not None:
            widget.layout().activate()
        widget.adjustSize()
        return max(
            widget.sizeHint().height(), widget.minimumSizeHint().height()
        )

    def _animate_sections(
        self,
        *,
        show: tuple[QWidget, ...] = (),
        hide: tuple[QWidget, ...] = (),
    ) -> None:
        if self._anim_group is None:
            self._anim_group = QParallelAnimationGroup(self)
        group = self._anim_group
        group.stop()
        group.clear()
        try:
            group.finished.disconnect()
        except (TypeError, RuntimeError):
            pass

        if not self.isVisible():
            for widget in hide:
                widget.setVisible(False)
            for widget in show:
                widget.setVisible(True)
            for widget in (*show, *hide):
                widget.setMaximumHeight(UNLIMITED_HEIGHT)
            self._resize_to_content()
            return

        origin = self.geometry()
        starts = {
            widget: widget.height() if widget.isVisible() else 0
            for widget in (*show, *hide)
        }

        for widget in hide:
            widget.setVisible(False)
        for widget in show:
            widget.setVisible(True)
        ends = {widget: self._natural_height(widget) for widget in show}
        target_height = self._content_height()

        for widget in (*show, *hide):
            widget.setVisible(True)
            widget.setMinimumHeight(0)
            widget.setMaximumHeight(starts[widget])
            animation = create_max_height_animation(widget, group)
            animation.setStartValue(starts[widget])
            animation.setEndValue(ends.get(widget, 0))
            group.addAnimation(animation)

        self.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.setMinimumHeight(0)

        geometry = QPropertyAnimation(self, b"geometry", group)
        geometry.setDuration(SECTION_COLLAPSE_DURATION_MS)
        geometry.setEasingCurve(SECTION_COLLAPSE_EASING)
        geometry.setStartValue(origin)
        geometry.setEndValue(self._target_geometry(origin, target_height))
        group.addAnimation(geometry)

        group.finished.connect(
            lambda: self._finish_sections(show, hide),
            Qt.ConnectionType.SingleShotConnection,
        )
        group.start()

    def _finish_sections(
        self, show: tuple[QWidget, ...], hide: tuple[QWidget, ...]
    ) -> None:
        for widget in hide:
            widget.setVisible(False)
        for widget in (*show, *hide):
            widget.setMaximumHeight(UNLIMITED_HEIGHT)
        self.layout().setSizeConstraint(
            QLayout.SizeConstraint.SetDefaultConstraint
        )
        self._resize_to_content()

    def _on_per_entry_toggled(self, checked: bool) -> None:
        self.output_hint.setText(
            "Each transcription becomes its own file inside the chosen folder."
            if checked
            else "Everything is written to a single file. Turn on \u201cOne "
            "file per transcription\u201d to fill a folder instead."
        )
        self._refresh_default_path()

    def _on_path_changed(self, _text: str) -> None:
        self._refresh_export_enabled()

    def _refresh_export_enabled(self) -> None:
        self._refresh_selected_count()
        if self._worker is not None:
            self.export_btn.setEnabled(False)
            return
        ready = bool(self.path_edit.text().strip())
        if self.selected_radio.isChecked():
            ready = ready and bool(self._checked_ids())
        self.export_btn.setEnabled(ready)

    def _refresh_selected_count(self) -> None:
        if not hasattr(self, "selected_count_label"):
            return
        checked = len(self._checked_ids())
        total = self.selected_list.count()
        self.selected_count_label.setText(f"{checked} of {total} selected")

    def _refresh_default_path(self) -> None:
        if not hasattr(self, "path_edit"):
            return
        current = self.path_edit.text().strip()
        if current and current not in self._default_paths():
            return
        self.path_edit.setText(self._default_path())

    def _default_paths(self) -> set[str]:
        folder = os.path.dirname(os.path.abspath(config.RECORDINGS_FOLDER))
        paths = {os.path.join(folder, "history_export")}
        for extension in _FORMAT_EXTENSIONS.values():
            paths.add(os.path.join(folder, f"openwhisper_history.{extension}"))
        return paths

    def _default_path(self) -> str:
        folder = os.path.dirname(os.path.abspath(config.RECORDINGS_FOLDER))
        if self.per_entry_check.isChecked():
            return os.path.join(folder, "history_export")
        extension = _FORMAT_EXTENSIONS.get(self._current_format(), "md")
        return os.path.join(folder, f"openwhisper_history.{extension}")

    def _current_format(self) -> str:
        return self.format_combo.currentData() or FORMAT_MARKDOWN

    def _checked_ids(self) -> List[str]:
        ids: List[str] = []
        for row in range(self.selected_list.count()):
            item = self.selected_list.item(row)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            entry_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
            if entry_id:
                ids.append(entry_id)
        return ids

    def _on_browse(self) -> None:
        if self.per_entry_check.isChecked():
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
            "Export History",
            self._default_path(),
            f"{label} (*.{extension});;All Files (*)",
        )
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path = f"{path}.{extension}"
        self.path_edit.setText(path)

    def _resolve_targets(self) -> List[Dict[str, Any]]:
        if self.selected_radio.isChecked():
            checked = set(self._checked_ids())
            return [
                entry
                for entry in self._entries
                if str(entry.get("id") or "") in checked
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
        return filter_export_entries(
            list(self._entries),
            from_dt=from_dt,
            to_dt=to_dt,
            only_with_audio=self.audio_only_check.isChecked(),
        )

    def _on_export(self) -> None:
        if self._worker is not None:
            return
        targets = self._resolve_targets()
        if not targets:
            QMessageBox.information(
                self,
                "Export History",
                "No transcriptions match the selected criteria.",
            )
            return
        params = {
            "targets": targets,
            "fmt": self._current_format(),
            "per_entry": self.per_entry_check.isChecked(),
            "output": self.path_edit.text().strip(),
            "include_cleaned": self.include_cleaned_check.isChecked(),
            "include_raw": self.include_raw_check.isChecked(),
        }
        self._cancel_requested = False
        self._set_busy(True)
        self.progress.emit(0, len(targets), "")
        self._worker = threading.Thread(
            target=self._export_worker,
            args=(params,),
            name="history-export",
            daemon=True,
        )
        self._worker.start()

    def _export_worker(self, params: Dict[str, Any]) -> None:
        try:
            entries: List[Dict[str, Any]] = []
            targets = params["targets"]
            total = len(targets)
            for index, entry in enumerate(targets):
                if self._cancel_requested:
                    self.export_canceled.emit()
                    return
                entries.append(entry)
                self.progress.emit(
                    index + 1, total, entry.get("preview_text") or ""
                )
            if self._cancel_requested:
                self.export_canceled.emit()
                return
            fmt = params["fmt"]
            output = params["output"]
            include_cleaned = params["include_cleaned"]
            include_raw = params["include_raw"]
            if params["per_entry"]:
                write_per_entry_files(
                    entries,
                    fmt,
                    output,
                    include_cleaned=include_cleaned,
                    include_raw=include_raw,
                )
                summary = output
            else:
                document = render_export_document(
                    entries,
                    fmt,
                    include_cleaned=include_cleaned,
                    include_raw=include_raw,
                )
                parent = os.path.dirname(output)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(output, "w", encoding="utf-8") as handle:
                    handle.write(document)
                summary = output
            self.export_finished.emit(len(entries), summary)
        except Exception as exc:
            logger.exception("History export failed")
            self.export_failed.emit(str(exc))

    def _set_busy(self, busy: bool) -> None:
        self.export_btn.setVisible(not busy)
        self.close_btn.setVisible(not busy)
        self.cancel_work_btn.setVisible(busy)
        self.cancel_work_btn.setEnabled(True)
        self.progress_bar.setVisible(busy)
        if not busy:
            self.progress_label.setText("")
            self.progress_bar.reset()
        self._refresh_export_enabled()
        self._resize_to_content()

    def _on_progress(self, done: int, total: int, title: str) -> None:
        if total <= 0:
            self.progress_bar.set_indeterminate()
        else:
            self.progress_bar.set_fraction(done / total)
        label = f"Exporting {done}/{total}"
        if title:
            label = f"{label} — {title}"
        self.progress_label.setText(label)

    def _on_export_finished(self, count: int, summary: str) -> None:
        self._worker = None
        self._set_busy(False)
        noun = "transcription" if count == 1 else "transcriptions"
        QMessageBox.information(
            self,
            "Export History",
            f"Exported {count} {noun} to:\n{summary}",
        )

    def _on_export_failed(self, message: str) -> None:
        self._worker = None
        self._set_busy(False)
        QMessageBox.warning(
            self,
            "Export Failed",
            f"Could not export history:\n{message}",
        )

    def _on_export_canceled(self) -> None:
        self._worker = None
        self._set_busy(False)
        self.progress_label.setText("Export canceled — nothing was written")

    def _on_cancel_work(self) -> None:
        self._cancel_requested = True
        self.cancel_work_btn.setEnabled(False)
        self.progress_label.setText("Canceling…")

    def reject(self) -> None:
        if self._worker is not None:
            self._on_cancel_work()
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._worker is not None:
            self._on_cancel_work()
            event.ignore()
            return
        super().closeEvent(event)
