"""Shared layout, selection, sizing, animation, and worker for export dialogs.

The history and Past Meetings export dialogs differ only in their copy, the
criteria checkbox, the two "include" options, and how an item turns into a
document; everything else lives here. Collection and rendering run on a
worker thread, and every UI update arrives via signals so nothing touches
widgets off the Qt thread. Cancel is cooperative and checked between items —
nothing is written until collection finishes.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, time
from typing import Any, Callable, Dict, List, Optional

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

# The format values both export services accept (``meeting.export.bulk`` and
# ``services.history_export`` define the same three).
FORMAT_MARKDOWN = "markdown"
FORMAT_TXT = "txt"
FORMAT_JSON = "json"

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


class ExportDialogBase(QDialog):
    """Pick items, criteria, and a format, then export them to disk.

    Subclasses set the copy below and implement the hooks at the end of the
    class: the criteria checkbox, the include options, list labels, the
    default location, filtering, and how collected entries are written.
    """

    #: Prefix of every styled object name (``<prefix>Dialog``, ``Card``...).
    _NAME_PREFIX = ""
    #: Window title and heading; also titles the message boxes.
    _TITLE = ""
    _SUBTITLE = ""
    _LOAD_ERROR = ""
    _LOAD_LOG = ""
    _EMPTY_TEXT = ""
    _SCOPE_EYEBROW = ""
    _ALL_LABEL = ""
    _SELECTED_LABEL = ""
    _SEARCH_PLACEHOLDER = ""
    #: Singular and plural name of one exported item.
    _NOUN = ""
    _NOUN_PLURAL = ""
    _MARKDOWN_HINT = ""
    _FAILED_TEXT = ""
    _FAILED_LOG = ""
    _DEFAULT_DIR_NAME = ""
    _DEFAULT_FILE_STEM = ""
    _THREAD_NAME = ""

    progress = pyqtSignal(int, int, str)
    export_finished = pyqtSignal(int, str)
    export_failed = pyqtSignal(str)
    export_canceled = pyqtSignal()

    def __init__(
        self,
        parent=None,
        item_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    ):
        super().__init__(parent)
        self.setMinimumWidth(600)
        self.setMaximumWidth(680)
        self.setModal(True)
        self._worker: threading.Thread | None = None
        self._cancel_requested = False
        self._anim_group: QParallelAnimationGroup | None = None
        self._section_targets: dict[QWidget, bool] = {}
        self.progress.connect(self._on_progress)
        self.export_finished.connect(self._on_export_finished)
        self.export_failed.connect(self._on_export_failed)
        self.export_canceled.connect(self._on_export_canceled)

        self.setObjectName(f"{self._NAME_PREFIX}Dialog")
        self.setWindowTitle(self._TITLE)
        self._item_provider = item_provider
        self._items: List[Dict[str, Any]] = []
        self._load_items()
        self._setup_ui()

    def _load_items(self) -> None:
        try:
            self._items = list(self._item_provider() or [])
            self._load_error = ""
        except Exception as exc:
            logger.exception(self._LOAD_LOG)
            self._items = []
            self._load_error = str(exc)

    # ---- layout ----

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(14)

        title = QLabel(self._TITLE)
        title.setObjectName("modelManagerTitle")
        layout.addWidget(title)
        subtitle = WrappedLabel(self._SUBTITLE)
        subtitle.setObjectName("modelManagerSubtitle")
        layout.addWidget(subtitle)

        if self._load_error or not self._items:
            text = (
                f"{self._LOAD_ERROR}:\n{self._load_error}"
                if self._load_error
                else self._EMPTY_TEXT
            )
            notice = WrappedLabel(text)
            notice.setObjectName("infoLabel")
            layout.addWidget(notice)
            layout.addStretch()
            close_btn = Button("Close")
            close_btn.clicked.connect(self.reject)
            row = QHBoxLayout()
            row.addStretch()
            row.addWidget(close_btn)
            layout.addLayout(row)
            return

        self.scope_card = self._build_scope_card()
        layout.addWidget(self.scope_card)
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
        frame.setObjectName(f"{self._NAME_PREFIX}Card")
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(16, 12, 16, 14)
        inner.setSpacing(10)
        label = QLabel(eyebrow)
        label.setObjectName(f"{self._NAME_PREFIX}Eyebrow")
        inner.addWidget(label)
        return inner, frame

    def _meta_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(f"{self._NAME_PREFIX}Meta")
        return label

    def _mini_button(self, text: str) -> Button:
        button = Button(text)
        button.setObjectName(f"{self._NAME_PREFIX}MiniBtn")
        return button

    def _build_scope_card(self) -> QWidget:
        inner, frame = self._card(self._SCOPE_EYEBROW)

        scope_row = QHBoxLayout()
        scope_row.setSpacing(20)
        self.all_radio = QRadioButton(self._ALL_LABEL)
        self.all_radio.setChecked(True)
        self.selected_radio = QRadioButton(self._SELECTED_LABEL)
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
        self.filters_panel.setObjectName(f"{self._NAME_PREFIX}Pane")
        panel_layout = QVBoxLayout(self.filters_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(8)

        self.date_range_check = QCheckBox("Limit to a date range")
        self.date_range_check.toggled.connect(self._on_date_range_toggled)
        panel_layout.addWidget(self.date_range_check)

        self.date_range_row = QWidget()
        self.date_range_row.setObjectName(f"{self._NAME_PREFIX}Pane")
        date_row = QHBoxLayout(self.date_range_row)
        date_row.setContentsMargins(28, 0, 0, 0)
        date_row.setSpacing(8)
        from_label = self._meta_label("From")
        self.from_date = NoWheelDateEdit()
        self.from_date.setDate(QDate.currentDate().addMonths(-1))
        to_label = self._meta_label("To")
        self.to_date = NoWheelDateEdit()
        self.to_date.setDate(QDate.currentDate())
        date_row.addWidget(from_label)
        date_row.addWidget(self.from_date, 1)
        date_row.addWidget(to_label)
        date_row.addWidget(self.to_date, 1)
        self.date_range_row.setVisible(False)
        panel_layout.addWidget(self.date_range_row)

        panel_layout.addWidget(self._build_criteria_check())
        return self.filters_panel

    def _build_selected_panel(self) -> QWidget:
        self.selected_panel = QWidget()
        self.selected_panel.setObjectName(f"{self._NAME_PREFIX}Pane")
        panel_layout = QVBoxLayout(self.selected_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(8)

        self.selected_search = QLineEdit()
        self.selected_search.setPlaceholderText(self._SEARCH_PLACEHOLDER)
        self.selected_search.setClearButtonEnabled(True)
        self.selected_search.textChanged.connect(self._filter_selected_list)
        panel_layout.addWidget(self.selected_search)

        self.selected_list = QListWidget()
        self.selected_list.setObjectName(f"{self._NAME_PREFIX}List")
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
        for item in self._items:
            self.selected_list.addItem(self._make_list_item(item))
        self.selected_list.itemChanged.connect(
            lambda _item: self._refresh_export_enabled()
        )
        panel_layout.addWidget(self.selected_list)

        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(8)
        select_all_btn = self._mini_button("Select all")
        select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        clear_btn = self._mini_button("Clear")
        clear_btn.clicked.connect(lambda: self._set_all_checked(False))
        toggle_row.addWidget(select_all_btn)
        toggle_row.addWidget(clear_btn)
        toggle_row.addStretch()
        self.selected_count_label = self._meta_label("")
        toggle_row.addWidget(self.selected_count_label)
        panel_layout.addLayout(toggle_row)
        return self.selected_panel

    def _make_list_item(self, entry: Dict[str, Any]) -> QListWidgetItem:
        label = self._item_label(entry)
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

        self.per_item_check = QCheckBox(f"One file per {self._NOUN}")
        self.per_item_check.toggled.connect(self._on_per_item_toggled)
        inner.addWidget(self.per_item_check)

        self._include_checks = self._build_include_checks()
        for check in self._include_checks:
            check.setChecked(True)
            inner.addWidget(check)

        self.markdown_only_hint = WrappedLabel(self._MARKDOWN_HINT)
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
        browse_btn = self._mini_button("Browse…")
        browse_btn.clicked.connect(self._on_browse)
        output_row.addWidget(self.path_edit, 1)
        output_row.addWidget(browse_btn)
        inner.addLayout(output_row)

        self.output_hint = WrappedLabel(self._single_file_hint())
        self.output_hint.setObjectName("infoLabel")
        inner.addWidget(self.output_hint)
        return frame

    def _single_file_hint(self) -> str:
        return (
            "Everything is written to a single file. Turn on “One file "
            f"per {self._NOUN}” to fill a folder instead."
        )

    def _build_progress_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self.progress_bar = AnimatedProgressBar()
        self.progress_bar.reset()
        self.progress_bar.setVisible(False)
        self.progress_label = WrappedLabel("")
        self.progress_label.setObjectName(f"{self._NAME_PREFIX}Progress")
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

    # ---- selection and criteria ----

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
        for check in self._include_checks:
            check.setEnabled(markdown)
        self._refresh_default_path()
        self._animate_sections(
            show=() if markdown else (self.markdown_only_hint,),
            hide=(self.markdown_only_hint,) if markdown else (),
        )

    def _on_per_item_toggled(self, checked: bool) -> None:
        self.output_hint.setText(
            f"Each {self._NOUN} becomes its own file inside the chosen folder."
            if checked
            else self._single_file_hint()
        )
        self._refresh_default_path()

    def _sections(self) -> tuple[QWidget, ...]:
        names = (
            "date_range_row",
            "markdown_only_hint",
            "filters_panel",
            "selected_panel",
            "scope_card",
            "content_card",
            "output_card",
        )
        return tuple(
            widget
            for widget in (getattr(self, name, None) for name in names)
            if widget is not None
        )

    def _resolve_targets(self) -> List[Dict[str, Any]]:
        # Filters only accompany the "all" scope; an explicit selection is
        # exported exactly as checked.
        if self.selected_radio.isChecked():
            checked = set(self._checked_ids())
            return [
                item
                for item in self._items
                if str(item.get("id") or "") in checked
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
        return self._filter_items(list(self._items), from_dt, to_dt)

    # ---- sizing and animation ----

    def _content_height(self) -> int:
        """Height the dialog needs for the sections currently visible.

        Measuring has to activate the layout, which would otherwise resize the
        window to the result on the spot — visible as a one-frame jump ahead of
        an animation. ``SetNoConstraint`` keeps the pass read-only.
        """
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
        # A different control can interrupt a transition; keep its unfinished
        # sections in the new animation so none stay visible or height-clamped.
        self._section_targets.update((widget, True) for widget in show)
        self._section_targets.update((widget, False) for widget in hide)
        show = tuple(w for w, visible in self._section_targets.items() if visible)
        hide = tuple(w for w, visible in self._section_targets.items() if not visible)
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
            self._finish_sections(show, hide)
            return

        # Interrupting a swap resumes from the height on screen rather than
        # restarting from the section's collapsed or natural extreme.
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

        # The window follows the animation, not the layout's own sizing, until
        # the sections reach their final heights. The layout's minimum height is
        # released too, or every frame of a collapse clamps to the pre-swap
        # minimum and the window only snaps down once the animation ends.
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
        self._section_targets.clear()
        for widget in hide:
            widget.setVisible(False)
        for widget in (*show, *hide):
            widget.setMaximumHeight(UNLIMITED_HEIGHT)
        self.layout().setSizeConstraint(
            QLayout.SizeConstraint.SetDefaultConstraint
        )
        self._resize_to_content()

    # ---- output path ----

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
        folder = self._export_root()
        paths = {os.path.join(folder, self._DEFAULT_DIR_NAME)}
        for extension in _FORMAT_EXTENSIONS.values():
            paths.add(
                os.path.join(folder, f"{self._DEFAULT_FILE_STEM}.{extension}")
            )
        return paths

    def _default_path(self) -> str:
        folder = self._export_root()
        if self.per_item_check.isChecked():
            return os.path.join(folder, self._DEFAULT_DIR_NAME)
        extension = _FORMAT_EXTENSIONS.get(self._current_format(), "md")
        return os.path.join(folder, f"{self._DEFAULT_FILE_STEM}.{extension}")

    def _current_format(self) -> str:
        return self.format_combo.currentData() or FORMAT_MARKDOWN

    def _on_browse(self) -> None:
        if self.per_item_check.isChecked():
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
            self._TITLE,
            self._default_path(),
            f"{label} (*.{extension});;All Files (*)",
        )
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path = f"{path}.{extension}"
        self.path_edit.setText(path)

    def _checked_ids(self) -> list[str]:
        ids: list[str] = []
        for row in range(self.selected_list.count()):
            item = self.selected_list.item(row)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            entry_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
            if entry_id:
                ids.append(entry_id)
        return ids

    # ---- export ----

    def _on_export(self) -> None:
        if self._worker is not None:
            return
        targets = self._resolve_targets()
        if not targets:
            QMessageBox.information(
                self,
                self._TITLE,
                f"No {self._NOUN_PLURAL} match the selected criteria.",
            )
            return
        params = {
            "targets": targets,
            "fmt": self._current_format(),
            "per_item": self.per_item_check.isChecked(),
            "output": self.path_edit.text().strip(),
            "options": self._include_options(),
        }
        self._cancel_requested = False
        self._set_busy(True)
        self.progress.emit(0, len(targets), "")
        self._worker = threading.Thread(
            target=self._export_worker,
            args=(params,),
            name=self._THREAD_NAME,
            daemon=True,
        )
        self._worker.start()

    def _export_worker(self, params: Dict[str, Any]) -> None:
        try:
            collect = self._entry_collector()
            entries: List[Dict[str, Any]] = []
            targets = params["targets"]
            total = len(targets)
            for index, item in enumerate(targets):
                if self._cancel_requested:
                    self.export_canceled.emit()
                    return
                entry = collect(item)
                if entry is not None:
                    entries.append(entry)
                self.progress.emit(index + 1, total, self._progress_title(item))
            if self._cancel_requested:
                self.export_canceled.emit()
                return
            fmt = params["fmt"]
            output = params["output"]
            options = params["options"]
            if params["per_item"]:
                self._write_per_item_files(entries, fmt, output, **options)
            else:
                document = self._render_document(entries, fmt, **options)
                parent = os.path.dirname(output)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(output, "w", encoding="utf-8") as handle:
                    handle.write(document)
            self.export_finished.emit(len(entries), output)
        except Exception as exc:
            logger.exception(self._FAILED_LOG)
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
        noun = self._NOUN if count == 1 else self._NOUN_PLURAL
        QMessageBox.information(
            self,
            self._TITLE,
            f"Exported {count} {noun} to:\n{summary}",
        )

    def _on_export_failed(self, message: str) -> None:
        self._worker = None
        self._set_busy(False)
        QMessageBox.warning(
            self,
            "Export Failed",
            f"{self._FAILED_TEXT}:\n{message}",
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

    # ---- subclass hooks ----

    def _build_criteria_check(self) -> QCheckBox:
        """The "only items with ..." checkbox under the date range."""
        raise NotImplementedError

    def _build_include_checks(self) -> tuple[QCheckBox, ...]:
        """The Markdown-only "Include ..." checkboxes; they start checked."""
        raise NotImplementedError

    def _include_options(self) -> Dict[str, bool]:
        """Keyword arguments the include checkboxes pass to the writers."""
        raise NotImplementedError

    def _item_label(self, item: Dict[str, Any]) -> str:
        raise NotImplementedError

    def _export_root(self) -> str:
        """Folder the default export file or folder is placed in."""
        raise NotImplementedError

    def _filter_items(
        self,
        items: List[Dict[str, Any]],
        from_dt: Optional[datetime],
        to_dt: Optional[datetime],
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def _entry_collector(self) -> Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Return item -> export entry, or None to skip; runs on the worker."""
        raise NotImplementedError

    def _progress_title(self, item: Dict[str, Any]) -> str:
        raise NotImplementedError

    def _write_per_item_files(self, entries, fmt: str, output: str, **options) -> None:
        raise NotImplementedError

    def _render_document(self, entries, fmt: str, **options) -> str:
        raise NotImplementedError
