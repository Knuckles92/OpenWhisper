"""
Post-ASR transcript cleanup via OpenAI or OpenRouter chat models.
"""
import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

from openai import OpenAI

from config import config
try:
    from services.settings import (
        TranscriptCleanupModelSort,
        TranscriptCleanupProvider,
        TranscriptCleanupReasoning,
        default_transcript_cleanup_model,
    )
except ImportError:  # pragma: no cover - supports lightweight test stubs
    class TranscriptCleanupProvider:
        OPENAI = "openai"
        OPENROUTER = "openrouter"
        ALL = (OPENAI, OPENROUTER)

    class TranscriptCleanupModelSort:
        ALPHABETICAL = "alphabetical"
        ALL = (ALPHABETICAL,)

    class TranscriptCleanupReasoning:
        OFF = "off"
        LOW = "low"
        MEDIUM = "medium"
        HIGH = "high"
        ALL = (OFF, LOW, MEDIUM, HIGH)

    def default_transcript_cleanup_model(provider):
        if provider == TranscriptCleanupProvider.OPENROUTER:
            return config.TRANSCRIPT_CLEANUP_OPENROUTER_MODEL
        return config.TRANSCRIPT_CLEANUP_MODEL

logger = logging.getLogger(__name__)

# Back-compat aliases.
CLEANUP_MODEL = config.TRANSCRIPT_CLEANUP_MODEL
CLEANUP_SYSTEM_PROMPT = config.TRANSCRIPT_CLEANUP_PROMPT

_PROVIDER_ENV_KEYS = {
    TranscriptCleanupProvider.OPENAI: "OPENAI_API_KEY",
    TranscriptCleanupProvider.OPENROUTER: "OPENROUTER_API_KEY",
}

# OpenRouter attributes traffic to the app via these optional headers.
_OPENROUTER_HEADERS = {"X-Title": "OpenWhisper"}

# OpenAI's /models endpoint returns every model family; only chat-completions
# models make sense for cleanup. OpenRouter's catalog is already chat-only.
_OPENAI_CHAT_PREFIXES = ("gpt-", "chatgpt-", "o1", "o3", "o4")
_OPENAI_NON_CHAT_MARKERS = (
    "audio", "realtime", "tts", "whisper", "embedding", "moderation",
    "dall-e", "transcribe", "image", "search", "instruct",
)


@dataclass(frozen=True)
class CleanupInfo:
    """Provider/model that produced a cleaned transcript."""

    provider: str
    model: str


def provider_env_key(provider: str) -> str:
    """Return the environment variable name holding the provider's API key."""
    return _PROVIDER_ENV_KEYS.get(
        provider, _PROVIDER_ENV_KEYS[TranscriptCleanupProvider.OPENAI]
    )


def _provider_base_url(provider: str) -> Optional[str]:
    """Return the API base URL for a provider (None = OpenAI default)."""
    if provider == TranscriptCleanupProvider.OPENROUTER:
        return config.OPENROUTER_BASE_URL
    return None


def _provider_headers(provider: str) -> Optional[dict]:
    """Return extra default headers for a provider, if any."""
    if provider == TranscriptCleanupProvider.OPENROUTER:
        return dict(_OPENROUTER_HEADERS)
    return None


def find_api_key(provider: str) -> Optional[str]:
    """Get the provider's API key from environment variables or the .env file.

    Args:
        provider: A ``TranscriptCleanupProvider`` value.

    Returns:
        API key string, or None when unavailable.
    """
    env_key = provider_env_key(provider)
    api_key = os.getenv(env_key)
    if not api_key:
        try:
            from dotenv import load_dotenv

            env_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..",
                config.ENV_FILE,
            )
            load_dotenv(env_path)
            api_key = os.getenv(env_key)
        except ImportError:
            logger.warning(
                "python-dotenv not installed. Skipping .env file loading."
            )
        except Exception as exc:
            logger.warning("Failed to load .env file: %s", exc)
    return api_key


