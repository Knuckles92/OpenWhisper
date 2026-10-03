"""Compact app chrome for a compositor-owned window."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMenuBar

from services.desktop_session import use_omarchy_ui


class DesktopHeader(QFrame):
    """Application menus stay available; moving and sizing belong to Hyprland."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("desktopHeader")
        self.setStyleSheet("""
            QFrame#desktopHeader {
                background: @bg;
                border-bottom: 1px solid @border;
            }
            QLabel#desktopBrand {
                color: @text-heading;
                font-size: 13px;
                font-weight: 600;
                padding: 6px 0;
            }
            QLabel#desktopBadge {
                color: @accent;
                font-size: 10px;
                font-weight: 600;
                padding: 3px 6px;
                border: 1px solid @accent-muted;
            }
            QMenuBar { background: transparent; color: @text; border: none; }
            QMenuBar::item { padding: 6px 8px; }
            QMenuBar::item:selected { background: @surface-hover; }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(10)
        self.title_label = QLabel("OpenWhisper")
        self.title_label.setObjectName("desktopBrand")
        layout.addWidget(self.title_label)
        self.badge = QLabel("OMARCHY")
        self.badge.setObjectName("desktopBadge")
        self.badge.setVisible(use_omarchy_ui())
        layout.addWidget(self.badge)
        layout.addStretch()
        self.menu_bar = QMenuBar()
        self.menu_bar.setNativeMenuBar(False)
        layout.addWidget(self.menu_bar, 0, Qt.AlignmentFlag.AlignVCenter)

    def resizeEvent(self, event):
        self.badge.setVisible(use_omarchy_ui() and event.size().width() >= 520)
        super().resizeEvent(event)
