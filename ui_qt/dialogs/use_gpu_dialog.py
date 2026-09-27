"""Offer to move Local Whisper onto an NVIDIA GPU whose CUDA libraries are missing."""
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout

from services.format_utils import format_size_bytes
from services.gpu_info import format_gb
from services.gpu_setup import GpuSetupPlan
from ui_qt.widgets import Button, PrimaryButton


def plan_steps(plan: GpuSetupPlan) -> list:
    """The offer's bullet points, in the order they happen."""
    steps = []
    if plan.install_component:
        steps.append(
            f"Install GPU Acceleration (NVIDIA's CUDA libraries): "
            f"{format_size_bytes(plan.component_download_bytes)} download, "
            f"{format_size_bytes(plan.component_install_bytes)} on disk."
        )
    if plan.switches_model:
        steps.append(f"Switch Local Whisper from {plan.previous_model} to {plan.model}.")
    if plan.model_download_bytes:
        steps.append(
            f"Download Whisper {plan.model}: {format_size_bytes(plan.model_download_bytes)}."
        )
    run = f"Run {plan.model} on the GPU at {plan.compute_type}"
    if plan.estimate_mib and plan.gpu is not None:
        run += (f", about {format_gb(plan.estimate_mib)} of its "
                f"{format_gb(plan.gpu.total_mib)}")
    elif plan.estimate_mib:
        run += f", about {format_gb(plan.estimate_mib)} of GPU memory"
    run += "."
    if not plan.supports_float16:
        run += " This GPU has no float16, so it runs int8, which also halves the memory."
    steps.append(run)
    return steps


class UseGpuDialog(QDialog):
    """Accept: "Use this GPU". Reject: keep using the CPU and don't ask again."""

    def __init__(self, plan: GpuSetupPlan, parent=None):
        super().__init__(parent)
        card = plan.gpu.name if plan.gpu is not None else "an NVIDIA GPU"
        self.setWindowTitle("Use this GPU")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        title = QLabel("Transcribe on this computer's GPU?")
        title.setObjectName("headerLabel")
        layout.addWidget(title)
        self.body = QLabel(
            f"This computer has {card}, but Local Whisper is running on the "
            "CPU because the CUDA libraries it needs aren't installed. "
            "Using the GPU will:\n\n"
            + "\n".join(f"• {step}" for step in plan_steps(plan))
            + "\n\nIt all happens in the background, and dictation keeps working "
            "on the CPU until the GPU is ready."
        )
        self.body.setWordWrap(True)
        layout.addWidget(self.body)
        buttons = QHBoxLayout()
        buttons.addStretch()
        later = Button("Keep using the CPU")
        later.setToolTip("GPU Acceleration stays available in Settings → Downloads.")
        later.clicked.connect(self.reject)
        buttons.addWidget(later)
        use = PrimaryButton("Use this GPU")
        use.setDefault(True)
        use.clicked.connect(self.accept)
        buttons.addWidget(use)
        layout.addLayout(buttons)
