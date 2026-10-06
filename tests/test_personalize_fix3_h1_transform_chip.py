"""A transform's History chip names it once, as the runtime saves it."""

import importlib
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from services import focus_context
from services.focus_context import FocusSnapshot, TextContext
from services.models import TranscriptionHistory
from tests.test_personalize_s3_command_delivery import (  # noqa: F401  (fixtures)
    NOTEPAD, FakeCleaner, FakeService, _qapp, _run_submitted, h,
)


def _saved_transform(h):  # noqa: F811
    focus_context.set_service(FakeService(
        FocusSnapshot(NOTEPAD, TextContext(selected="teh draft", selection_known=True)),
        current=NOTEPAD))
    h.runtime._transcript_cleanup = FakeCleaner("The draft.")
    h.controller.command_runtime.run_transform("fix-grammar")
    deadline = time.monotonic() + 2
    while not h.submitted and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.002)
    _run_submitted(h)
    fields, = h.history.entries
    history = importlib.import_module("services.history_manager").history_manager
    return history.get_entry_by_id(history.add_entry(**fields).id)


def test_a_transform_run_through_the_runtime_is_named_once(h):  # noqa: F811
    from ui_qt.dialogs.history_entry_dialog import HistoryEntryDialog
    from ui_qt.widgets.history_sidebar import HistoryItemWidget

    entry = _saved_transform(h)
    card = HistoryItemWidget(entry)
    assert card.kind_chip.text() == "Transform · Fix grammar"
    dialog = HistoryEntryDialog(entry)
    chip = dialog.findChild(QLabel, "historyEntryKindChip")
    assert chip.text() == "Transform · Fix grammar"
    dialog.close()
    card.deleteLater()


def test_a_bare_or_missing_transform_name_still_reads_well():
    from ui_qt.widgets.history_sidebar import _kind_label

    bare = TranscriptionHistory.create(text="x", model="base", source_name="Polish", entry_kind="transform")
    nameless = TranscriptionHistory.create(text="x", model="base", source_name="", entry_kind="transform")
    assert _kind_label(bare) == "Transform · Polish"
    assert _kind_label(nameless) == "Transform"
