"""API transcription backend: OpenAI or another OpenAI-compatible server."""
import logging
import threading
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from typing import Optional, List, Tuple
from .base import TranscriptionBackend
from config import config
from services import openai_retirement, speech_endpoints
from services.settings import serving_api_model, settings_manager
from services.speech_endpoints import SpeechEndpoint

logger = logging.getLogger(__name__)

#: Chunk uploads run this many at a time. Deliberately small: the uploads have
#: no retry path, so extra parallelism mostly raises the odds of a rate-limit
#: rejection that would fail the whole transcription.
CHUNK_UPLOAD_CONCURRENCY = 3

#: Statuses that blame the request itself. A request carrying hints is sent
#: once more without them; an auth, quota or rate-limit error would only
#: fail again.
_HINT_REJECTIONS = frozenset({400, 422})


def transcription_hints(endpoint: SpeechEndpoint, api_model: str, recognition) -> dict:
    """The hint fields ``api_model`` on ``endpoint`` takes for a RecognitionContext.

    OpenAI's gpt-transcribe takes ``keywords`` and ``languages``; every other
    model, OpenAI's older ones and other servers' Whisper models alike, takes
    a ``prompt``, read as preceding text, and one ``language``. A server that
    assumes a language without the field is asked to detect it.
    """
    from services.recognition_context import keywords, language_code, vocabulary_prompt

    phrases = recognition.phrases if recognition else ()
    language = language_code(recognition.language) if recognition else ""
    hints = {}
    if endpoint.is_openai and api_model == "gpt-transcribe":
        words = keywords(phrases)
        if words:
            hints["keywords"] = words
        if language:
            hints["languages"] = [language]
    else:
        prompt = vocabulary_prompt(phrases)
        if prompt:
            hints["prompt"] = prompt
        language = language or endpoint.auto_language
        if language:
            hints["language"] = language
    return hints


def response_format(endpoint: SpeechEndpoint, api_model: str) -> str:
    """OpenAI's older models answer in plain text, as they always have here.

    Everything else gets JSON, the API's default and so the one format every
    compatible server takes (LocalAI, for one, rejects "text").
    """
    return "text" if endpoint.is_openai and api_model != "gpt-transcribe" else "json"


