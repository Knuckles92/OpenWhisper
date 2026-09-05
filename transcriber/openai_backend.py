"""OpenAI API transcription backend."""
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List
from openai import OpenAI
from .base import TranscriptionBackend
from config import config
from services.credentials import resolve_credential
from services.settings import (
    LEGACY_API_MODELS,
    resolve_api_transcription_model,
    settings_manager,
)

logger = logging.getLogger(__name__)

#: Chunk uploads run this many at a time. Deliberately small: the uploads have
#: no retry path, so extra parallelism mostly raises the odds of a rate-limit
#: rejection that would fail the whole transcription.
CHUNK_UPLOAD_CONCURRENCY = 3


class OpenAIBackend(TranscriptionBackend):
    """OpenAI API transcription backend."""

    def __init__(self, model_type: str = "api", api_key: str = None):
        super().__init__()
        self.model_type = model_type
        self.api_key = api_key or self._get_api_key()
        self.client: Optional[OpenAI] = None
        self._initialize_client()

    def _get_api_key(self) -> Optional[str]:
        return resolve_credential("OPENAI_API_KEY")

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
        if self.model_type == "api":
            return resolve_api_transcription_model(settings_manager.load_all_settings())
        if self.model_type in LEGACY_API_MODELS:
            return LEGACY_API_MODELS[self.model_type]
        if self.model_type in config.API_MODEL_CHOICES:
            return self.model_type
        raise ValueError(f"Unknown API transcription model: {self.model_type}")

    def _transcribe_file(self, audio_path: str, api_model: str) -> str:
        with open(audio_path, "rb") as audio_file:
            response = self.client.audio.transcriptions.create(
                model=api_model,
                file=audio_file,
                response_format="json" if api_model == "gpt-transcribe" else "text",
            )
        return (response if isinstance(response, str) else response.text).strip()

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

            transcript = self._transcribe_file(audio_path, api_model)

            if self.should_cancel:
                logger.info("Transcription canceled by user")
                raise Exception("Transcription canceled")

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

    def update_api_key(self, api_key: Optional[str]):
        """Replace the API key and reinitialize the client."""
        self.api_key = api_key
        self._initialize_client()

    def _transcribe_one_chunk(self, chunk_file: str, api_model: str) -> str:
        """Upload one chunk. Raises if the job was canceled before it started."""
        if self.should_cancel:
            raise Exception("Transcription canceled")

        return self._transcribe_file(chunk_file, api_model)

    def transcribe_chunks(self, chunk_files: List[str]) -> str:
        """Transcribe chunks concurrently and combine their text in order.

        Each chunk is an independent upload with no shared state, and the wall
        clock here is almost entirely network, so a serial loop leaves a large
        file waiting out one round trip after another.

        The pool is local to this call rather than the controller's: that one
        has two workers and ``transcribe_large_audio_file`` already occupies
        one, so fanning out onto it would win nothing and would compete with
        model loading and Hugging Face downloads.

        ``CHUNK_UPLOAD_CONCURRENCY`` stays low on purpose — there is no retry
        path here, so more parallelism mostly buys a higher chance of a 429.
        """
        if not self.is_available():
            raise Exception("OpenAI API is not available (no API key or client initialization failed)")

        try:
            self.is_transcribing = True
            self.reset_cancel_flag()

            api_model = self._get_api_model_name()
            total = len(chunk_files)

            logger.info(
                f"Starting chunked transcription with OpenAI API model: {api_model} "
                f"({total} chunks, up to {CHUNK_UPLOAD_CONCURRENCY} at a time)"
            )

            if total <= 1:
                transcriptions = [
                    self._transcribe_one_chunk(chunk, api_model)
                    for chunk in chunk_files
                ]
            else:
                workers = min(CHUNK_UPLOAD_CONCURRENCY, total)
                with ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix="chunk-upload"
                ) as pool:
                    # executor.map keeps results in submission order, which the
                    # combined transcript depends on, and re-raises the first
                    # failure once the in-flight uploads have finished.
                    transcriptions = list(
                        pool.map(
                            lambda chunk: self._transcribe_one_chunk(
                                chunk, api_model
                            ),
                            chunk_files,
                        )
                    )

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
        return f"OpenAI ({self._get_api_model_name()})"

    @property
    def requires_file_splitting(self) -> bool:
        """Return True because the API enforces a 25 MB upload limit."""
        return True