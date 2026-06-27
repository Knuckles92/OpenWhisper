"""
Transcription backends for the OpenWhisper application.
"""
from .base import TranscriptionBackend
from .local_backend import LocalWhisperBackend
from .openai_backend import OpenAIBackend

__all__ = ['TranscriptionBackend', 'LocalWhisperBackend', 'OpenAIBackend']