"""Base transcription backend interface."""
from abc import ABC, abstractmethod
from typing import Callable, Optional


class TranscriptionBackend(ABC):
    """Abstract base class for transcription backends."""

    #: Status of the steps inside one long ``transcribe`` call, such as
    #: splitting a large file and uploading its chunks: ``(message,
    #: transcribing)``, where ``transcribing`` is True from the step that
    #: starts the transcription proper. Set by the application controller.
    on_progress: Optional[Callable[[str, bool], None]] = None

    #: Whether ``transcribe`` also takes ``recognition=``, a
    #: services.recognition_context.RecognitionContext with the dictation's
    #: language and vocabulary. Callers pass it only when this is True and
    #: the context is not empty.
    supports_recognition: bool = False

    def __init__(self):
        self.is_transcribing = False
        self.should_cancel = False

    @property
    def recognition_support(self) -> str:
        """"model" when dictionary phrases reach the speech model, else "after".

        Cheap: the Dictionary page reads it to say what the dictionary does
        with this engine.
        """
        return "after"

    @abstractmethod
    def transcribe(self, audio_path: str) -> str:
        """Transcribe an audio file to text."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Return whether the backend is ready to transcribe."""
        pass

    def cancel_transcription(self):
        """Cancel ongoing transcription."""
        self.should_cancel = True

    def reset_cancel_flag(self):
        """Reset the cancellation flag."""
        self.should_cancel = False

    def large_file_size_mb(self, audio_path: str) -> Optional[float]:
        """Return the file's size in MiB if ``transcribe`` will split it.

        None, the default, means the file is transcribed in one pass whatever
        its size; only a backend with an upload limit splits.
        """
        return None

    def _report_progress(self, message: str, transcribing: bool = False) -> None:
        callback = self.on_progress
        if callback is not None:
            callback(message, transcribing)

    def cleanup(self):
        """Release backend resources."""
        pass

    @property
    def name(self) -> str:
        """Get the backend name."""
        return self.__class__.__name__
