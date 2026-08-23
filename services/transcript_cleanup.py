"""Post-ASR cleanup via OpenAI-compatible chat models."""
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from openai import OpenAI

from config import config
from services.text_llm import (
    AUTH_FREE_API_KEY,
    connection_fingerprint,
    create_openai_client,
    chat_request_options,
    default_model_for_profile,
    filter_openai_chat_models,
    get_profile,
    list_chat_models,
    lookup_env_value,
    resolve_api_key,
)
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
        profile = get_profile(provider)
        if profile is not None:
            return default_model_for_profile(profile)
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


@dataclass(frozen=True)
class CleanupInfo:
    """Provider/model that produced a cleaned transcript."""

    provider: str
    model: str


def provider_env_key(provider: str) -> str:
    """Return the environment variable name holding the provider's API key."""
    profile = get_profile(provider)
    if profile is not None:
        return profile.api_key_env or ""
    return _PROVIDER_ENV_KEYS.get(
        provider, _PROVIDER_ENV_KEYS[TranscriptCleanupProvider.OPENAI]
    )


def _provider_base_url(provider: str) -> Optional[str]:
    profile = get_profile(provider)
    if profile is not None:
        return profile.base_url
    if provider == TranscriptCleanupProvider.OPENROUTER:
        return config.OPENROUTER_BASE_URL
    return None


def _provider_headers(provider: str) -> Optional[dict]:
    profile = get_profile(provider)
    if profile is not None and profile.kind == TranscriptCleanupProvider.OPENROUTER:
        from services.text_llm import provider_headers

        return provider_headers(profile)
    if provider == TranscriptCleanupProvider.OPENROUTER:
        return {"X-Title": "OpenWhisper"}
    return None


def find_api_key(provider: str) -> Optional[str]:
    """Resolve credentials, including a dummy key for auth-free endpoints."""
    profile = get_profile(provider)
    if profile is not None:
        return resolve_api_key(profile)
    env_key = provider_env_key(provider)
    if not env_key:
        return AUTH_FREE_API_KEY
    return lookup_env_value(env_key)


def _filter_openai_chat_models(model_ids: List[str]) -> List[str]:
    return filter_openai_chat_models(model_ids)


def list_cleanup_models(
    provider: str,
    api_key: Optional[str] = None,
    sort: Optional[str] = None,
) -> List[str]:
    """Fetch chat model IDs, preserving requested OpenRouter server ranking."""
    profile = get_profile(provider)
    if profile is not None:
        return list_chat_models(profile, api_key=api_key, sort=sort)
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
    """Return ``(rule, error)``, falling back to the stripped instruction."""
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
        self.provider = self._normalize_provider(provider)
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
        self._connection: Optional[tuple] = None
        self._initialize_client()

    @staticmethod
    def _normalize_provider(provider: Optional[str]) -> str:
        if isinstance(provider, str) and get_profile(provider) is not None:
            return provider
        if provider in TranscriptCleanupProvider.ALL:
            return provider
        return config.TRANSCRIPT_CLEANUP_PROVIDER

    def _initialize_client(self) -> None:
        profile = get_profile(self.provider)
        if profile is not None:
            key = self.api_key or find_api_key(self.provider)
            self.api_key = key
            if not key:
                logger.debug(
                    "No %s API key; transcript cleanup unavailable",
                    profile.api_key_env,
                )
                self.client = None
                self._connection = None
                return
            try:
                self.client = create_openai_client(
                    profile,
                    timeout=config.TRANSCRIPT_CLEANUP_TIMEOUT_S,
                    api_key=key,
                )
                self._connection = connection_fingerprint(profile)
                logger.info(
                    "Transcript cleanup client initialized (%s)", profile.id
                )
            except Exception as exc:
                logger.error(
                    "Failed to initialize transcript cleanup client: %s", exc
                )
                self.client = None
                self._connection = None
            return

        if self.api_key:
            try:
                self.client = OpenAI(
                    api_key=self.api_key,
                    base_url=_provider_base_url(self.provider),
                    default_headers=_provider_headers(self.provider),
                    timeout=config.TRANSCRIPT_CLEANUP_TIMEOUT_S,
                )
                self._connection = (
                    self.provider,
                    _provider_base_url(self.provider),
                    provider_env_key(self.provider),
                    self.api_key,
                )
                logger.info(
                    "Transcript cleanup client initialized (%s)", self.provider
                )
            except Exception as exc:
                logger.error(
                    "Failed to initialize transcript cleanup client: %s", exc
                )
                self.client = None
                self._connection = None
        else:
            logger.debug(
                "No %s API key; transcript cleanup unavailable",
                provider_env_key(self.provider),
            )
            self.client = None
            self._connection = None

    def configure(
        self, provider: str, model: str, reasoning: Optional[str] = None
    ) -> None:
        """Apply settings, rebuilding only when endpoint or credentials change."""
        normalized = self._normalize_provider(provider)
        profile = get_profile(normalized)
        fingerprint = (
            connection_fingerprint(profile) if profile is not None else None
        )
        needs_rebuild = normalized != self.provider or (
            fingerprint is not None and fingerprint != self._connection
        )
        if needs_rebuild:
            self.provider = normalized
            self.api_key = find_api_key(normalized)
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
        first-class param; OpenRouter takes a ``reasoning`` object. Custom
        endpoints always use ``temperature=0``.
        """
        profile = get_profile(self.provider)
        if profile is not None:
            return chat_request_options(profile, self.reasoning)
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
