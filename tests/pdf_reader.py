"""Read a written PDF back with PDFium, the engine QtPdf wraps.

PyQt6 6.12 moved QtPdf into PyQt6-WebEngine, and the app itself never reads
PDFs, so the tests use pypdfium2 instead of pulling in Chromium.
"""
from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium
from PyQt6.QtGui import QImage


def page_texts(path: Path) -> list[str]:
    """Each page's text, in reading order."""
    pdf = pdfium.PdfDocument(str(path))
    try:
        return [page.get_textpage().get_text_range() for page in pdf]
    finally:
        pdf.close()


def metadata(path: Path) -> dict[str, str]:
    pdf = pdfium.PdfDocument(str(path))
    try:
        return pdf.get_metadata_dict()
    finally:
        pdf.close()


def page_size(path: Path, index: int) -> tuple[float, float]:
    """The page's width and height in points."""
    pdf = pdfium.PdfDocument(str(path))
    try:
        return pdf[index].get_size()
    finally:
        pdf.close()


def render(path: Path, index: int, scale: float = 1.0) -> QImage:
    """The page at ``scale`` pixels per point, transparent where nothing is painted."""
    pdf = pdfium.PdfDocument(str(path))
    try:
        bitmap = pdf[index].render(scale=scale, fill_color=(0, 0, 0, 0))
        assert bitmap.mode == "BGRA"
        # PDFium's BGRA is Qt's ARGB32 on a little-endian machine.
        return QImage(
            bytes(bitmap.buffer), bitmap.width, bitmap.height, bitmap.stride,
            QImage.Format.Format_ARGB32,
        ).copy()
    finally:
        pdf.close()
