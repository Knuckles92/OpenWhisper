"""ElidingLabel sizing when a stylesheet pads or borders the text.

A ``padding`` or ``border`` rule lands in the label's contents margins. The
label must reserve that chrome in its size hint and elide against the area the
text is actually painted into, otherwise a layout that grants exactly the hint
clips the last characters without ever showing an ellipsis.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from ui_qt.widgets.eliding_label import ElidingLabel

PADDING = 8
BORDER = 1
CHROME = 2 * (PADDING + BORDER)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _padded(text: str) -> ElidingLabel:
    label = ElidingLabel(text)
    label.setStyleSheet(
        f"QLabel {{ padding: 3px {PADDING}px; border: {BORDER}px solid red; }}"
    )
    return label


def test_size_hint_reserves_the_stylesheet_chrome(app):
    text = "25 European languages"
    label = _padded(text)
    advance = label.fontMetrics().horizontalAdvance(text)
    assert label.sizeHint().width() >= advance + CHROME


@pytest.mark.parametrize("font_size", [10, 11, 12, 13, 14, 15])
def test_full_text_is_shown_at_its_own_size_hint(app, font_size):
    text = "25 European languages"
    label = _padded(text)
    label.setStyleSheet(
        label.styleSheet()
        + f"QLabel {{ font-size: {font_size}px; font-weight: 600; }}"
    )
    label.ensurePolished()
    label.resize(label.sizeHint())
    label.show()
    app.processEvents()
    assert QLabel.text(label) == text


def test_elision_fits_inside_the_padded_area(app):
    text = "A secondary line that is far too long for the room it is given"
    label = _padded(text)
    label.resize(120, label.sizeHint().height())
    label.show()
    app.processEvents()
    shown = QLabel.text(label)
    assert shown != text
    assert shown.endswith("…")
    assert label.fontMetrics().horizontalAdvance(shown) <= label.contentsRect().width()


def test_minimum_width_still_ignores_text_length(app):
    short = _padded("Multilingual")
    long = _padded("x" * 200)
    assert long.minimumSizeHint().width() == short.minimumSizeHint().width()
    assert long.minimumSizeHint().width() < long.fontMetrics().horizontalAdvance(
        long.text()
    )


def test_size_hint_is_the_same_whether_or_not_currently_elided(app):
    text = "A secondary line that is far too long for the room it is given"
    elided = _padded(text)
    elided.resize(120, elided.sizeHint().height())
    elided.show()
    app.processEvents()
    assert QLabel.text(elided) != text

    fresh = _padded(text)
    assert elided.sizeHint().width() == fresh.sizeHint().width()

    # A plain QLabel with the same text and style is the reference for how
    # much room the text plus its chrome needs.
    plain = QLabel(text)
    plain.setStyleSheet(fresh.styleSheet())
    plain.ensurePolished()
    # ElidingLabel rounds fractional advances up to prevent clipping; QLabel
    # can round down by one pixel with macOS font metrics.
    assert 0 <= elided.sizeHint().width() - plain.sizeHint().width() <= 1
