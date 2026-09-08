"""Exercise shared Qt cleanup across test boundaries, even with live wrappers."""

import pytest
from PyQt6 import sip
from PyQt6.QtWidgets import QWidget


@pytest.fixture(scope="module")
def shared_widget(_session_qt_application):
    widget = QWidget()
    yield widget
    widget.deleteLater()


@pytest.fixture(scope="module")
def previous_widgets():
    # Retain wrappers so garbage collection cannot accidentally make this pass.
    return []


@pytest.mark.parametrize("iteration", range(3))
def test_widgets_are_destroyed_between_tests(shared_widget, previous_widgets, iteration):
    assert not sip.isdeleted(shared_widget)
    assert all(sip.isdeleted(widget) for widget in previous_widgets)

    closed = QWidget()
    closed.setObjectName(f"closed-window-{iteration}")
    child = QWidget(closed)
    closed.close()
    deferred = QWidget()
    deferred.deleteLater()
    added_to_shared = QWidget(shared_widget)
    previous_widgets.extend((closed, child, deferred, added_to_shared))
