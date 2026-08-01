"""Single-component row for the Model Manager dialog.

Shows one downloadable component's identity, its install state, its size, and
the actions that apply (Install / Cancel / Remove / Repair), plus a
determinate progress bar while an install runs.

Styling deliberately mirrors :mod:`ui_qt.widgets.model_row_widget` so the
Components group and the model list read as one list.
"""
import logging
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
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
        background-color: rgba(44, 44, 46, 0.55);
        border: 1px solid rgba(255, 255, 255, 0.06);
        border-radius: 10px;
    }
    QFrame#componentRow:hover {
        background-color: rgba(58, 58, 60, 0.65);
        border: 1px solid rgba(10, 132, 255, 0.28);
    }
    QLabel#componentRowName {
        color: #f5f5f7;
        background-color: transparent;
        border: none;
        font-weight: 600;
    }
    QLabel#componentRowSummary {
        color: #8e8e93;
        background-color: transparent;
        border: none;
    }
    QLabel#componentRowSize {
        color: #aeaeb2;
        background-color: transparent;
        border: none;
    }
    QLabel#componentRowBadge {
        background-color: rgba(142, 142, 147, 0.14);
        color: #aeaeb2;
        border: 1px solid rgba(142, 142, 147, 0.28);
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
        background-color: rgba(255, 255, 255, 0.06);
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
        background-color: rgba(58, 58, 60, 0.4);
        color: #636366;
        border: 1px solid rgba(255, 255, 255, 0.06);
    }
    QPushButton#componentRemoveButton {
        background-color: transparent;
        color: #ff6961;
        border: 1px solid rgba(255, 255, 255, 0.1);
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
        self._setup_ui()

    def _setup_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 12, 10)
        outer.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(12)

        identity = QVBoxLayout()
        identity.setSpacing(2)

        self.name_label = QLabel(self.component_id)
        self.name_label.setObjectName("componentRowName")
        name_font = QFont("Segoe UI", 10)
        name_font.setBold(True)
        self.name_label.setFont(name_font)
        identity.addWidget(self.name_label)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("componentRowSummary")
        self.summary_label.setFont(QFont("Segoe UI", 8))
        self.summary_label.setWordWrap(True)
        identity.addWidget(self.summary_label)

        row.addLayout(identity, stretch=1)

        self.size_label = QLabel("")
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

        if info.state == ComponentState.INSTALLED:
            self.size_label.setText(format_size_bytes(info.install_bytes))
            self.install_button.hide()
            self.remove_button.show()
        else:
            if info.download_bytes:
                self.size_label.setText(f"{format_size_bytes(info.download_bytes)} download")
            else:
                self.size_label.setText("")

            self.install_button.show()
            self.install_button.setText(_PRIMARY_LABELS.get(info.state, "Install"))
            # Without a catalog there is nothing to fetch; keep the button
            # visible but inert so the row still explains itself.
            can_install = bool(info.download_bytes) or info.state == ComponentState.BROKEN
            self.install_button.setEnabled(can_install)
            self.install_button.setToolTip(
                "" if can_install
                else "Could not reach the download server. Check your connection and reopen this window."
            )
            self.remove_button.setVisible(
                info.state in (ComponentState.BROKEN, ComponentState.INCOMPATIBLE)
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
