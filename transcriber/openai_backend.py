"""OpenAI API transcription backend."""
import logging
import threading
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING, Optional, List
from .base import TranscriptionBackend
from config import config
from services import openai_retirement
from services.credentials import resolve_credential
from services.settings import (
    resolve_api_transcription_model,
    serving_api_model,
    settings_manager,
)

if TYPE_CHECKING:
    from openai import OpenAI

logger = logging.getLogger(__name__)

#: Chunk uploads run this many at a time. Deliberately small: the uploads have
#: no retry path, so extra parallelism mostly raises the odds of a rate-limit
#: rejection that would fail the whole transcription.
CHUNK_UPLOAD_CONCURRENCY = 3

#: Statuses that blame the request itself. A request carrying hints is sent
#: once more without them; an auth, quota or rate-limit error would only
#: fail again.
_HINT_REJECTIONS = frozenset({400, 422})


def transcription_hints(api_model: str, recognition) -> dict:
    """The hint fields ``api_model`` takes for a RecognitionContext.

    gpt-transcribe takes ``keywords`` and ``languages``; the older models
    take a ``prompt``, read as preceding text, and one ``language``.
    """
    from services.recognition_context import keywords, language_code, vocabulary_prompt

    if not recognition:
        return {}
    hints = {}
    language = language_code(recognition.language)
    if api_model == "gpt-transcribe":
        words = keywords(recognition.phrases)
        if words:
            hints["keywords"] = words
        if language:
            hints["languages"] = [language]
    else:
        prompt = vocabulary_prompt(recognition.phrases)
        if prompt:
            hints["prompt"] = prompt
        if language:
            hints["language"] = language
    return hints


