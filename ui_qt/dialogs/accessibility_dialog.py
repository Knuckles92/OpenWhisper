"""Optional macOS auto-paste setup, shared by startup and Settings."""

import logging

from PyQt6.QtCore import QTimer, QUrl, Qt
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout

from services._hotkey_pynput import accessibility_app_bundle
from services.hotkey_manager import (
    accessibility_permission_instructions,
    is_accessibility_trusted,
    request_accessibility_trust,
)
from services.settings import SettingsKey, settings_manager
from ui_qt.widgets.buttons import Button, compact_primary_button, neutral_button
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)


class AccessibilityDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("accessibilityDialog")
        self.setWindowTitle("Set up auto-paste")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(520)
        # As short as the copy allows; the labels raise the floor as they wrap.
        self.resize(560, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        def paragraph(text, name="consentBodyLabel"):
            label = WrappedLabel(text)
            label.setObjectName(name)
            label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(label)
            return label

        paragraph("Paste transcriptions into the app you’re using", "headerLabel")
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
        # Empty off macOS, where there is no Accessibility pane to describe.
        paragraph(instructions).setVisible(bool(instructions))
        if self.bundle:
            path_label = paragraph(f"App to allow: {self.bundle}", "infoLabel")
            path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        # Secondary helpers sit with the steps; the footer holds the choice.
        helpers = QHBoxLayout()
        helpers.setSpacing(8)
        if self.bundle:
            self.finder_button = neutral_button(Button("Show App in Finder"))
            self.finder_button.clicked.connect(self._reveal_app)
            helpers.addWidget(self.finder_button)
        help_button = neutral_button(Button("Already enabled or missing from the list?"))
        help_button.setCheckable(True)
        help_button.setAutoDefault(False)
        helpers.addWidget(help_button)
        helpers.addStretch()
        layout.addLayout(helpers)
        self.recovery = paragraph(
            "Use + to add the app "
            f"shown above. If an older {name} entry is enabled but access still "
            "fails, remove that entry with −, add this copy, and enable it. "
            "If access still isn’t detected, quit and reopen OpenWhisper."
        )
        self.recovery.setVisible(False)
        help_button.toggled.connect(self.recovery.setVisible)
        self.status = paragraph("")
        paragraph(
            "We won’t ask again at launch. Set up auto-paste anytime in Settings → General.",
            "infoLabel",
        )

        layout.addStretch(1)
        layout.addSpacing(4)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch()
        self.done_button = neutral_button(Button("Continue with clipboard"))
        self.done_button.clicked.connect(self.close)
        actions.addWidget(self.done_button)
        self.open_button = compact_primary_button(Button("Open Accessibility Settings"))
        self.open_button.clicked.connect(self._open_settings)
        actions.addWidget(self.open_button)
        layout.addLayout(actions)
        self.open_button.setDefault(True)
        # Otherwise the first helper takes focus, and Enter would click it.
        self.open_button.setFocus()
        if self.bundle:
            self.finder_button.setAutoDefault(False)

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
