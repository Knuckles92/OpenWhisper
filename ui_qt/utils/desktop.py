"""Use actual Qt platform capabilities, not the presence of an XWayland display."""

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from services.desktop_session import is_wayland_session, use_omarchy_ui


def is_wayland() -> bool:
    app = QApplication.instance()
    if app is not None:
        return app.platformName().lower().startswith("wayland")
    return is_wayland_session()


def compositor_managed() -> bool:
    return is_wayland() or use_omarchy_ui()


def on_screen_position(frame: QRect, available: QRect) -> QPoint:
    """Where a window's frame goes so that all of it is on ``available``.

    Left where it is when it already fits; otherwise centred on ``available``,
    and never with its top-left corner, the title bar's, off it.
    """
    if available.contains(frame):
        return frame.topLeft()
    return QPoint(
        max(available.x(), available.x() + (available.width() - frame.width()) // 2),
        max(available.y(), available.y() + (available.height() - frame.height()) // 2),
    )


def without_window_buttons(flags):
    """Keep compositor management, but request no client titlebar controls."""
    return (flags | Qt.WindowType.CustomizeWindowHint) & ~(
        Qt.WindowType.WindowMinMaxButtonsHint
        | Qt.WindowType.WindowCloseButtonHint
        | Qt.WindowType.WindowContextHelpButtonHint
    )


def fit_desktop_dialog(dialog: QDialog) -> None:
    """Fit floating dialogs to logical screen space, scrolling oversized forms."""
    if dialog.isMaximized() or dialog.isFullScreen():
        return
    screen = dialog.screen()
    if screen is None:
        return
    available = screen.availableGeometry()
    room = QSize(max(1, available.width() - 64), max(1, available.height() - 64))
    layout = dialog.layout()
    if layout is not None:
        layout.activate()
        # A compositor can assign a smaller tile after the dialog opens. Keep
        # the form accessible without imposing its minimum on the surface.
        if not getattr(dialog, "_desktop_dialog_scroll", None):
            contents = QWidget()
            contents.setLayout(layout)
            scroll = QScrollArea(dialog)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setWidget(contents)
            outer = QVBoxLayout(dialog)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.addWidget(scroll)
            dialog._desktop_dialog_scroll = scroll
    dialog.setMinimumSize(QSize(240, 120).boundedTo(room))
    dialog.resize(dialog.size().boundedTo(room))


class DesktopWindowFilter(QObject):
    """Cover dialogs created later, including Qt's own message/file dialogs."""

    def eventFilter(self, obj, event):
        if isinstance(obj, QDialog) and use_omarchy_ui():
            if event.type() == QEvent.Type.Polish:
                obj.setWindowFlags(without_window_buttons(obj.windowFlags()))
                obj.setSizeGripEnabled(False)
            elif event.type() == QEvent.Type.Show and not event.spontaneous():
                fit_desktop_dialog(obj)
        return False
