"""Re-apply stylesheet rules after a widget's dynamic property changes.

Qt evaluates property selectors such as ``QLabel[tone="warn"]`` when a widget
is polished, so changing the property alone does not restyle it.
"""
from typing import Any

from PyQt6.QtWidgets import QWidget


def repolish(widget: QWidget) -> None:
    """Restyle ``widget`` so property selectors match its current values."""
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


def set_style_property(widget: QWidget, name: str, value: Any) -> None:
    """Set a dynamic property that stylesheets select on, then restyle."""
    widget.setProperty(name, value)
    repolish(widget)
