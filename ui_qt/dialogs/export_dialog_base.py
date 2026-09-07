"""Shared selection, sizing, animation, and worker feedback for export dialogs."""
from __future__ import annotations

import threading

from PyQt6.QtCore import (
    QParallelAnimationGroup,
    QPropertyAnimation,
    QRect,
    Qt,
    pyqtSignal,
)
from PyQt6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLayout, QWidget

from ui_qt.utils.collapse_animation import (
    SECTION_COLLAPSE_DURATION_MS,
    SECTION_COLLAPSE_EASING,
    UNLIMITED_HEIGHT,
    create_max_height_animation,
)
from ui_qt.widgets import Button, PrimaryButton


class ExportDialogBase(QDialog):
    """Subclasses own their sections, export criteria, paths, and worker."""

    progress = pyqtSignal(int, int, str)
    export_finished = pyqtSignal(int, str)
    export_failed = pyqtSignal(str)
    export_canceled = pyqtSignal()

    def __init__(self, parent=None):
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
