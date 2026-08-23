"""OpenAI API transcription backend."""
import os
import logging
from typing import Optional, List
from openai import OpenAI
from .base import TranscriptionBackend
from config import config

logger = logging.getLogger(__name__)


class OpenAIBackend(TranscriptionBackend):
    """OpenAI API transcription backend."""

    def __init__(self, model_type: str = "api_whisper", api_key: str = None):
        super().__init__()
        self.model_type = model_type
        self.api_key = api_key or self._get_api_key()
        self.client: Optional[OpenAI] = None
        self._initialize_client()

    def _get_api_key(self) -> Optional[str]:
        api_key = os.getenv('OPENAI_API_KEY')

        if not api_key:
            try:
                from dotenv import load_dotenv
                from config import env_file_path
                load_dotenv(env_file_path())
                api_key = os.getenv('OPENAI_API_KEY')
            except ImportError:
                logger.warning("python-dotenv not installed. Skipping .env file loading.")
            except Exception as e:
                logger.warning(f"Failed to load .env file: {e}")

        return api_key

    def _initialize_client(self):
        if self.api_key:
            try:
                self.client = OpenAI(api_key=self.api_key)
                logger.info("OpenAI client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize OpenAI client: {e}")
                self.client = None
        else:
            logger.warning("No OpenAI API key found")
            self.client = None

    def _get_api_model_name(self) -> str:
        if self.model_type == "api_gpt4o":
            return "gpt-4o-transcribe"
        elif self.model_type == "api_gpt4o_mini":
            return "gpt-4o-mini-transcribe"
        else:  # api_whisper or default
            return "whisper-1"

    def transcribe(self, audio_path: str) -> str:
        """Transcribe an audio file with the configured OpenAI model."""
        if not self.is_available():
            raise Exception("OpenAI API is not available (no API key or client initialization failed)")

        try:
            self.is_transcribing = True
            self.reset_cancel_flag()

            api_model = self._get_api_model_name()
            logger.info(f"Using OpenAI API model: {api_model}")
            logger.info("Sending audio file to OpenAI API...")

            with open(audio_path, "rb") as f:
                response = self.client.audio.transcriptions.create(
                    model=api_model,
                    file=f,
                    response_format="text"
                )

            if self.should_cancel:
                logger.info("Transcription canceled by user")
                raise Exception("Transcription canceled")

            transcript = response.strip()
            logger.info(f"API transcription complete. Length: {len(transcript)} characters")

            return transcript

        except Exception as e:
            logger.error(f"OpenAI API transcription failed: {e}")
            raise
        finally:
            self.is_transcribing = False

    def is_available(self) -> bool:
        """Return whether the API client is initialized."""
        return self.client is not None and self.api_key is not None

    def update_api_key(self, api_key: str):
        """Replace the API key and reinitialize the client."""
        self.api_key = api_key
        self._initialize_client()

    def transcribe_chunks(self, chunk_files: List[str]) -> str:
        """Transcribe chunks sequentially and combine their text."""
        if not self.is_available():
            raise Exception("OpenAI API is not available (no API key or client initialization failed)")

        try:
            self.is_transcribing = True
            self.reset_cancel_flag()

            api_model = self._get_api_model_name()
            transcriptions = []

            logger.info(f"Starting chunked transcription with OpenAI API model: {api_model}")

            for i, chunk_file in enumerate(chunk_files):
                if self.should_cancel:
                    logger.info("Chunked transcription canceled by user")
                    raise Exception("Transcription canceled")

                logger.info(f"Processing chunk {i+1}/{len(chunk_files)} with OpenAI API: {chunk_file}")

                with open(chunk_file, "rb") as f:
                    response = self.client.audio.transcriptions.create(
                        model=api_model,
                        file=f,
                        response_format="text"
                    )

                chunk_text = response.strip()
                transcriptions.append(chunk_text)

                logger.info(f"Chunk {i+1}/{len(chunk_files)} completed. Length: {len(chunk_text)} characters")

            from services.audio_processor import audio_processor
            combined_text = audio_processor.combine_transcriptions(transcriptions)

            logger.info(f"OpenAI chunked transcription complete. Total length: {len(combined_text)} characters")
            return combined_text

        except Exception as e:
            logger.error(f"OpenAI chunked transcription failed: {e}")
            raise
        finally:
            self.is_transcribing = False

    def change_model_type(self, model_type: str):
        """Change the model used for subsequent requests."""
        self.model_type = model_type
        logger.info(f"Model type changed to: {model_type}")

    def cleanup(self):
        """Clean up OpenAI client resources."""
        try:
            if self.client is not None:
                logger.info(f"Cleaning up OpenAI backend ({self.model_type})...")

                self.should_cancel = True

                self.client.close()
                self.client = None

                logger.info(f"OpenAI backend ({self.model_type}) cleaned up successfully")
        except Exception as e:
            logger.debug(f"Error during OpenAI backend cleanup: {e}")

    @property
    def name(self) -> str:
        return f"OpenAI ({self.model_type})"

    @property
    def requires_file_splitting(self) -> bool:
        """Return True because the API enforces a 25 MB upload limit."""
        return True