"""Explain the second required download for an optional speech model."""
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout

from services.component_catalog import get_component_details
from services.components import catalog_entry_for_platform
from services.format_utils import format_size_bytes
from services.local_asr.catalog import BACKENDS, MODELS, NVIDIA_VULKAN_RUNTIME
from ui_qt.widgets import Button, PrimaryButton

_THIRD_PARTY_NOTICE = (
    "This installs third-party executable software. Archive integrity checks do not guarantee "
    "security. Keep speech runtimes updated. See the runtime's source and license in Downloads."
)


def _sizes(component_id: str) -> tuple[int, int]:
    """(download, on disk) bytes for a runtime on this platform."""
    entry = catalog_entry_for_platform(component_id) or {}
    download = sum(archive.get("size_bytes", 0) for archive in entry.get("archives", []))
    return download, int(entry.get("install_bytes", 0))


def _size(size_bytes: int) -> str:
    """Keep "117 MB" on one line."""
    return format_size_bytes(size_bytes).replace(" ", " ")


class RequiredRuntimeDialog(QDialog):
    def __init__(self, model_name: str, component_id: str, parent=None):
        super().__init__(parent)
        model = MODELS[model_name].label
        runtime = get_component_details(component_id).display_name
        size, _installed = _sizes(component_id)
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
            f"{_THIRD_PARTY_NOTICE}\n\n"
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
        install.setAutoDefault(False)
        install.clicked.connect(self.accept)
        # Enter on a prompt that just appeared must not start a download.
        later.setDefault(True)
        later.setFocus()
        buttons.addWidget(install)
        layout.addLayout(buttons)


class GpuRuntimeDialog(QDialog):
    """First use of Parakeet or Nemotron on Auto with an NVIDIA GPU.

    ``choice`` is the runtime to install: the GPU one ("Use this GPU", the
    recommended choice), the CPU one, or None for Later. Later is the default
    button: Enter on a prompt that just appeared must not start a download.
    """

    def __init__(self, model_name: str, gpu_component: str, cpu_component: str, parent=None):
        super().__init__(parent)
        from services.gpu_info import nvidia_gpu

        self.choice = None
        model = MODELS[model_name]
        gpu = nvidia_gpu()
        card = gpu.short_name if gpu is not None else "this GPU"
        unbroken_card = card.replace(" ", " ")
        api = "Vulkan" if gpu_component == NVIDIA_VULKAN_RUNTIME else "CUDA"
        gpu_download, gpu_disk = _sizes(gpu_component)
        cpu_download, cpu_disk = _sizes(cpu_component)
        self.setWindowTitle("Choose a speech runtime")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        title = QLabel(f"Run {BACKENDS[model.backend]} on the {card}?")
        title.setObjectName("headerLabel")
        layout.addWidget(title)
        self.body = QLabel(
            f"{model.label} needs a speech runtime, the software that runs the model on this "
            "computer. Downloading the model alone does not enable transcription.\n\n"
            f"This computer's {gpu.name if gpu is not None else 'NVIDIA GPU'} can run it:\n\n"
            f"• Use this GPU: {get_component_details(gpu_component).display_name} runs the model "
            f"on the {unbroken_card} with {api}, usually several times faster than the CPU. "
            f"{_size(gpu_download)} download, {_size(gpu_disk)} on disk.\n\n"
            f"• Use the CPU: {get_component_details(cpu_component).display_name} runs it on the "
            f"processor. {_size(cpu_download)} download, {_size(cpu_disk)} on disk.\n\n"
            "You can add the other runtime later in Downloads. The model loads automatically "
            "once it and the runtime are both downloaded.\n\n"
            f"{_THIRD_PARTY_NOTICE}\n\n"
            "If you choose Later, the model stays unavailable until you install a runtime in Downloads."
        )
        self.body.setWordWrap(True)
        layout.addWidget(self.body)
        buttons = QHBoxLayout()
        buttons.addStretch()
        later = Button("Later")
        later.clicked.connect(self.reject)
        buttons.addWidget(later)
        cpu = Button("Use the CPU")
        cpu.setAutoDefault(False)
        cpu.clicked.connect(lambda: self._choose(cpu_component))
        buttons.addWidget(cpu)
        use = PrimaryButton("Use this GPU")
        use.setAutoDefault(False)
        use.clicked.connect(lambda: self._choose(gpu_component))
        later.setDefault(True)
        later.setFocus()
        buttons.addWidget(use)
        layout.addLayout(buttons)

    def _choose(self, component_id: str) -> None:
        self.choice = component_id
        self.accept()
