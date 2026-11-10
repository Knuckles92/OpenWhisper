"""Walkthrough documents built on a worker while the main thread handles events."""
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSizeF
from PyQt6.QtGui import QTextTable
from PyQt6.QtWidgets import QApplication

from services.walkthrough import document

TABLE = "# Steps\n\n| Step | Result |\n|---|---|\n| Sign in | Stalls |\n\nThen it recovers.\n"


def _build(count, tmp_path, errors):
    for _ in range(count):
        try:
            composer = document._MarkdownComposer(QSizeF(600, 800), 96, tmp_path)
            doc, _groups = composer.layout(TABLE)
            tables = [frame for frame in doc.rootFrame().childFrames()
                      if isinstance(frame, QTextTable)]
            assert [(table.rows(), table.columns()) for table in tables] == [(2, 2)]
        except Exception as exc:
            errors.append(exc)
            return


def test_frames_stay_usable_in_documents_built_back_to_back_on_a_worker(tmp_path):
    # Without the main thread catching up first, about one document in a
    # hundred raised "wrapped C/C++ object of type QTextFrame has been deleted".
    errors = []
    worker = threading.Thread(target=_build, args=(600, tmp_path, errors))
    worker.start()
    while worker.is_alive():
        QApplication.processEvents()
    assert errors == []


def test_a_worker_does_not_wait_long_for_a_main_thread_that_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(document, "_SETTLE_SECONDS", 0.05)
    errors = []
    worker = threading.Thread(target=_build, args=(3, tmp_path, errors))
    started = time.monotonic()
    worker.start()
    worker.join(5)
    assert not worker.is_alive() and errors == []
    assert time.monotonic() - started < 3


def test_documents_can_be_built_on_the_main_thread(tmp_path):
    errors = []
    _build(5, tmp_path, errors)
    assert errors == []