class OpenAIBackend(TranscriptionBackend):
    """OpenAI API transcription backend."""

    #: Whether ``client`` still has to be built for ``api_key``. The
    #: controller creates this backend at startup in every session, and the
    #: openai SDK takes most of a second to import, so the client waits for
    #: the first request or for ``prepare_client`` on a worker.
    _client_pending = False
    supports_recognition = True

    def __init__(self, model_type: str = "api", api_key: str = None):
        super().__init__()
        self.model_type = model_type
        self.api_key = api_key or self._get_api_key()
        self.client: Optional[OpenAI] = None
        self._client_lock = threading.Lock()
        self._client_pending = True

    def _get_api_key(self) -> Optional[str]:
        return resolve_credential("OPENAI_API_KEY")

    def prepare_client(self) -> None:
        """Build the client if it is still pending. Safe from any thread."""
        if not self._client_pending:
            return
        with self._client_lock:
            if self._client_pending:
                self._initialize_client()
                self._client_pending = False

    def _initialize_client(self):
        if self.api_key:
            try:
                from services.isolated import IsolatedOpenAIClient

                self.client = IsolatedOpenAIClient(api_key=self.api_key)
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
        if self.model_type in config.API_MODEL_CHOICES:
            return serving_api_model(self.model_type)
        raise ValueError(f"Unknown API transcription model: {self.model_type}")

    def _transcribe_file(self, audio_path: str, api_model: str, recognition=None) -> str:
        try:
            return self._request_hinted(audio_path, api_model, recognition)
        except Exception as exc:
            if (
                api_model not in openai_retirement.RETIRING_TRANSCRIPTION_MODELS
                or not openai_retirement.is_model_gone_error(exc)
            ):
                raise
            # OpenAI switched the model off before this computer's clock
            # reached the shutdown date. The fallback model takes other
            # hint fields, so they are chosen again for it.
            logger.warning(
                "OpenAI no longer serves %s; retrying with %s",
                api_model, config.DEFAULT_API_MODEL,
            )
            return self._request_hinted(audio_path, config.DEFAULT_API_MODEL, recognition)

    def _request_hinted(self, audio_path: str, api_model: str, recognition) -> str:
        """One request with ``recognition``'s hints, then once more without them.

        Hints must never cost a dictation, and OpenAI documents no limits
        for them.
        """
        hints = transcription_hints(api_model, recognition)
        if not hints:
            return self._request_transcript(audio_path, api_model)
        try:
            return self._request_transcript(audio_path, api_model, **hints)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status not in _HINT_REJECTIONS or openai_retirement.is_model_gone_error(exc):
                raise
            logger.warning(
                "OpenAI rejected a request with hints (HTTP %s: %d keywords, "
                "%d prompt characters, language %s); retrying without them",
                status, len(hints.get("keywords", ())), len(hints.get("prompt", "")),
                "set" if "language" in hints or "languages" in hints else "unset",
            )
            return self._request_transcript(audio_path, api_model)

    def _request_transcript(self, audio_path: str, api_model: str, **hints) -> str:
        if self.should_cancel:
            raise RuntimeError("Transcription canceled")
        with open(audio_path, "rb") as audio_file:
            response = self.client.audio.transcriptions.create(
                model=api_model,
                file=audio_file,
                response_format="json" if api_model == "gpt-transcribe" else "text",
                **hints,
            )
        return (response if isinstance(response, str) else response.text).strip()

    def large_file_size_mb(self, audio_path: str) -> Optional[float]:
        """Return the file's size in MiB if it is over the API's upload limit."""
        from services.audio_processor import audio_processor

        needs_splitting, file_size_mb = audio_processor.check_file_size(audio_path)
        return file_size_mb if needs_splitting else None

    def transcribe(self, audio_path: str, recognition=None) -> str:
        """Transcribe a file, uploading one over the size limit in chunks.

        ``recognition``'s language and vocabulary go with every request, in
        the fields the model takes (``transcription_hints``).
        """
        try:
            self.is_transcribing = True
            self.reset_cancel_flag()
            self.prepare_client()
            if self.should_cancel:
                raise RuntimeError("Transcription canceled")
            if not self.is_available():
                raise Exception("OpenAI API is not available (no API key or client initialization failed)")

            api_model = self._get_api_model_name()
            logger.info(f"Using OpenAI API model: {api_model}")
            # Without a context the helpers are called exactly as before.
            extra = {"recognition": recognition} if recognition else {}
            if self.large_file_size_mb(audio_path) is not None:
                transcript = self._transcribe_split(audio_path, api_model, **extra)
            else:
                logger.info("Sending audio file to OpenAI API...")
                transcript = self._transcribe_file(audio_path, api_model, **extra)

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

    @property
    def recognition_support(self) -> str:
        return "model"

    def is_available(self) -> bool:
        """Return whether there is a key and a client, built or still pending.

        Recording start asks this on the UI thread, so it never builds the
        client itself; the request does, on its worker.
        """
        return self.api_key is not None and (
            self.client is not None or self._client_pending
        )

    def update_api_key(self, api_key: Optional[str]):
        """Replace the API key; the next request rebuilds the client."""
        with self._client_lock:
            if self.client is not None:
                self.client.close()
                self.client = None
            self.api_key = api_key
            self._client_pending = True

    def _transcribe_one_chunk(self, chunk_file: str, api_model: str, **extra) -> str:
        """Upload one chunk. Raises if the job was canceled before it started."""
        if self.should_cancel:
            raise Exception("Transcription canceled")

        return self._transcribe_file(chunk_file, api_model, **extra)

    def _transcribe_split(self, audio_path: str, api_model: str, **extra) -> str:
        """Split a file over the upload limit and upload the chunks.

        The chunks are temp files shared through ``audio_processor``, so they
        are removed here however the job ends.
        """
        from services.audio_processor import audio_processor

        try:
            chunk_files = audio_processor.split_audio_file(
                audio_path, self._report_progress, should_cancel=lambda: self.should_cancel
            )
            if not chunk_files:
                raise Exception("Failed to split audio file")
            if self.should_cancel:
                raise Exception("Transcription canceled")
            self._report_progress(
                f"Transcribing {len(chunk_files)} chunks...", transcribing=True
            )
            return self._transcribe_chunks(chunk_files, api_model, **extra)
        finally:
            try:
                audio_processor.cleanup_temp_files()
            except Exception as cleanup_error:
                logger.warning(f"Failed to cleanup temp files: {cleanup_error}")

    def _transcribe_chunks(self, chunk_files: List[str], api_model: str, **extra) -> str:
        """Upload chunks concurrently and combine their text in order.

        Each chunk is an independent upload with no shared state, and the wall
        clock here is almost entirely network, so a serial loop leaves a large
        file waiting out one round trip after another.

        The pool is local to this call rather than the controller's: that one
        has two workers and this job already occupies one, so fanning out onto
        it would win nothing and would compete with model loading and Hugging
        Face downloads.

        ``CHUNK_UPLOAD_CONCURRENCY`` stays low on purpose — there is no retry
        path here, so more parallelism mostly buys a higher chance of a 429.
        """
        total = len(chunk_files)
        logger.info(
            f"Starting chunked transcription with OpenAI API model: {api_model} "
            f"({total} chunks, up to {CHUNK_UPLOAD_CONCURRENCY} at a time)"
        )

        if total <= 1:
            transcriptions = [
                self._transcribe_one_chunk(chunk, api_model, **extra)
                for chunk in chunk_files
            ]
        else:
            workers = min(CHUNK_UPLOAD_CONCURRENCY, total)
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="chunk-upload"
            ) as pool:
                futures = [pool.submit(self._transcribe_one_chunk, chunk, api_model, **extra)
                           for chunk in chunk_files]
                done, _ = wait(futures, return_when=FIRST_EXCEPTION)
                failed = next((future for future in futures
                               if future in done and future.exception() is not None), None)
                if failed is not None:
                    # A rejected chunk must not wait out an unrelated stalled
                    # upload. Close the process-backed client before joining
                    # this pool, and preserve the original request failure.
                    for future in futures:
                        future.cancel()
                    self.cancel_transcription()
                    failed.result()
                transcriptions = [future.result() for future in futures]

        from services.audio_processor import audio_processor
        return audio_processor.combine_transcriptions(transcriptions)

    def cleanup(self):
        """Clean up OpenAI client resources."""
        try:
            if self.client is not None:
                logger.info(f"Cleaning up OpenAI backend ({self.model_type})...")

                self.should_cancel = True

                self.client.close()
                self.client = None

                self._client_pending = True

                logger.info(f"OpenAI backend ({self.model_type}) cleaned up successfully")
        except Exception as e:
            logger.debug(f"Error during OpenAI backend cleanup: {e}")

    def cancel_transcription(self):
        super().cancel_transcription()
        # Killing upload workers interrupts DNS, upload, and response reads.
        # A new job gets a fresh client and cancellation state.
        self.cleanup()

    @property
    def name(self) -> str:
        return f"OpenAI ({self._get_api_model_name()})"
