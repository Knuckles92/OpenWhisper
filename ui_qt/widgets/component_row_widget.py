"""Single-component row for the Downloads dialog.

Shows one downloadable component's identity, its install state, its size, and
the actions that apply (Install / Cancel / Remove / Repair), plus a
determinate progress bar while an install runs. Clicking the row body opens
the bundled profile popup; install and remove stay on the action buttons.

Styling deliberately mirrors :mod:`ui_qt.widgets.model_row_widget` so the
Components group and the model list read as one list.
"""
import logging
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
)

from services.components import ComponentInfo, ComponentState, InstallPhase
from services.format_utils import format_size_bytes
from ui_qt.widgets.buttons import Button, DangerButton, PrimaryButton

logger = logging.getLogger(__name__)

# Child labels must set an explicit transparent background — the global
# ``QWidget { background-color: #1c1c1e }`` rule otherwise paints dark
# rectangles on top of the lighter row fill.
_ROW_STYLE = """
    QFrame#componentRow {
        background-color: #141b22;
        border: 1px solid #303b45;
        border-radius: 12px;
    }
    QFrame#componentRow:hover {
        background-color: #182028;
        border: 1px solid #3d4a57;
    }
    QFrame#componentRow:focus {
        border: 1px solid #2a5382;
        outline: none;
    }
    QFrame#componentRow[selected="true"] {
        background-color: #17263a;
        border: 1px solid #3a6aa3;
    }
    QLabel#componentRowName {
        color: #e8edf2;
        background-color: transparent;
        border: none;
        font-weight: 600;
    }
    QLabel#componentRowSummary {
        color: #98a3b0;
        background-color: transparent;
        border: none;
    }
    QLabel#componentRowSize {
        color: #c7d0d9;
        background-color: transparent;
        border: none;
    }
    QLabel#componentRowBadge {
        background-color: rgba(141, 154, 167, 0.12);
        color: #aeb8c3;
        border: 1px solid rgba(141, 154, 167, 0.28);
        border-radius: 6px;
        padding: 2px 8px;
        font-size: 10px;
        font-weight: 600;
    }
    QLabel#componentRowBadge[tone="downloading"] {
        background-color: rgba(10, 132, 255, 0.14);
        color: #6fb1ff;
        border: 1px solid rgba(10, 132, 255, 0.28);
    }
    QLabel#componentRowBadge[tone="installed"] {
        background-color: rgba(48, 209, 88, 0.12);
        color: #32d74b;
        border: 1px solid rgba(48, 209, 88, 0.28);
    }
    QLabel#componentRowBadge[tone="warning"] {
        background-color: rgba(255, 159, 10, 0.14);
        color: #ff9f0a;
        border: 1px solid rgba(255, 159, 10, 0.32);
    }
    QProgressBar#componentProgress {
        background-color: #243039;
        border: none;
        border-radius: 3px;
        max-height: 6px;
        min-height: 6px;
        text-align: center;
        color: transparent;
    }
    QProgressBar#componentProgress::chunk {
        background-color: #0a84ff;
        border-radius: 3px;
    }
    QPushButton#componentInstallButton,
    QPushButton#componentRemoveButton {
        border-radius: 7px;
        padding: 4px 10px;
        font-size: 11px;
        font-weight: 600;
        min-height: 28px;
        max-height: 28px;
    }
    QPushButton#componentInstallButton {
        background-color: rgba(10, 132, 255, 0.18);
        color: #6fb1ff;
        border: 1px solid rgba(10, 132, 255, 0.32);
    }
    QPushButton#componentInstallButton:hover {
        background-color: rgba(10, 132, 255, 0.28);
        border: 1px solid rgba(10, 132, 255, 0.5);
    }
    QPushButton#componentInstallButton:disabled {
        background-color: #1b252e;
        color: #5d6873;
        border: 1px solid #263038;
    }
    QPushButton#componentRemoveButton {
        background-color: transparent;
        color: #ff6961;
        border: 1px solid #3b4752;
    }
    QPushButton#componentRemoveButton:hover {
        background-color: rgba(255, 69, 58, 0.14);
        border: 1px solid rgba(255, 69, 58, 0.45);
    }
"""

