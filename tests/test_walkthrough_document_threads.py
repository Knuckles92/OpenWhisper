"""Walkthrough documents built on a worker while the main thread handles events."""
import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import QSizeF
from PyQt6.QtWidgets import QApplication

from services.walkthrough import document, word

WRITE_UP = (
    "# Steps\n\n1. Sign in.\n2. Invoices hang.\n\n- one\n- two\n\n"
    "| Step | Result |\n|---|---|\n| Sign in | Stalls |\n\n3. Then it recovers.\n"
)


def _write_pairs(count, folder, errors):
    for _ in range(count):
        try:
            document.write_markdown_pdf(WRITE_UP, folder / "w.pdf", folder=folder, title="Steps")
            word.write_markdown_docx(WRITE_UP, folder / "w.docx", folder=folder, title="Steps")
        except Exception as exc:
            errors.append(exc)
            return


def test_write_ups_written_back_to_back_on_a_worker_stay_usable(tmp_path):
    # Lists emptied, or documents dropped, on the worker used to make PyQt
    # mark a later document's layout, frame or table deleted while in use:
    # this failed within 25 pairs, mostly within a few.
    errors = []
    worker = threading.Thread(target=_write_pairs, args=(80, tmp_path, errors))
    worker.start()
    while worker.is_alive():
        QApplication.processEvents()
    assert errors == []


def test_a_worker_never_waits_for_a_blocked_main_thread(tmp_path):
    errors = []
    worker = threading.Thread(target=_write_pairs, args=(3, tmp_path, errors))
    worker.start()
    worker.join(10)
    assert not worker.is_alive() and errors == []
    QApplication.processEvents()


def test_a_document_built_on_a_worker_is_deleted_on_the_main_thread(tmp_path):
    built = {}

    def build():
        composer = document._MarkdownComposer(QSizeF(600, 800), 96, tmp_path)
        built["doc"], _groups = composer.layout(WRITE_UP)
        built["layout"] = built["doc"].documentLayout()
        document._release(built["doc"])

    worker = threading.Thread(target=build)
    worker.start()
    worker.join(10)
    assert not sip.isdeleted(built["doc"])
    QApplication.processEvents()
    assert sip.isdeleted(built["doc"]) and sip.isdeleted(built["layout"])


def test_write_ups_can_be_written_on_the_main_thread(tmp_path):
    errors = []
    _write_pairs(3, tmp_path, errors)
    assert errors == []
