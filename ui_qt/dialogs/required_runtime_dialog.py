"""Explain the second required download for an optional speech model."""
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout

from services.component_catalog import get_component_details
from services.components import catalog_entry_for_platform
from services.format_utils import format_size_bytes
from services.local_asr.catalog import MODELS
from ui_qt.widgets import Button, PrimaryButton


class RequiredRuntimeDialog(QDialog):
    def __init__(self, model_name: str, component_id: str, parent=None):
        super().__init__(parent)
        model = MODELS[model_name].label
        runtime = get_component_details(component_id).display_name
        entry = catalog_entry_for_platform(component_id) or {}
        size = sum(archive.get("size_bytes", 0) for archive in entry.get("archives", []))
        self.setWindowTitle("Required speech runtime")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        title = QLabel(f"{runtime} is required")
        title.setObjectName("headerLabel")
        layout.addWidget(title)
        self.body = QLabel(
            f"{model} needs both its model files and {runtime} to work. "
            "The runtime is the software that runs the model on this computer; "
            "downloading the model alone does not enable transcription.\n\n"
            f"Install {runtime} now? This is a separate {format_size_bytes(size)} download, "
            "shared by compatible models. After both downloads finish, the selected model loads automatically.\n\n"
            "If you choose Later, the model will remain unavailable until you install this runtime in Downloads."
        )
        self.body.setWordWrap(True)
        layout.addWidget(self.body)
        buttons = QHBoxLayout()
        buttons.addStretch()
        later = Button("Later")
        later.clicked.connect(self.reject)
        buttons.addWidget(later)
        install = PrimaryButton("Install required runtime")
        install.setDefault(True)
        install.clicked.connect(self.accept)
        buttons.addWidget(install)
        layout.addLayout(buttons)