# Badge text and tone per state. "installing" is handled separately because it
# carries live progress.
_STATE_BADGES = {
    ComponentState.NOT_INSTALLED: ("Not installed", "idle"),
    ComponentState.EXTERNAL: ("Available", "installed"),
    ComponentState.INSTALLED: ("Installed", "installed"),
    ComponentState.UPDATE_AVAILABLE: ("Update available", "warning"),
    ComponentState.INCOMPATIBLE: ("Update required", "warning"),
    ComponentState.BROKEN: ("Damaged", "warning"),
}

# Primary button label per state. Absent states fall back to "Install".
_PRIMARY_LABELS = {
    ComponentState.NOT_INSTALLED: "Install",
    ComponentState.UPDATE_AVAILABLE: "Update",
    ComponentState.INCOMPATIBLE: "Reinstall",
    ComponentState.BROKEN: "Repair",
}

_PHASE_LABELS = {
    InstallPhase.RESOLVING: "Preparing…",
    InstallPhase.DOWNLOADING: "Downloading…",
    InstallPhase.VERIFYING: "Verifying…",
    InstallPhase.EXTRACTING: "Installing…",
    InstallPhase.FINALIZING: "Finishing…",
}


class ComponentRowWidget(QFrame):
    """One row in the Model Manager's Components group.

    The row is "dumb": it renders whatever state is handed to
    :meth:`update_state` / :meth:`set_progress` and re-emits button clicks with
    its component id. Download and install logic stays with the controller.
    """

    install_clicked = pyqtSignal(str)
    cancel_clicked = pyqtSignal(str)
    remove_clicked = pyqtSignal(str)
    details_requested = pyqtSignal(str)

    def __init__(self, component_id: str, parent=None):
        """Initialize the row for one component.

        Args:
            component_id: Stable component identifier (see ``ComponentId``).
        """
        super().__init__(parent)
        self.component_id = component_id
        self._installing = False

        self.setObjectName("componentRow")
        self.setStyleSheet(_ROW_STYLE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip("Click to view component details")
        self.setAccessibleName(f"{component_id} component")
        self.setAccessibleDescription(
            "Open component details. Install and remove actions are separate."
        )
        self._setup_ui()

    def _setup_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 12, 10)
        outer.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(12)

        identity = QVBoxLayout()
        identity.setSpacing(2)

        from ui_qt.widgets.eliding_label import ElidingLabel
        self.name_label = ElidingLabel(self.component_id)
        self.name_label.setObjectName("componentRowName")
        name_font = QFont("Segoe UI", 10)
        name_font.setBold(True)
        self.name_label.setFont(name_font)
        identity.addWidget(self.name_label)

        self.summary_label = ElidingLabel("")
        self.summary_label.setObjectName("componentRowSummary")
        self.summary_label.setFont(QFont("Segoe UI", 8))
        identity.addWidget(self.summary_label)

        row.addLayout(identity, stretch=1)

        self.size_label = ElidingLabel("")
        self.size_label.setObjectName("componentRowSize")
        self.size_label.setFont(QFont("Segoe UI", 9))
        self.size_label.setMinimumWidth(110)
        self.size_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(self.size_label)

        self.badge = QLabel("")
        self.badge.setObjectName("componentRowBadge")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setFixedHeight(22)
        row.addWidget(self.badge)

        self.install_button = PrimaryButton("Install")
        self.install_button.setObjectName("componentInstallButton")
        self._compact_button(self.install_button, 92)
        self.install_button.clicked.connect(self._on_primary_clicked)
        row.addWidget(self.install_button)

        self.remove_button = DangerButton("Remove")
        self.remove_button.setObjectName("componentRemoveButton")
        self._compact_button(self.remove_button, 80)
        self.remove_button.clicked.connect(
            lambda: self.remove_clicked.emit(self.component_id)
        )
        row.addWidget(self.remove_button)

        outer.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setObjectName("componentProgress")
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.hide()
        outer.addWidget(self.progress)

    @staticmethod
    def _compact_button(button: Button, width: int) -> None:
        """Apply dialog-sized dimensions to a shared application button."""
        button.set_base_minimum_size(width, 28)
        button.setMinimumWidth(width)
        button.setMaximumWidth(width)
        button.setMinimumHeight(28)
        button.setMaximumHeight(28)
        button.setFont(QFont("Segoe UI", 10))

    def _on_primary_clicked(self) -> None:
        """Route the primary button to install or cancel, whichever applies."""
        if self._installing:
            self.cancel_clicked.emit(self.component_id)
        else:
            self.install_clicked.emit(self.component_id)

    def _set_badge(self, text: str, tone: str) -> None:
        """Update badge text and dynamic tone property for QSS styling."""
        self.badge.setText(text)
        self.badge.setProperty("tone", tone)
        # Re-polish so the dynamic property selector takes effect.
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)
        self.badge.update()

    def update_state(self, info: ComponentInfo, installing: bool) -> None:
        """Render the row for a component state.

        Args:
            info: Current component description.
            installing: True while an install for this component is in flight.
        """
        self._installing = installing
        self.name_label.setText(info.display_name)

        summary = info.summary
        if info.reason:
            summary = f"{summary}  —  {info.reason}" if summary else info.reason
        self.summary_label.setText(summary)

        if installing:
            self._set_badge("Downloading…", "downloading")
            self.install_button.setText("Cancel")
            self.install_button.setEnabled(True)
            self.remove_button.hide()
            self.progress.show()
            return

        self.progress.hide()
        self.progress.setValue(0)

        text, tone = _STATE_BADGES.get(info.state, ("Unknown", "idle"))
        self._set_badge(text, tone)

        if info.state in (ComponentState.EXTERNAL, ComponentState.INSTALLED):
            self.size_label.setText(
                "Existing setup"
                if info.state == ComponentState.EXTERNAL
                else format_size_bytes(info.install_bytes)
            )
            self.install_button.hide()
            self.remove_button.setVisible(info.state == ComponentState.INSTALLED)
        else:
            if info.download_bytes:
                self.size_label.setText(f"{format_size_bytes(info.download_bytes)} download")
            else:
                self.size_label.setText("")

            self.install_button.show()
            self.install_button.setText(_PRIMARY_LABELS.get(info.state, "Install"))
            # The catalog ships with the app, so a component always has a size
            # to download. This previously guarded against an unreachable remote
            # catalog, which no longer exists.
            self.install_button.setEnabled(True)
            self.install_button.setToolTip("")
            # UPDATE_AVAILABLE belongs here too: a pending update is an offer,
            # not an obligation, and the user must still be able to remove the
            # component outright instead of being forced to update it first.
            self.remove_button.setVisible(
                info.state in (
                    ComponentState.BROKEN,
                    ComponentState.INCOMPATIBLE,
                    ComponentState.UPDATE_AVAILABLE,
                )
            )

    def set_progress(self, phase: str, done: int, total: int) -> None:
        """Update the progress bar and badge during an install.

        Args:
            phase: One of the :class:`~services.components.InstallPhase` values.
            done: Units completed (bytes while downloading, entries while
                extracting).
            total: Total units, or 0 when unknown.
        """
        if not self._installing:
            return

        self._set_badge(_PHASE_LABELS.get(phase, "Working…"), "downloading")

        if total <= 0:
            # Indeterminate: Qt animates a busy bar when min == max == 0.
            self.progress.setRange(0, 0)
            self.size_label.setText("")
            return

        self.progress.setRange(0, 100)
        self.progress.setValue(int(done * 100 / total))

        if phase == InstallPhase.DOWNLOADING:
            self.size_label.setText(
                f"{format_size_bytes(done)} of {format_size_bytes(total)}"
            )
        else:
            self.size_label.setText("")

    @staticmethod
    def _is_action_child(widget) -> bool:
        while widget is not None:
            if isinstance(widget, QAbstractButton):
                return True
            widget = widget.parentWidget()
        return False

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.position().toPoint())
            if not self._is_action_child(child):
                self.details_requested.emit(self.component_id)
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self.details_requested.emit(self.component_id)
            event.accept()
            return
        super().keyPressEvent(event)