class OpenAIBackend(TranscriptionBackend):
    """Uploads audio to the selected OpenAI-compatible transcription server.

    ``model_type`` "api" follows the saved server and model; an OpenAI model
    id pins this backend to that OpenAI model.

    The client is built for one connection (address, key, timeout) on the
    first request after it changes, so a saved key or a newly chosen server
    reaches the next request without anyone pushing it here. Building waits
    for a request (or ``prepare_client`` on a worker) because the controller
    creates this backend at startup in every session.
    """

    supports_recognition = True

    def __init__(self, model_type: str = "api"):
        super().__init__()
        self.model_type = model_type
        self.client = None
        #: The connection ``client`` was built for, and one that failed to build.
        self._client_connection: Optional[tuple] = None
        self._failed_connection: Optional[tuple] = None
        self._client_lock = threading.Lock()

    def _target(self) -> Tuple[SpeechEndpoint, str]:
        """The endpoint and model the next request goes to."""
        if self.model_type == "api":
            settings = settings_manager.load_all_settings()
            endpoint = speech_endpoints.selected_endpoint(settings)
            return endpoint, speech_endpoints.endpoint_model(endpoint, settings)
        if self.model_type in config.API_MODEL_CHOICES:
            return (
                speech_endpoints.configured_endpoint(speech_endpoints.OPENAI_ENDPOINT_ID),
                serving_api_model(self.model_type),
            )
        raise ValueError(f"Unknown API transcription model: {self.model_type}")

    @staticmethod
    def _connection(endpoint: SpeechEndpoint) -> tuple:
        return (endpoint.base_url, speech_endpoints.api_key(endpoint), endpoint.timeout_s)

    def _build_client(self, connection: tuple):
        from services.isolated import IsolatedOpenAIClient

        base_url, api_key, timeout_s = connection
        return IsolatedOpenAIClient(api_key=api_key, base_url=base_url, read_timeout=timeout_s)

    def prepare_client(self, endpoint: Optional[SpeechEndpoint] = None) -> None:
        """Build the client for ``endpoint`` (default: the selected one) if it isn't.

        Safe from any thread.
        """
        endpoint = endpoint or self._target()[0]
        connection = self._connection(endpoint)
        with self._client_lock:
            if self.client is not None and self._client_connection == connection:
                return
            if self.client is not None:
                self.client.close()
            self.client = None
            self._client_connection = None
            if connection[1] is None:
                logger.warning("No API key for %s transcription", endpoint.name)
                return
            try:
                self.client = self._build_client(connection)
                self._client_connection = connection
                self._failed_connection = None
                logger.info("%s transcription client initialized", endpoint.name)
            except Exception as e:
                logger.error(f"Failed to initialize the {endpoint.name} client: {e}")
                self._failed_connection = connection

    def _get_api_model_name(self) -> str:
        return self._target()[1]

    def _transcribe_file(
        self, audio_path: str, endpoint: SpeechEndpoint, api_model: str, recognition=None,
    ) -> str:
        try:
            return self._request_hinted(audio_path, endpoint, api_model, recognition)
        except Exception as exc:
            if (
                not endpoint.is_openai
                or api_model not in openai_retirement.RETIRING_TRANSCRIPTION_MODELS
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
            return self._request_hinted(
                audio_path, endpoint, config.DEFAULT_API_MODEL, recognition,
            )

    def _request_hinted(
        self, audio_path: str, endpoint: SpeechEndpoint, api_model: str, recognition,
    ) -> str:
        """One request with ``recognition``'s hints, then once more without them.

        Hints must never cost a dictation, and neither OpenAI nor the
        compatible servers document limits for them.
        """
        hints = transcription_hints(endpoint, api_model, recognition)
        if not hints:
            return self._request_transcript(audio_path, endpoint, api_model)
        try:
            return self._request_transcript(audio_path, endpoint, api_model, **hints)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status not in _HINT_REJECTIONS or openai_retirement.is_model_gone_error(exc):
                raise
            logger.warning(
                "%s rejected a request with hints (HTTP %s: %d keywords, "
                "%d prompt characters, language %s); retrying without them",
                endpoint.name, status, len(hints.get("keywords", ())),
                len(hints.get("prompt", "")),
                "set" if "language" in hints or "languages" in hints else "unset",
            )
            return self._request_transcript(audio_path, endpoint, api_model)

    def _request_transcript(
        self, audio_path: str, endpoint: SpeechEndpoint, api_model: str, **hints,
    ) -> str:
        if self.should_cancel:
            raise RuntimeError("Transcription canceled")
        with open(audio_path, "rb") as audio_file:
            response = self.client.audio.transcriptions.create(
                model=api_model,
                file=audio_file,
                response_format=response_format(endpoint, api_model),
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
            endpoint, api_model = self._target()
            self.prepare_client(endpoint)
            if self.should_cancel:
                raise RuntimeError("Transcription canceled")
            if self.client is None:
                raise Exception(
                    f"{endpoint.name} transcription is not available "
                    "(no API key or client initialization failed)"
                )

            logger.info(f"Using {endpoint.name} model: {api_model}")
            # Without a context the helpers are called exactly as before.
            extra = {"recognition": recognition} if recognition else {}
            if self.large_file_size_mb(audio_path) is not None:
                transcript = self._transcribe_split(audio_path, endpoint, api_model, **extra)
            else:
                logger.info(f"Sending audio file to {endpoint.name}...")
                transcript = self._transcribe_file(audio_path, endpoint, api_model, **extra)

            if self.should_cancel:
                logger.info("Transcription canceled by user")
                raise Exception("Transcription canceled")

            logger.info(f"API transcription complete. Length: {len(transcript)} characters")

            return transcript

        except Exception as e:
            logger.error(f"API transcription failed: {e}")
            raise
        finally:
            self.is_transcribing = False

    @property
    def recognition_support(self) -> str:
        return "model"

    def is_available(self) -> bool:
        """Return whether the selected server has its key and a client that built.

        Recording start asks this on the UI thread, so it never builds the
        client itself; the request does, on its worker.
        """
        connection = self._connection(self._target()[0])
        return connection[1] is not None and connection != self._failed_connection

    def _transcribe_one_chunk(
        self, chunk_file: str, endpoint: SpeechEndpoint, api_model: str, **extra,
    ) -> str:
        """Upload one chunk. Raises if the job was canceled before it started."""
        if self.should_cancel:
            raise Exception("Transcription canceled")

        return self._transcribe_file(chunk_file, endpoint, api_model, **extra)

    def _transcribe_split(
        self, audio_path: str, endpoint: SpeechEndpoint, api_model: str, **extra,
    ) -> str:
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
            return self._transcribe_chunks(chunk_files, endpoint, api_model, **extra)
        finally:
            try:
                audio_processor.cleanup_temp_files()
            except Exception as cleanup_error:
                logger.warning(f"Failed to cleanup temp files: {cleanup_error}")

    def _transcribe_chunks(
        self, chunk_files: List[str], endpoint: SpeechEndpoint, api_model: str, **extra,
    ) -> str:
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
            f"Starting chunked transcription with {endpoint.name} model: {api_model} "
            f"({total} chunks, up to {CHUNK_UPLOAD_CONCURRENCY} at a time)"
        )

        if total <= 1:
            transcriptions = [
                self._transcribe_one_chunk(chunk, endpoint, api_model, **extra)
                for chunk in chunk_files
            ]
        else:
            workers = min(CHUNK_UPLOAD_CONCURRENCY, total)
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="chunk-upload"
            ) as pool:
                futures = [
                    pool.submit(self._transcribe_one_chunk, chunk, endpoint, api_model, **extra)
                    for chunk in chunk_files
                ]
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
        """Close the client; the next request builds a fresh one."""
        try:
            with self._client_lock:
                client, self.client = self.client, None
                self._client_connection = None
            if client is not None:
                logger.info(f"Cleaning up API backend ({self.model_type})...")

                self.should_cancel = True

                client.close()

                logger.info(f"API backend ({self.model_type}) cleaned up successfully")
        except Exception as e:
            logger.debug(f"Error during API backend cleanup: {e}")

    def cancel_transcription(self):
        super().cancel_transcription()
        # Killing upload workers interrupts DNS, upload, and response reads.
        # A new job gets a fresh client and cancellation state.
        self.cleanup()

    @property
    def name(self) -> str:
        endpoint, api_model = self._target()
        return f"{endpoint.name} ({api_model})"
