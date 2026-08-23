"""Base transcription backend interface."""
from abc import ABC, abstractmethod
from typing import Optional, List


class TranscriptionBackend(ABC):
    """Abstract base class for transcription backends."""

    def __init__(self):
        self.is_transcribing = False
        self.should_cancel = False

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

    @property
    def requires_file_splitting(self) -> bool:
        """Return whether large inputs must be split; defaults conservatively."""
        return True

    def transcribe_chunks(self, chunk_files: List[str]) -> str:
        """Transcribe chunks sequentially and combine their text."""
        from services.audio_processor import audio_processor

        transcriptions = []
        for chunk_file in chunk_files:
            if self.should_cancel:
                raise Exception("Transcription canceled")

            chunk_text = self.transcribe(chunk_file)
            transcriptions.append(chunk_text)

        return audio_processor.combine_transcriptions(transcriptions)

    def cleanup(self):
        """Release backend resources."""
        pass

    @property
    def name(self) -> str:
        """Get the backend name."""
        return self.__class__.__name__