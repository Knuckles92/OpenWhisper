"""Presentation-independent recording and processing states."""
from enum import Enum


class OverlayState(Enum):
    NONE = "none"
    RECORDING = "recording"
    PROCESSING = "processing"
    TRANSCRIBING = "transcribing"
    CLEANING = "cleaning"
    CANCELING = "canceling"
    STT_ENABLED = "stt_enabled"
    STT_DISABLED = "stt_disabled"
    COMMAND_LISTENING = "command_listening"
    REWRITING = "rewriting"
