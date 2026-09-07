"""Optional macOS auto-paste setup, shared by startup and Settings."""

import logging

from PyQt6.QtCore import QTimer, QUrl, Qt
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QPushButton, QVBoxLayout

from services._hotkey_pynput import accessibility_app_bundle
from services.hotkey_manager import (
    accessibility_permission_instructions,
    is_accessibility_trusted,
    request_accessibility_trust,
)
from services.settings import SettingsKey, settings_manager
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)


class AccessibilityDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set up auto-paste")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(520)
        self.resize(560, 400)
        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        def paragraph(text):
            label = WrappedLabel(text)
            label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(label)
            return label

        paragraph("Paste transcriptions into the app you’re using")
        paragraph(
            "Auto-paste needs Accessibility access. Recording and global hotkeys "
            "work without it, and you can paste copied text with ⌘V."
        )
        self.bundle = accessibility_app_bundle()
        name = self.bundle.name if self.bundle else "OpenWhisper"
        if self.bundle:
            instructions = (
                f"1. Open Accessibility Settings below.\n"
                f"2. Turn on {name}.\n"
                "3. Come back here — we’ll check access automatically."
            )
        else:
            instructions = accessibility_permission_instructions()
        paragraph(instructions)
        if self.bundle:
            path_label = paragraph(f"App to allow: {self.bundle}")
            path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.recovery = paragraph(
            "Use + to add the app "
            f"shown above. If an older {name} entry is enabled but access still "
            "fails, remove that entry with −, add this copy, and enable it. "
            "If access still isn’t detected, quit and reopen OpenWhisper."
        )
        self.recovery.setVisible(False)
        help_button = QPushButton("Already enabled or missing from the list?")
        help_button.setCheckable(True)
        help_button.toggled.connect(self.recovery.setVisible)
        layout.addWidget(help_button)
        self.status = paragraph("")
        actions = QHBoxLayout()
        self.open_button = QPushButton("Open Accessibility Settings")
        self.open_button.clicked.connect(self._open_settings)
        actions.addWidget(self.open_button)
        if self.bundle:
            self.finder_button = QPushButton("Show App in Finder")
            self.finder_button.clicked.connect(self._reveal_app)
            actions.addWidget(self.finder_button)
        layout.addLayout(actions)
        self.done_button = QPushButton("Continue with clipboard")
        self.done_button.clicked.connect(self.close)
        layout.addWidget(self.done_button)
        paragraph("We won’t ask again at launch. Set up auto-paste anytime in Settings → General.")

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh_status)
        self.refresh_status()

    def showEvent(self, event):
        super().showEvent(event)
        try:
            settings_manager.save_setting(SettingsKey.MACOS_ACCESSIBILITY_INTRO_SEEN, True)
        except Exception:
            logger.exception("Could not remember Accessibility introduction")
        self.refresh_status()
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def refresh_status(self):
        trusted = is_accessibility_trusted()
        self.status.setText(
            "Accessibility access is enabled. Permission setup is complete."
            if trusted else "Access not detected yet. Clipboard paste is available."
        )
        self.done_button.setText("Done" if trusted else "Continue with clipboard")

    def _open_settings(self):
        if not is_accessibility_trusted():
            request_accessibility_trust()
        opened = QDesktopServices.openUrl(QUrl(
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
        ))
        if not opened:
            self.status.setText("Open System Settings → Privacy & Security → Accessibility.")

    def _reveal_app(self):
        # Reveal the bundle itself so it can be dragged into the permissions list.
        from AppKit import NSWorkspace
        from Foundation import NSURL

        NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_(
            [NSURL.fileURLWithPath_(str(self.bundle))]
        )


def show_accessibility_setup(parent):
    """Keep a single non-modal setup window per owner."""
    existing = parent.findChild(AccessibilityDialog)
    if existing is not None:
        existing.show()
        existing.raise_()
        existing.activateWindow()
        return existing
    dialog = AccessibilityDialog(parent)
    dialog.show()
    return dialog
