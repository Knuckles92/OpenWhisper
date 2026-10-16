"""Speech-to-text servers the API backend can upload audio to.

Every endpoint speaks OpenAI's ``POST /audio/transcriptions``. OpenAI stays
the default and keeps its own model list, hint fields and retirement dates;
the rest are presets for servers with a standard address (each one editable)
plus one Custom slot for anything else.

The connection half of an endpoint is a ``TextLLMProfile``: the same named
base URL and credential variable the text side uses, so the loopback test,
key lookup and destination copy are shared rather than repeated.

Settings: ``api_transcription_endpoint`` names the selected endpoint and
``api_transcription_servers`` holds each configurable endpoint's overrides
(``base_url``, ``api_key_env``, ``model``). OpenAI's model stays under
``api_transcription_model``, so existing settings mean what they always did.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from config import config
from services.settings import (
    SettingsKey,
    api_model_choices,
    resolve_api_transcription_model,
    settings_manager,
)
from services.text_llm import (
    PROFILE_KIND_CUSTOM,
    TextLLMProfile,
    consent_destination,
    normalize_base_url,
    resolve_api_key,
    validate_api_key_env,
)

logger = logging.getLogger(__name__)

OPENAI_ENDPOINT_ID = "openai"
CUSTOM_ENDPOINT_ID = "custom"

#: Read timeout for one upload to a hosted service.
HOSTED_TIMEOUT_S = 120.0
#: Read timeout for a server someone runs themselves. Such a server loads the
#: model on the first request and may run on a CPU, where one upload chunk
#: (several minutes of audio) takes minutes; Cancel still stops it at once.
SELF_HOSTED_TIMEOUT_S = 600.0

#: Speaches tags each catalog entry with a task; LocalAI's capabilities
#: catalog lists what each model can do.
_SPEECH_TASK = "automatic-speech-recognition"
_SPEECH_CAPABILITY = "transcript"


@dataclass(frozen=True)
class SpeechEndpoint:
    """One transcription server and how to talk to it."""

    connection: TextLLMProfile
    #: Model used until the user picks one.
    default_model: str
    #: Whether the user may change the address, key variable and model id.
    configurable: bool
    description: str = ""
    #: Catalog route under the base URL; None when the server has none, or
    #: when ``known_models`` already says what it serves.
    models_path: Optional[str] = "/models"
    known_models: Tuple[str, ...] = ()
    #: ``language`` value that asks for detection, for a server that assumes
    #: one language when the field is absent.
    auto_language: str = ""

    @property
    def id(self) -> str:
        return self.connection.id

    @property
    def name(self) -> str:
        return self.connection.name

    @property
    def base_url(self) -> Optional[str]:
        return self.connection.base_url

    @property
    def is_openai(self) -> bool:
        return self.id == OPENAI_ENDPOINT_ID

    @property
    def is_local(self) -> bool:
        """True when audio stays on this computer (a loopback address)."""
        return self.connection.is_local

    @property
    def is_configured(self) -> bool:
        return self.is_openai or bool(self.base_url)

    @property
    def self_hosted(self) -> bool:
        """True for a server the user runs: a server preset wherever it
        lives, or any endpoint on this computer (``TextLLMProfile``'s rule).

        The Custom slot is anything else, which is as likely a hosted
        gateway, so only its address decides.
        """
        return (self.configurable and self.id != CUSTOM_ENDPOINT_ID) or self.is_local

    @property
    def timeout_s(self) -> float:
        return SELF_HOSTED_TIMEOUT_S if self.self_hosted else HOSTED_TIMEOUT_S


def _endpoint(
    endpoint_id: str, name: str, base_url: Optional[str], api_key_env: str,
    default_model: str, *, hosted: bool = False, **details,
) -> SpeechEndpoint:
    return SpeechEndpoint(
        connection=TextLLMProfile(
            endpoint_id, name,
            PROFILE_KIND_CUSTOM if endpoint_id == CUSTOM_ENDPOINT_ID else endpoint_id,
            base_url, api_key_env, builtin=endpoint_id != CUSTOM_ENDPOINT_ID,
        ),
        default_model=default_model,
        configurable=not hosted,
        **details,
    )


#: Display order. Self-hosted presets ship each server's documented default
#: address; ``configured_endpoint`` applies the user's overrides.
ENDPOINTS: Tuple[SpeechEndpoint, ...] = (
    _endpoint(
        OPENAI_ENDPOINT_ID, "OpenAI", None, "OPENAI_API_KEY",
        config.DEFAULT_API_MODEL, hosted=True,
        description="OpenAI's transcription models. Needs an OpenAI API key.",
    ),
    _endpoint(
        "groq", "Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
        # Groq's catalog mixes in its chat models without marking either.
        "whisper-large-v3-turbo", hosted=True, models_path=None,
        known_models=("whisper-large-v3-turbo", "whisper-large-v3"),
        description="Open-weights Whisper models hosted by Groq. Needs a Groq API key.",
    ),
    _endpoint(
        "speaches", "Speaches", "http://127.0.0.1:8000/v1", "",
        "Systran/faster-whisper-small",
        description="A Speaches server. Lists the models it has downloaded.",
    ),
    _endpoint(
        "localai", "LocalAI", "http://127.0.0.1:8080/v1", "", "whisper-1",
        models_path="/models/capabilities",
        description="A LocalAI server with a transcription model installed.",
    ),
    _endpoint(
        "vllm", "vLLM", "http://127.0.0.1:8000/v1", "",
        "openai/whisper-large-v3-turbo",
        description="A vLLM server running a Whisper model (vllm serve).",
    ),
    _endpoint(
        # whisper-server answers on /inference unless started with
        # --inference-path /v1/audio/transcriptions. It ignores ``model``,
        # has no catalog, and assumes English without ``language``.
        "whisper_cpp", "whisper.cpp", "http://127.0.0.1:8080/v1", "", "whisper-1",
        models_path=None, auto_language="auto",
        description=(
            "whisper.cpp's whisper-server, started with "
            "--inference-path /v1/audio/transcriptions."
        ),
    ),
    _endpoint(
        CUSTOM_ENDPOINT_ID, "Custom server", None, "", "whisper-1",
        description="Any server with an OpenAI-compatible /audio/transcriptions route.",
    ),
)
ENDPOINT_IDS: Tuple[str, ...] = tuple(endpoint.id for endpoint in ENDPOINTS)
_BY_ID: Dict[str, SpeechEndpoint] = {endpoint.id: endpoint for endpoint in ENDPOINTS}


def _settings(settings: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    return settings_manager.load_all_settings() if settings is None else settings


def _overrides(settings: Mapping[str, Any], endpoint_id: str) -> Dict[str, Any]:
    servers = settings.get(SettingsKey.API_TRANSCRIPTION_SERVERS)
    entry = servers.get(endpoint_id) if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else {}


def configured_endpoint(
    endpoint_id: str, settings: Optional[Mapping[str, Any]] = None,
) -> SpeechEndpoint:
    """``endpoint_id``'s preset with the user's address and key variable.

    An unknown id is OpenAI's, so a damaged setting never sends audio
    somewhere the user did not choose.
    """
    endpoint = _BY_ID.get(endpoint_id, _BY_ID[OPENAI_ENDPOINT_ID])
    if not endpoint.configurable:
        return endpoint
    saved = _overrides(_settings(settings), endpoint.id)
    try:
        url = saved.get("base_url")
        env = saved.get("api_key_env")
        connection = replace(
            endpoint.connection,
            base_url=valid_server_url(url) if isinstance(url, str) and url else endpoint.base_url,
            api_key_env=validate_api_key_env(env) if isinstance(env, str) else "",
        )
    except ValueError:
        logger.warning("Ignoring an invalid saved %s server", endpoint.name)
        return endpoint
    return replace(endpoint, connection=connection)


def selected_endpoint(settings: Optional[Mapping[str, Any]] = None) -> SpeechEndpoint:
    """The endpoint the API backend uploads to.

    A Custom slot without an address can't be used, so it reads as OpenAI.
    """
    settings = _settings(settings)
    endpoint_id = settings.get(SettingsKey.API_TRANSCRIPTION_ENDPOINT)
    endpoint = configured_endpoint(
        endpoint_id if isinstance(endpoint_id, str) else OPENAI_ENDPOINT_ID, settings,
    )
    return endpoint if endpoint.is_configured else _BY_ID[OPENAI_ENDPOINT_ID]


def endpoint_model(
    endpoint: SpeechEndpoint, settings: Optional[Mapping[str, Any]] = None,
) -> str:
    """The model ``endpoint`` transcribes with.

    OpenAI's goes through its retirement rules; any other server's is the
    user's own model id, or the preset's default.
    """
    settings = _settings(settings)
    if endpoint.is_openai:
        return resolve_api_transcription_model(settings)
    model = _overrides(settings, endpoint.id).get("model")
    return model.strip() if isinstance(model, str) and model.strip() else endpoint.default_model


def selected_model(settings: Optional[Mapping[str, Any]] = None) -> str:
    settings = _settings(settings)
    return endpoint_model(selected_endpoint(settings), settings)


def valid_server_url(url: str) -> str:
    """A canonical server URL, ``/v1`` when it has no path; else ``ValueError``."""
    normalized = normalize_base_url(url)
    parts = urlsplit(normalized)
    if parts.username or parts.password or parts.query:
        raise ValueError("Use a server URL without credentials or query parameters.")
    if not parts.path:
        normalized += "/v1"
    return normalized


def save_selection(endpoint_id: str) -> None:
    if endpoint_id not in _BY_ID:
        raise ValueError(f"Unknown transcription server: {endpoint_id}")
    settings_manager.save_setting(SettingsKey.API_TRANSCRIPTION_ENDPOINT, endpoint_id)


def save_model(endpoint_id: str, model: str) -> None:
    """Remember ``endpoint_id``'s model; OpenAI's keeps its own setting."""
    model = (model or "").strip()
    if not model:
        raise ValueError("Enter a model id.")
    if endpoint_id == OPENAI_ENDPOINT_ID:
        if model not in config.API_MODEL_CHOICES:
            raise ValueError(f"OpenAI has no transcription model {model}.")
        settings_manager.save_setting(SettingsKey.API_TRANSCRIPTION_MODEL, model)
        return
    _update_server(endpoint_id, {"model": model})


def save_server(endpoint_id: str, *, base_url: str, api_key_env: str, model: str) -> None:
    """Persist a configurable server's address, key variable and model.

    Raises ``ValueError`` with user-facing text for a field that won't do.
    """
    endpoint = _BY_ID.get(endpoint_id)
    if endpoint is None or not endpoint.configurable:
        raise ValueError(f"{endpoint.name if endpoint else endpoint_id} can't be edited.")
    model = (model or "").strip()
    if not model:
        raise ValueError("Enter a model id.")
    _update_server(endpoint_id, {
        "base_url": valid_server_url(base_url),
        "api_key_env": validate_api_key_env(api_key_env),
        "model": model,
    })


def _update_server(endpoint_id: str, values: Dict[str, str]) -> None:
    if endpoint_id not in _BY_ID or not _BY_ID[endpoint_id].configurable:
        raise ValueError(f"{endpoint_id} has no server settings.")

    def apply(settings: Dict[str, Any]) -> None:
        servers = settings.get(SettingsKey.API_TRANSCRIPTION_SERVERS)
        servers = dict(servers) if isinstance(servers, dict) else {}
        entry = servers.get(endpoint_id)
        servers[endpoint_id] = {**(entry if isinstance(entry, dict) else {}), **values}
        settings[SettingsKey.API_TRANSCRIPTION_SERVERS] = servers

    settings_manager.mutate_settings(apply)


def api_key(endpoint: SpeechEndpoint) -> Optional[str]:
    """The key to send, a placeholder for a server without one, or None if missing."""
    return resolve_api_key(endpoint.connection)


def destination(endpoint: SpeechEndpoint) -> str:
    """Where uploaded audio goes, for privacy copy."""
    return consent_destination(endpoint.connection)


def audio_destination_sentence(endpoint: SpeechEndpoint) -> str:
    """One sentence saying where dictated audio goes."""
    if endpoint.is_local:
        return f"Audio stays on this computer: it goes to {destination(endpoint)}."
    return f"Audio is uploaded to {destination(endpoint)}."


def credential_owners(settings: Optional[Mapping[str, Any]] = None) -> List[Tuple[str, str]]:
    """``(variable, endpoint name)`` for every usable endpoint that sends a key."""
    settings = _settings(settings)
    owners = []
    for endpoint_id in ENDPOINT_IDS:
        endpoint = configured_endpoint(endpoint_id, settings)
        if endpoint.connection.api_key_env and endpoint.is_configured:
            owners.append((endpoint.connection.api_key_env, endpoint.name))
    return owners


def _is_speech_model(item: Dict[str, Any]) -> bool:
    task = item.get("task")
    if task is not None and task != _SPEECH_TASK:
        return False
    capabilities = item.get("capabilities")
    return not isinstance(capabilities, list) or _SPEECH_CAPABILITY in capabilities


def list_models(endpoint: SpeechEndpoint, *, timeout: float = 5.0) -> List[str]:
    """Speech model ids ``endpoint`` offers.

    OpenAI's are the curated list (its catalog holds every other model too).
    Other servers are asked over their catalog route; a server without one
    leaves the user's own model id as the only choice. Raises on a failed
    request so the caller can say so.
    """
    if endpoint.is_openai:
        return list(api_model_choices())
    if endpoint.models_path is None:
        return list(endpoint.known_models)
    import httpx

    key = api_key(endpoint)
    response = httpx.get(
        f"{endpoint.base_url}{endpoint.models_path}",
        headers={"Authorization": f"Bearer {key}"} if key else {},
        timeout=timeout,
    )
    if response.status_code == 404:
        return []
    response.raise_for_status()
    data = response.json().get("data")
    return sorted(
        item["id"] for item in (data if isinstance(data, list) else ())
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
        and _is_speech_model(item)
    )
