"""Explicit overlay state values used to route UI transitions."""
from enum import Enum


class OverlayState(Enum):
    """Logical overlay states independent of human-readable status text."""

    NONE = "none"
    RECORDING = "recording"
    PROCESSING = "processing"
    TRANSCRIBING = "transcribing"
    CANCELING = "canceling"
    STT_ENABLED = "stt_enabled"
    STT_DISABLED = "stt_disabled"