def _filter_openai_chat_models(model_ids: List[str]) -> List[str]:
    """Keep only OpenAI model ids usable with the chat completions API."""
    filtered = []
    for model_id in model_ids:
        lowered = model_id.lower()
        if not lowered.startswith(_OPENAI_CHAT_PREFIXES):
            continue
        if any(marker in lowered for marker in _OPENAI_NON_CHAT_MARKERS):
            continue
        filtered.append(model_id)
    return filtered


def list_cleanup_models(
    provider: str,
    api_key: Optional[str] = None,
    sort: Optional[str] = None,
) -> List[str]:
    """Fetch the provider's available chat model ids live from its API.

    Args:
        provider: A ``TranscriptCleanupProvider`` value.
        api_key: Optional explicit API key. Looked up from the environment /
            .env file when omitted.
        sort: Optional ``TranscriptCleanupModelSort`` value. OpenRouter
            supports server-side ranking via its ``sort`` query parameter;
            OpenAI does not, so anything but alphabetical is ignored there.

    Returns:
        List of model id strings — server ranking order when an OpenRouter
        sort is requested, alphabetical otherwise.

    Raises:
        RuntimeError: When no API key is available for the provider.
        Exception: Network/API errors from the underlying client.
    """
    key = api_key or find_api_key(provider)
    if not key:
        raise RuntimeError(
            f"No API key found for {provider} (set {provider_env_key(provider)})"
        )
    client = OpenAI(
        api_key=key,
        base_url=_provider_base_url(provider),
        default_headers=_provider_headers(provider),
        timeout=15.0,
    )
    server_sort = (
        provider == TranscriptCleanupProvider.OPENROUTER
        and sort
        and sort != TranscriptCleanupModelSort.ALPHABETICAL
    )
    if server_sort:
        # Preserve OpenRouter's ranking; sorting here would discard it.
        return [
            model.id
            for model in client.models.list(extra_query={"sort": sort})
        ]
    model_ids = [model.id for model in client.models.list()]
    if provider == TranscriptCleanupProvider.OPENAI:
        model_ids = _filter_openai_chat_models(model_ids)
    return sorted(model_ids)


