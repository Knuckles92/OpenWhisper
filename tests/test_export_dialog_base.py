"""Shared export behavior exercised through both concrete dialogs."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication, QLayout

from ui_qt.dialogs.history_export_dialog import HistoryExportDialog
from ui_qt.dialogs.meeting_export_dialog import MeetingExportDialog
from ui_qt.utils.collapse_animation import UNLIMITED_HEIGHT


@pytest.fixture(params=["history", "meeting"])
def export_dialog(request):
    app = QApplication.instance() or QApplication([])
    entries = [
        {"id": "a", "preview_text": "Alpha", "title": "Alpha"},
        {"id": "b", "preview_text": "Beta", "title": "Beta"},
    ]
    if request.param == "history":
        dialog = HistoryExportDialog(entry_provider=lambda: entries)
    else:
        dialog = MeetingExportDialog(meeting_provider=lambda: entries)
    yield dialog
    dialog._worker = None
    dialog.close()
    dialog.deleteLater()
    app.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("font_scale", [1.0, 1.3])
def test_scope_animation_can_reverse_without_leaving_height_constraints(
    export_dialog, font_scale
):
    dialog = export_dialog
    font = dialog.font()
    font.setPointSizeF(font.pointSizeF() * font_scale)
    dialog.setFont(font)
    dialog.show()
    QApplication.processEvents()
    dialog._resize_to_content()
    original_width = dialog.width()
    dialog.selected_radio.setChecked(True)
    group = dialog._anim_group
    group.setCurrentTime(group.duration() // 2)
    interrupted_height = dialog.selected_panel.height()

    dialog.all_radio.setChecked(True)
    animations = [group.animationAt(i) for i in range(group.animationCount())]
    shrinking = next(a for a in animations if a.targetObject() is dialog.selected_panel)
    assert shrinking.startValue() == interrupted_height
    group.setCurrentTime(group.duration())
    QApplication.processEvents()

    assert dialog.selected_panel.isHidden()
    assert dialog.filters_panel.isVisible()
    assert dialog.selected_panel.maximumHeight() == UNLIMITED_HEIGHT
    assert dialog.filters_panel.maximumHeight() == UNLIMITED_HEIGHT
    assert dialog.layout().sizeConstraint() == QLayout.SizeConstraint.SetDefaultConstraint
    assert dialog.height() == dialog._content_height()
    assert dialog.width() == original_width


def test_hidden_scope_and_date_changes_are_ready_when_shown(export_dialog):
    dialog = export_dialog
    dialog.selected_radio.setChecked(True)
    dialog.date_range_check.setChecked(True)
    dialog.all_radio.setChecked(True)
    assert dialog.selected_panel.isHidden()
    assert not dialog.filters_panel.isHidden()
    assert not dialog.date_range_row.isHidden()
    assert dialog.date_range_row.maximumHeight() == UNLIMITED_HEIGHT


def test_select_all_applies_only_to_search_matches(export_dialog):
    dialog = export_dialog
    dialog.selected_radio.setChecked(True)
    dialog.selected_search.setText("Alpha")
    dialog._set_all_checked(True)
    assert dialog._checked_ids() == ["a"]
    assert dialog.selected_count_label.text() == "1 of 2 selected"
    assert dialog.export_btn.isEnabled()

    dialog.selected_search.clear()
    dialog._set_all_checked(True)
    dialog.selected_search.setText("Beta")
    dialog._set_all_checked(False)
    assert dialog._checked_ids() == ["a"]
    assert dialog.selected_list.item(1).checkState() == Qt.CheckState.Unchecked


def test_close_during_export_requests_cancel_and_keeps_dialog_open(export_dialog):
    dialog = export_dialog
    dialog.show()
    QApplication.processEvents()
    dialog._worker = object()
    dialog._set_busy(True)
    dialog._on_progress(1, 2, "Alpha")
    assert dialog.progress_label.text() == "Exporting 1/2 — Alpha"
    assert not dialog.export_btn.isVisible()
    assert dialog.progress_bar.isVisible()

    dialog.close()
    assert dialog.isVisible()
    assert dialog._cancel_requested
    assert not dialog.cancel_work_btn.isEnabled()
    dialog.export_canceled.emit()
    assert dialog._worker is None
    assert dialog.export_btn.isVisible()
    assert dialog.close_btn.isVisible()
    assert not dialog.progress_bar.isVisible()
    assert dialog.progress_label.text() == "Export canceled — nothing was written"


def test_changing_format_during_scope_animation_finishes_both_sections(export_dialog):
    dialog = export_dialog
    dialog.show()
    QApplication.processEvents()
    dialog.selected_radio.setChecked(True)
    group = dialog._anim_group
    group.setCurrentTime(group.duration() // 2)
    dialog.format_combo.setCurrentIndex(dialog.format_combo.findData("json"))
    group.setCurrentTime(group.duration())
    QApplication.processEvents()
    assert dialog.filters_panel.isHidden()
    assert dialog.selected_panel.isVisible()
    assert dialog.selected_panel.maximumHeight() == UNLIMITED_HEIGHT
    assert dialog.filters_panel.maximumHeight() == UNLIMITED_HEIGHT
    assert dialog.markdown_only_hint.isVisible()
