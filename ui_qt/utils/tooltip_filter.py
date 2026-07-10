"""Application-level tooltip polish: rounded corners and a snappy delay.

QToolTip windows are opaque top-level widgets, so a QSS ``border-radius``
only clips the painted background — the window itself stays rectangular
and the corners show as square blocks. Making the tooltip window
translucent before it is shown lets the rounded QSS background render
against transparency instead.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import QProxyStyle, QStyle, QWidget


class SnappyTooltipStyle(QProxyStyle):
    """Shortens the native tooltip wake-up delay so hints appear promptly.

    The platform default (~700ms on Windows) makes ``setToolTip`` hints feel
    sluggish next to the instant ``HotkeyHoverHint`` pills. This proxy only
    overrides the wake-up style hint; everything else passes through to the
    wrapped platform style.
    """

    WAKE_UP_DELAY_MS = 200

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_ToolTip_WakeUpDelay:
            return self.WAKE_UP_DELAY_MS
        return super().styleHint(hint, option, widget, returnData)


class RoundedTooltipFilter(QObject):
    """Makes Qt's internal tooltip windows translucent for rounded corners.

    Install on the ``QApplication`` instance. Qt creates a fresh
    ``QTipLabel`` widget for every tooltip; this filter catches its
    Polish event (fired before the native window exists) and applies
    the attributes translucency requires.
    """

    def eventFilter(self, obj, event):
        if (
            event.type() == QEvent.Type.Polish
            and isinstance(obj, QWidget)
            and obj.inherits("QTipLabel")
        ):
            obj.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            # The native drop shadow is drawn for the rectangular window,
            # which would reintroduce square corners around the tooltip.
            obj.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        return False
