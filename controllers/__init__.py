"""
Controllers package for OpenWhisper application.

This package contains domain-specific controllers that handle different
aspects of the application logic.
"""

from controllers.transcription_controller import TranscriptionController
from controllers.completion_handler import CompletionHandler
from controllers.transcription_workflow import TranscriptionWorkflow
from controllers.recording_controller import RecordingController

__all__ = [
    'TranscriptionController',
    'CompletionHandler',
    'TranscriptionWorkflow',
    'RecordingController',
]
