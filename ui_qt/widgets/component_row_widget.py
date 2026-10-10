"""Single-component row for the Downloads dialog.

Shows one downloadable component's identity, its install state, its size, and
the actions that apply (Install / Cancel / Remove / Repair), plus a
determinate progress bar while an install runs. Clicking the row body opens
the bundled profile popup; install and remove stay on the action buttons.

Styling deliberately mirrors :mod:`ui_qt.widgets.model_row_widget` so the
Components group and the model list read as one list.
"""
import logging

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
)

from services.components import ComponentInfo, ComponentState, InstallPhase
from services.format_utils import format_size_bytes
from ui_qt.widgets.buttons import DangerButton, PrimaryButton
from ui_qt.widgets.download_row import DownloadRow, row_style

logger = logging.getLogger(__name__)

# Everything but the component-only badge tones comes from the shared row
# stylesheet.
_ROW_STYLE = row_style(
    "componentRow",
    primary="componentInstallButton",
    remove="componentRemoveButton",
    progress="componentProgress",
    extra="""
    QLabel#componentRowBadge[tone="installed"] {
        background-color: rgba(@success-rgb, 0.12);
        color: @success-text-strong;
        border: 1px solid rgba(@success-rgb, 0.28);
    }
    QLabel#componentRowBadge[tone="warning"] {
        background-color: rgba(@warning-rgb, 0.14);
        color: @warning-text;
        border: 1px solid rgba(@warning-rgb, 0.32);
    }
""",
)

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


class ComponentRowWidget(DownloadRow):
    """One row in the Components group of Settings → Downloads.

    The row is "dumb": it renders whatever state is handed to
    :meth:`update_state` / :meth:`set_progress` and re-emits button clicks with
    its component id. Download and install logic stays with the controller.
    """

    install_clicked = pyqtSignal(str)
    cancel_clicked = pyqtSignal(str)
    remove_clicked = pyqtSignal(str)

    #: Once an install ends, Remove takes the place of Install/Cancel, right
    #: under the pointer. It stays disabled this long so the second click of
    #: a double-click, or an impatient one, can't land on it.
    REMOVE_GUARD_MS = 600

    def __init__(self, component_id: str, parent=None):
        """Initialize the row for one component.

        Args:
            component_id: Stable component identifier (see ``ComponentId``).
        """
        super().__init__(
            component_id,
            object_name="componentRow",
            style=_ROW_STYLE,
            tooltip="Click to view component details",
            accessible_name=f"{component_id} component",
            accessible_description=(
                "Open component details. Install and remove actions are separate."
            ),
            parent=parent,
        )
        self.component_id = component_id
        self._installing = False
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

        self._remove_guard = QTimer(self)
        self._remove_guard.setSingleShot(True)
        self._remove_guard.setInterval(self.REMOVE_GUARD_MS)
        self._remove_guard.timeout.connect(self._release_remove)

        outer.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setObjectName("componentProgress")
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.hide()
        outer.addWidget(self.progress)

    def _on_primary_clicked(self) -> None:
        """Route the primary button to install or cancel, whichever applies."""
        if self._installing:
            self.cancel_clicked.emit(self.component_id)
        else:
            self.install_clicked.emit(self.component_id)

    def _release_remove(self) -> None:
        self.remove_button.setEnabled(True)

    def update_state(self, info: ComponentInfo, installing: bool) -> None:
        """Render the row for a component state.

        Args:
            info: Current component description.
            installing: True while an install for this component is in flight.
        """
        just_finished = self._installing and not installing
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
        if just_finished:
            self.remove_button.setEnabled(False)
            self._remove_guard.start()
        elif not self._remove_guard.isActive():
            self.remove_button.setEnabled(True)

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
