"""
Base transcription backend interface.
"""
from abc import ABC, abstractmethod
from typing import Optional, List


class TranscriptionBackend(ABC):
    """Abstract base class for transcription backends."""
    
    def __init__(self):
        """Initialize the transcription backend."""
        self.is_transcribing = False
        self.should_cancel = False
    
    @abstractmethod
    def transcribe(self, audio_file_path: str) -> str:
        """Transcribe audio file to text.
        
        Args:
            audio_file_path: Path to the audio file to transcribe.
            
        Returns:
            Transcribed text.
            
        Raises:
            Exception: If transcription fails.
        """
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if the backend is available and ready to use.
        
        Returns:
            True if backend is available, False otherwise.
        """
        pass
    
    def cancel_transcription(self):
        """Cancel ongoing transcription."""
        self.should_cancel = True
    
    def reset_cancel_flag(self):
        """Reset the cancellation flag."""
        self.should_cancel = False

    @property
    def requires_file_splitting(self) -> bool:
        """Whether this backend requires large files to be split.

        Override in subclasses to indicate if the backend has file size limits.
        Default is True (conservative - assumes splitting is needed).

        Returns:
            True if large files should be split, False if backend can handle any size.
        """
        return True

    def transcribe_chunks(self, chunk_files: List[str]) -> str:
        """Transcribe multiple audio chunk files and combine results.
        
        This is an optional method that backends can implement for optimized
        handling of chunked audio. If not implemented, the main UI will
        fall back to calling transcribe() for each chunk individually.
        
        Args:
            chunk_files: List of paths to audio chunk files.
            
        Returns:
            Combined transcribed text from all chunks.
            
        Raises:
            Exception: If transcription fails.
        """
        # Default implementation: transcribe each chunk and combine
        from services.audio_processor import audio_processor
        
        transcriptions = []
        for chunk_file in chunk_files:
            if self.should_cancel:
                raise Exception("Transcription cancelled")
            
            chunk_text = self.transcribe(chunk_file)
            transcriptions.append(chunk_text)
        
        return audio_processor.combine_transcriptions(transcriptions)
    
    def cleanup(self):
        """Clean up backend resources.
        
        Override this method in subclasses to release any resources
        (e.g., models, connections, etc.) when the application shuts down.
        """
        pass
    
    @property
    def name(self) -> str:
        """Get the backend name."""
        return self.__class__.__name__