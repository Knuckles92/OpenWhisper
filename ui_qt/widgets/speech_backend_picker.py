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


def populate_backend_combo(combo: QComboBox) -> None:
    """List engines alphabetically, keeping runtime IDs as item data."""
    for display_name in sorted(config.MODEL_CHOICES, key=str.casefold):
        combo.addItem(backend_picker_label(display_name), config.MODEL_VALUE_MAP[display_name])
        combo.setItemData(
            combo.count() - 1, backend_picker_tooltip(display_name),
            Qt.ItemDataRole.ToolTipRole,
        )
