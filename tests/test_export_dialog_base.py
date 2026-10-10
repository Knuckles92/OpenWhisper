"""Shared export behavior exercised through both concrete dialogs."""
from __future__ import annotations

import gc
import os
import threading
import weakref

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
    assert dialog._cancel_requested.is_set()
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


def test_format_values_match_both_export_services():
    from meeting.export import bulk
    from services import history_export
    from ui_qt.dialogs import export_dialog_base as base

    for name in ("FORMAT_MARKDOWN", "FORMAT_TXT", "FORMAT_JSON"):
        assert getattr(base, name) == getattr(bulk, name)
        assert getattr(base, name) == getattr(history_export, name)


def _run_export(dialog, per_item):
    """Run an export on its worker to the end and return what the dialog reported."""
    from unittest.mock import patch

    from PyQt6.QtWidgets import QMessageBox

    emitted = []
    dialog.export_finished.connect(lambda count, out: emitted.append((count, out)))
    dialog.export_failed.connect(lambda message: emitted.append(("failed", message)))
    dialog.per_item_check.setChecked(per_item)
    with patch.object(QMessageBox, "information"), patch.object(QMessageBox, "warning"):
        dialog._on_export()
        worker = dialog._worker
        worker.join(5)
        assert not worker.is_alive()
        QApplication.processEvents()
    assert dialog._worker is None
    return emitted


@pytest.mark.parametrize("per_item", [False, True])
def test_history_worker_passes_include_options_to_writers(
    monkeypatch, tmp_path, per_item
):
    calls = []
    monkeypatch.setattr(
        "ui_qt.dialogs.history_export_dialog.render_export_document",
        lambda entries, fmt, **kw: calls.append(("doc", entries, fmt, kw)) or "doc",
    )
    monkeypatch.setattr(
        "ui_qt.dialogs.history_export_dialog.write_per_entry_files",
        lambda entries, fmt, out, **kw: calls.append(("files", entries, fmt, kw)),
    )
    app = QApplication.instance() or QApplication([])
    entries = [{"id": "a", "preview_text": "Alpha"}, {"id": "b", "preview_text": "Beta"}]
    dialog = HistoryExportDialog(entry_provider=lambda: entries)
    try:
        dialog.include_raw_check.setChecked(False)
        dialog.path_edit.setText(str(tmp_path / "out.md"))
        emitted = _run_export(dialog, per_item)
        kind, written, fmt, options = calls[0]
        assert kind == ("files" if per_item else "doc")
        assert [entry["id"] for entry in written] == ["a", "b"]
        assert fmt == "markdown"
        assert options == {"include_cleaned": True, "include_raw": False}
        assert emitted == [(2, str(tmp_path / "out.md"))]
        if not per_item:
            assert (tmp_path / "out.md").read_text(encoding="utf-8") == "doc"
    finally:
        dialog.deleteLater()
        app.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("per_item", [False, True])
def test_meeting_worker_loads_entries_and_skips_missing(
    monkeypatch, tmp_path, per_item
):
    calls = []
    monkeypatch.setattr(
        "ui_qt.dialogs.meeting_export_dialog.render_export_document",
        lambda entries, fmt, **kw: calls.append(("doc", entries, fmt, kw)) or "doc",
    )
    monkeypatch.setattr(
        "ui_qt.dialogs.meeting_export_dialog.write_per_meeting_files",
        lambda entries, fmt, out, **kw: calls.append(("files", entries, fmt, kw)),
    )
    app = QApplication.instance() or QApplication([])
    meetings = [{"id": "a", "title": "Alpha"}, {"id": "b", "title": "Beta"}]
    loaded = {"a": {"meeting": "a"}}
    dialog = MeetingExportDialog(
        meeting_provider=lambda: meetings, entry_loader=loaded.get
    )
    try:
        dialog.include_intelligence_check.setChecked(False)
        dialog.path_edit.setText(str(tmp_path / "out.md"))
        emitted = _run_export(dialog, per_item)
        kind, written, fmt, options = calls[0]
        assert kind == ("files" if per_item else "doc")
        assert written == [{"meeting": "a"}]
        assert options == {"include_transcript": True, "include_intelligence": False}
        assert emitted == [(1, str(tmp_path / "out.md"))]
    finally:
        dialog.deleteLater()
        app.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("kind", ["history", "meeting"])
def test_a_dialog_deleted_mid_export_is_not_held_by_the_worker(monkeypatch, tmp_path, kind):
    # The worker used to run as a method of the dialog: it kept a deleted
    # dialog alive, then dropped the last reference (deleting a QWidget) or
    # emitted on it from its own thread, which can crash the process.
    gate = threading.Event()

    def render(entries, fmt, **options):
        gate.wait(5)
        return "doc"

    monkeypatch.setattr(f"ui_qt.dialogs.{kind}_export_dialog.render_export_document", render)
    items = [{"id": "a", "preview_text": "Alpha", "title": "Alpha"}]
    if kind == "history":
        dialog = HistoryExportDialog(entry_provider=lambda: items)
    else:
        dialog = MeetingExportDialog(
            meeting_provider=lambda: items, entry_loader=lambda meeting_id: {"id": meeting_id}
        )
    dialog.path_edit.setText(str(tmp_path / "out.md"))
    dialog._on_export()
    workers = [t for t in threading.enumerate() if t.name == dialog._THREAD_NAME]
    ref = weakref.ref(dialog)
    dialog.deleteLater()
    del dialog
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    # PyQt frees the dialog's own lambda connections with a queued call and
    # then a deferred delete; flush both.
    QApplication.processEvents()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    gc.collect()
    assert ref() is None
    gate.set()
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    QApplication.processEvents()
