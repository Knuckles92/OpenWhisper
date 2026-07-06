"""Shared timing and helpers for collapsible section animations."""
from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, Qt

# Keep in sync with MainWindow section resize animations.
SECTION_COLLAPSE_DURATION_MS = 300
SECTION_COLLAPSE_EASING = QEasingCurve.Type.InOutCubic
UNLIMITED_HEIGHT = 16777215


def create_max_height_animation(widget, parent=None) -> QPropertyAnimation:
    """Create a reusable maximumHeight animation for a section body."""
    anim = QPropertyAnimation(widget, b"maximumHeight", parent or widget)
    anim.setDuration(SECTION_COLLAPSE_DURATION_MS)
    anim.setEasingCurve(SECTION_COLLAPSE_EASING)
    return anim


def run_max_height_animation(
    anim: QPropertyAnimation,
    *,
    start: int,
    end: int,
    on_finished,
) -> None:
    """Run a maximumHeight animation, replacing any in-flight run."""
    anim.stop()
    anim.targetObject().setMaximumHeight(start)
    anim.setStartValue(start)
    anim.setEndValue(end)

    try:
        anim.finished.disconnect()
    except (TypeError, RuntimeError):
        pass
    anim.finished.connect(on_finished, Qt.ConnectionType.SingleShotConnection)
    anim.start()
