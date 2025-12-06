"""
Controllers package for OpenWhisper application.

This package contains domain-specific controllers that handle different
aspects of the application logic.
"""

from services.transcription_service import TranscriptionService as TranscriptionController
from services.completion_service import CompletionService as CompletionHandler
from services.workflow_service import WorkflowService as TranscriptionWorkflow
from services.recording_service import RecordingService as RecordingController

__all__ = [
    'TranscriptionController',
    'CompletionHandler',
    'TranscriptionWorkflow',
    'RecordingController',
]