def polish_cleanup_rule(
    instruction: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Rewrite a raw user instruction into a short imperative cleanup rule.

    Args:
        instruction: The user's raw typed or dictated instruction.
        provider: Optional ``TranscriptCleanupProvider`` value. Config
            default when omitted.
        model: Optional chat model id. Provider default when omitted.
        reasoning: Optional ``TranscriptCleanupReasoning`` value.

    Returns:
        Tuple of (rule_text, error). ``rule_text`` is the polished rule on
        success, or the stripped original instruction when polish is
        unavailable or failed; ``error`` is None only on success.
    """
    instruction = instruction.strip()
    if not instruction:
        return "", "empty instruction"
    cleaner = TranscriptCleanup(
        provider=provider, model=model, reasoning=reasoning
    )
    result = cleaner.cleanup(
        instruction,
        system_prompt=config.TRANSCRIPT_CLEANUP_RULE_POLISH_PROMPT,
    )
    return result.strip(), cleaner.last_error


class TranscriptCleanup:
    """Optional chat-model cleanup step applied after ASR."""

    def __init__(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        reasoning: Optional[str] = None,
    ):
        """Initialize the cleanup client.

        Args:
            provider: ``TranscriptCleanupProvider`` value. Defaults to config.
            model: Chat model id. Defaults to the provider's default model.
            api_key: Provider API key. Uses environment / .env if None.
            reasoning: ``TranscriptCleanupReasoning`` value ("off" disables).
        """
        self.provider = (
            provider if provider in TranscriptCleanupProvider.ALL
            else config.TRANSCRIPT_CLEANUP_PROVIDER
        )
        self.model = model or default_transcript_cleanup_model(self.provider)
        self.reasoning = (
            reasoning if reasoning in TranscriptCleanupReasoning.ALL
            else TranscriptCleanupReasoning.OFF
        )
        self.api_key = api_key or find_api_key(self.provider)
        self.client: Optional[OpenAI] = None
        # None after a successful cleanup() run; reason string otherwise.
        # Lets callers distinguish "cleanup ran, no changes" from "failed".
        self.last_error: Optional[str] = "not run"
        self._initialize_client()

    def _initialize_client(self) -> None:
        """Initialize the chat client when a key is available."""
        if self.api_key:
            try:
                self.client = OpenAI(
                    api_key=self.api_key,
                    base_url=_provider_base_url(self.provider),
                    default_headers=_provider_headers(self.provider),
                    timeout=config.TRANSCRIPT_CLEANUP_TIMEOUT_S,
                )
                logger.info(
                    "Transcript cleanup client initialized (%s)", self.provider
                )
            except Exception as exc:
                logger.error("Failed to initialize transcript cleanup client: %s", exc)
                self.client = None
        else:
            logger.debug(
                "No %s API key; transcript cleanup unavailable",
                provider_env_key(self.provider),
            )
            self.client = None

    def configure(
        self, provider: str, model: str, reasoning: Optional[str] = None
    ) -> None:
        """Apply the current provider/model selection, re-initializing if needed.

        Cheap when nothing changed; only a provider switch rebuilds the client
        (new base URL and API key). Called before each cleanup so settings
        changes take effect without restarting.

        Args:
            provider: ``TranscriptCleanupProvider`` value.
            model: Chat model id to use for cleanup requests.
            reasoning: ``TranscriptCleanupReasoning`` value ("off" disables).
        """
        if provider in TranscriptCleanupProvider.ALL and provider != self.provider:
            self.provider = provider
            self.api_key = find_api_key(provider)
            self._initialize_client()
        if model and model.strip():
            self.model = model.strip()
        if reasoning in TranscriptCleanupReasoning.ALL:
            self.reasoning = reasoning

    def is_available(self) -> bool:
        """Whether cleanup can be attempted."""
        return self.client is not None and self.api_key is not None

    def _request_options(self) -> dict:
        """Build per-request kwargs for the current reasoning level.

        Reasoning models reject an explicit ``temperature``, so it is only
        sent when reasoning is off. OpenAI takes ``reasoning_effort`` as a
        first-class param; OpenRouter takes a ``reasoning`` object.
        """
        if self.reasoning == TranscriptCleanupReasoning.OFF:
            return {"temperature": 0}
        if self.provider == TranscriptCleanupProvider.OPENROUTER:
            return {"extra_body": {"reasoning": {"effort": self.reasoning}}}
        return {"reasoning_effort": self.reasoning}

    def cleanup(self, text: str, system_prompt: Optional[str] = None) -> str:
        """Clean up transcript text, falling back to the original on failure.

        Args:
            text: Raw ASR transcript.
            system_prompt: Optional system prompt. Falls back to the config
                default when empty or omitted.

        Returns:
            Cleaned text, or the original text if cleanup is skipped or fails.
            ``last_error`` is None afterwards only when cleanup succeeded.
        """
        if not text or not text.strip():
            self.last_error = "empty input"
            return text

        if not self.is_available():
            self.last_error = "cleanup unavailable"
            logger.warning("Transcript cleanup unavailable; returning raw text")
            return text

        prompt = (system_prompt or "").strip() or config.TRANSCRIPT_CLEANUP_PROMPT

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text},
                ],
                **self._request_options(),
            )
            cleaned = (response.choices[0].message.content or "").strip()
            if not cleaned:
                self.last_error = "empty response"
                logger.warning("Transcript cleanup returned empty; using raw text")
                return text
            self.last_error = None
            return cleaned
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning("Transcript cleanup failed; using raw text: %s", exc)
            return text
