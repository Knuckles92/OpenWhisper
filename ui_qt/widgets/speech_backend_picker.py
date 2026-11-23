"""GUI labels and ordering for speech engines, with stable backend IDs."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox

from config import config, parakeet_mlx_supported


MLX_RECOMMENDATION = (
    "Parakeet MLX is recommended on this Mac. It uses the Apple GPU for "
    "faster transcription."
)


def backend_display_name(backend: str) -> str:
    """Canonical engine name used by controller signals and saved selections."""
    names = {value: display for display, value in config.MODEL_VALUE_MAP.items()}
    return names.get(backend) or names[config.DEFAULT_BACKEND]


def backend_picker_label(display_name: str) -> str:
    """Annotate Mac choices without changing their stored identities."""
    if parakeet_mlx_supported():
        if display_name == "Parakeet MLX":
            return "Parakeet MLX (recommended)"
        if display_name == "Parakeet":
            return "Parakeet (CPU)"
    return display_name


def backend_picker_tooltip(display_name: str) -> str:
    if parakeet_mlx_supported():
        if display_name == "Parakeet MLX":
            return MLX_RECOMMENDATION
        if display_name in ("Parakeet", "Local Whisper", "Whisper"):
            return "Runs on the CPU on this Mac. " + MLX_RECOMMENDATION
    return display_name


def speech_model_picker_label(model_name: str) -> str:
    """Label a catalog model using the same Mac guidance as the engine picker."""
    from services.local_asr.catalog import MODELS

    model = MODELS[model_name]
    label = model.label
    if parakeet_mlx_supported():
        if model.backend == "parakeet_mlx":
            label += " — recommended"
        elif model.backend == "parakeet":
            label += " (CPU)"
    return label


def populate_backend_combo(combo: QComboBox, *, include_unsupported: bool = False) -> None:
    """List engines supported on this computer, keeping runtime IDs as item data."""
    from services.local_asr.catalog import backend_supported_here

    for display_name in sorted(config.MODEL_CHOICES, key=str.casefold):
        backend = config.MODEL_VALUE_MAP[display_name]
        if not include_unsupported and not backend_supported_here(backend):
            continue
        combo.addItem(backend_picker_label(display_name), backend)
        combo.setItemData(
            combo.count() - 1, backend_picker_tooltip(display_name),
            Qt.ItemDataRole.ToolTipRole,
        )


def ensure_backend_item(combo: QComboBox, backend: str) -> int:
    """Ensure ``backend`` is present in ``combo`` (e.g. a saved cross-OS selection)."""
    index = combo.findData(backend)
    if index >= 0:
        return index
    display_name = backend_display_name(backend)
    if config.MODEL_VALUE_MAP.get(display_name) != backend:
        return -1
    combo.addItem(backend_picker_label(display_name), backend)
    index = combo.count() - 1
    combo.setItemData(index, backend_picker_tooltip(display_name), Qt.ItemDataRole.ToolTipRole)
    return index
