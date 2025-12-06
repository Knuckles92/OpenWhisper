"""Deprecated shim. Use services.audio_processing_service instead."""

from services.audio_processing_service import (
    AudioFilePreview,
    AudioProcessingService,
    audio_processing_service,
    audio_processor,
)

__all__ = [
    "AudioFilePreview",
    "AudioProcessingService",
    "audio_processing_service",
    "audio_processor",
]

