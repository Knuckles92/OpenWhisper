"""Scale designed font-size declarations to the user's UI type size."""
from __future__ import annotations

import re
from typing import Optional

from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QWidget

_FONT_SIZE_RE = re.compile(
    r"(font-size:\s*)(\d+(?:\.\d+)?)(px|pt)",
    re.IGNORECASE,
)
_UNSCALED_PROP = "unscaledStyleSheet"
_SCALED_PROP = "scaledStyleSheet"
_BASE_APP_POINT_SIZE = 10

_current_percent: int = 100
_theme_manager = None
_scaling = False


def current_ui_font_scale() -> float:
    """The active scale as a multiplier of the designed theme (1.0 = 100%)."""
    return _current_percent / 100.0


def current_ui_font_scale_percent() -> int:
    return _current_percent


def scale_qss_fonts(stylesheet: str, scale: float) -> str:
    """Multiply every ``font-size`` in ``stylesheet`` by ``scale``.

    Other length properties are left alone so padding and control sizes stay
    at their designed values. A scale of 1.0 returns ``stylesheet`` unchanged.
    """
    if not stylesheet or abs(scale - 1.0) < 1e-6:
        return stylesheet

    def _replace(match: re.Match) -> str:
        prefix, number, unit = match.group(1), match.group(2), match.group(3)
        value = float(number) * scale
        if "." in number:
            scaled = f"{value:.1f}".rstrip("0").rstrip(".")
        else:
            scaled = str(max(1, int(round(value))))
        return f"{prefix}{scaled}{unit}"

    return _FONT_SIZE_RE.sub(_replace, stylesheet)


def apply_ui_font_scale(
    percent: int,
    *,
    app: Optional[QApplication] = None,
    theme_manager=None,
) -> None:
    """Apply ``percent`` to the application stylesheet, default font, and widgets.

    Widget-local stylesheets that set ``font-size`` are rewritten from a stored
    unscaled original so changing the size twice does not compound.
    """
    global _current_percent, _theme_manager

    _current_percent = int(percent)
    if theme_manager is not None:
        _theme_manager = theme_manager

    instance = app or QApplication.instance()
    if instance is None:
        return

    scale = _current_percent / 100.0
    manager = _theme_manager
    if manager is not None:
        sheet = manager.scaled_stylesheet(scale)
        if sheet:
            instance.setStyleSheet(sheet)

    instance.setFont(QFont("Segoe UI", max(6, int(round(_BASE_APP_POINT_SIZE * scale)))))
    _rescale_widget_stylesheets(instance, scale)


def _rescale_widget_stylesheets(app: QApplication, scale: float) -> None:
    global _scaling
    for widget in app.allWidgets():
        original = widget.property(_UNSCALED_PROP)
        if not original:
            sheet = widget.styleSheet()
            if not sheet or "font-size" not in sheet.lower():
                continue
            last_scaled = widget.property(_SCALED_PROP)
            if last_scaled and sheet == last_scaled:
                continue
            original = sheet
            widget.setProperty(_UNSCALED_PROP, original)
        scaled = scale_qss_fonts(original, scale)
        widget.setProperty(_SCALED_PROP, scaled)
        if widget.styleSheet() == scaled:
            continue
        _scaling = True
        try:
            widget.setStyleSheet(scaled)
        finally:
            _scaling = False


def _scale_widget_on_style_change(widget: QWidget, scale: float) -> None:
    """Treat a new widget stylesheet as designed-at-100% and scale it."""
    global _scaling
    if _scaling:
        return
    sheet = widget.styleSheet()
    if not sheet or "font-size" not in sheet.lower():
        return
    last_scaled = widget.property(_SCALED_PROP)
    if last_scaled and sheet == last_scaled:
        return
    widget.setProperty(_UNSCALED_PROP, sheet)
    scaled = scale_qss_fonts(sheet, scale)
    widget.setProperty(_SCALED_PROP, scaled)
    if scaled == sheet:
        return
    _scaling = True
    try:
        widget.setStyleSheet(scaled)
    finally:
        _scaling = False


class FontScaleFilter(QObject):
    """Scale widget-local ``font-size`` rules as widgets apply stylesheets.

    The application stylesheet is scaled in one pass. Controls that set their
    own stylesheet after that still need the same multiply, including menus
    and dialogs created later in the session.
    """

    def eventFilter(self, obj, event):
        if (
            event.type() == QEvent.Type.StyleChange
            and isinstance(obj, QWidget)
        ):
            _scale_widget_on_style_change(obj, current_ui_font_scale())
        return False
