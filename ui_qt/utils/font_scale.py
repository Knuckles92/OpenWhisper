"""Resolve designed stylesheets to the user's theme and UI type size.

Every stylesheet in the app — the application theme and the ones widgets set
on themselves — is written at 100% with ``@token`` colours. This module turns
that source into what Qt actually receives: palette tokens substituted and
every ``font-size`` multiplied by the chosen scale. Widgets keep their source
stylesheet in a dynamic property so a later theme or size change can redo the
resolution without compounding.
"""
from __future__ import annotations

import re
from typing import Optional

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QWidget

from services.settings import UiTheme
from ui_qt.utils.palette import DARK, LIGHT, resolve_tokens

_FONT_SIZE_RE = re.compile(
    r"(font-size:\s*)(\d+(?:\.\d+)?)(px|pt)",
    re.IGNORECASE,
)
_SOURCE_PROP = "unscaledStyleSheet"
_RESOLVED_PROP = "scaledStyleSheet"
_BASE_APP_POINT_SIZE = 10

_current_percent: int = 100
_theme_preference: str = UiTheme.DARK
_theme_manager = None
_resolving = False


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


def resolve_stylesheet(source: str, scale: Optional[float] = None) -> str:
    """Substitute palette tokens in ``source`` and scale its font sizes."""
    if scale is None:
        scale = current_ui_font_scale()
    return scale_qss_fonts(resolve_tokens(source), scale)


def _needs_resolution(sheet: str) -> bool:
    lowered = sheet.lower()
    return "font-size" in lowered or "@" in sheet


def apply_ui_font_scale(
    percent: int,
    *,
    app: Optional[QApplication] = None,
    theme_manager=None,
) -> None:
    """Apply ``percent`` to the application stylesheet, default font, and widgets.

    Widget-local stylesheets are re-resolved from their stored source so
    changing the size twice does not compound.
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
    _resolve_widget_stylesheets(instance, scale)


def current_ui_theme_preference() -> str:
    """The ``UiTheme`` value last applied (``dark``, ``light``, or ``system``)."""
    return _theme_preference


def resolve_theme_preference(
    preference: str, app: Optional[QApplication] = None
) -> str:
    """Map a ``UiTheme`` preference to a palette name.

    "System" reads the platform colour scheme, which is only meaningful while
    no explicit scheme is pinned on the style hints.
    """
    if preference == UiTheme.LIGHT:
        return LIGHT
    if preference != UiTheme.SYSTEM:
        return DARK
    instance = app or QApplication.instance()
    if instance is None:
        return DARK
    scheme = instance.styleHints().colorScheme()
    return LIGHT if scheme == Qt.ColorScheme.Light else DARK


def apply_ui_theme(
    preference: str,
    *,
    app: Optional[QApplication] = None,
    theme_manager=None,
) -> bool:
    """Apply a ``UiTheme`` preference and restyle the application and every widget.

    Returns True when the palette changed. The application stylesheet, the
    ``QPalette``, and each widget-local stylesheet are all re-resolved;
    painted widgets repaint because Qt repolishes everything when the
    application stylesheet is replaced.

    An explicit theme pins the platform colour scheme so the window frame
    matches. "System" must leave that hint alone: pinning it would replace
    the value the theme is resolved from and silence ``colorSchemeChanged``.
    """
    global _theme_manager, _theme_preference

    if theme_manager is not None:
        _theme_manager = theme_manager
    manager = _theme_manager
    if manager is None:
        return False

    _theme_preference = preference
    instance = app or QApplication.instance()
    follow_system = preference == UiTheme.SYSTEM
    if follow_system and instance is not None:
        try:
            instance.styleHints().unsetColorScheme()
        except AttributeError:  # Qt < 6.8
            pass

    changed = manager.set_theme(resolve_theme_preference(preference, instance))
    if instance is not None:
        manager.apply_platform_palette(instance, pin_color_scheme=not follow_system)
    if changed:
        apply_ui_font_scale(_current_percent, app=instance)
    return changed


def _resolve_widget_stylesheets(app: QApplication, scale: float) -> None:
    global _resolving
    for widget in app.allWidgets():
        source = widget.property(_SOURCE_PROP)
        if not source:
            sheet = widget.styleSheet()
            if not sheet or not _needs_resolution(sheet):
                continue
            last_resolved = widget.property(_RESOLVED_PROP)
            if last_resolved and sheet == last_resolved:
                continue
            source = sheet
            widget.setProperty(_SOURCE_PROP, source)
        resolved = resolve_stylesheet(source, scale)
        widget.setProperty(_RESOLVED_PROP, resolved)
        if widget.styleSheet() == resolved:
            continue
        _resolving = True
        try:
            widget.setStyleSheet(resolved)
        finally:
            _resolving = False


def _resolve_widget_on_style_change(widget: QWidget, scale: float) -> None:
    """Treat a new widget stylesheet as designed source and resolve it."""
    global _resolving
    if _resolving:
        return
    sheet = widget.styleSheet()
    if not sheet or not _needs_resolution(sheet):
        return
    last_resolved = widget.property(_RESOLVED_PROP)
    if last_resolved and sheet == last_resolved:
        return
    widget.setProperty(_SOURCE_PROP, sheet)
    resolved = resolve_stylesheet(sheet, scale)
    widget.setProperty(_RESOLVED_PROP, resolved)
    if resolved == sheet:
        return
    _resolving = True
    try:
        widget.setStyleSheet(resolved)
    finally:
        _resolving = False


class WidgetStyleFilter(QObject):
    """Resolve widget-local stylesheets as widgets apply them.

    The application stylesheet is resolved in one pass. Controls that set
    their own stylesheet after that still need the same token substitution
    and font multiply, including menus and dialogs created later in the
    session.
    """

    def eventFilter(self, obj, event):
        if (
            event.type() == QEvent.Type.StyleChange
            and isinstance(obj, QWidget)
        ):
            _resolve_widget_on_style_change(obj, current_ui_font_scale())
        return False
