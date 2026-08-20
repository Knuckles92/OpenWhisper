"""Shared OpenAI-compatible text-LLM endpoint profiles.

Every chat-completions caller (transcript cleanup, meeting intelligence,
catalog listing) resolves a ``TextLLMProfile`` here. Built-in OpenAI and
OpenRouter profiles are immutable; users may add named custom endpoints.
API keys stay in the environment / ``.env`` file and are never persisted.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from openai import OpenAI

from config import config

logger = logging.getLogger(__name__)

PROFILE_KIND_OPENAI = "openai"
PROFILE_KIND_OPENROUTER = "openrouter"
PROFILE_KIND_CUSTOM = "custom"
BUILTIN_KINDS = (PROFILE_KIND_OPENAI, PROFILE_KIND_OPENROUTER)
PROFILE_KINDS = (*BUILTIN_KINDS, PROFILE_KIND_CUSTOM)

OPENAI_PROFILE_ID = "openai"
OPENROUTER_PROFILE_ID = "openrouter"
BUILTIN_PROFILE_IDS = (OPENAI_PROFILE_ID, OPENROUTER_PROFILE_ID)

# OpenAI's client requires a non-empty api_key even when the server ignores it.
AUTH_FREE_API_KEY = "dummy"
SIDECAR_API_KEY_ENV = "OPENWHISPER_LLM_API_KEY"

MAX_PROFILE_NAME_LEN = 80
MAX_CUSTOM_PROFILES = 50
_CUSTOM_ID_PREFIX = "custom_"
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_OPENROUTER_HEADERS = {"X-Title": "OpenWhisper"}
_OPENAI_CHAT_PREFIXES = ("gpt-", "chatgpt-", "o1", "o3", "o4")
_OPENAI_NON_CHAT_MARKERS = (
    "audio", "realtime", "tts", "whisper", "embedding", "moderation",
    "dall-e", "transcribe", "image", "search", "instruct",
)


@dataclass(frozen=True)
class TextLLMProfile:
    """One named chat-completions endpoint.

    Attributes:
        id: Stable profile id (``openai``, ``openrouter``, or ``custom_…``).
        name: User-visible label.
        kind: Compatibility flavor (``openai``, ``openrouter``, ``custom``).
        base_url: OpenAI-compatible API root, or None for official OpenAI.
        api_key_env: Environment variable holding the key. Empty means
            the endpoint does not require authentication.
        builtin: True for the immutable OpenAI and OpenRouter profiles.
    """

    id: str
    name: str
    kind: str
    base_url: Optional[str]
    api_key_env: str
    builtin: bool = False

    @property
    def requires_api_key(self) -> bool:
        """Whether a key must be present in the environment."""
        return bool(self.api_key_env)

    @property
    def is_local(self) -> bool:
        """True when the endpoint host is loopback."""
        if not self.base_url:
            return False
        host = (urlsplit(self.base_url).hostname or "").lower()
        return host in _LOCAL_HOSTS


@dataclass(frozen=True)
class TextLLMSnapshot:
    """Non-secret connection snapshot persisted with a meeting."""

    profile_id: str
    name: str
    kind: str
    base_url: Optional[str]
    api_key_env: str

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for settings / meeting rows."""
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "kind": self.kind,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
        }

    def to_profile(self) -> TextLLMProfile:
        """Rebuild a runtime profile from this snapshot."""
        return TextLLMProfile(
            id=self.profile_id,
            name=self.name,
            kind=self.kind if self.kind in PROFILE_KINDS else PROFILE_KIND_CUSTOM,
            base_url=self.base_url,
            api_key_env=self.api_key_env or "",
            builtin=(
                self.profile_id in BUILTIN_PROFILE_IDS
                and self.kind == self.profile_id
            ),
        )


def builtin_profiles() -> Tuple[TextLLMProfile, ...]:
    """Return the immutable OpenAI and OpenRouter profiles."""
    return (
        TextLLMProfile(
            id=OPENAI_PROFILE_ID,
            name="OpenAI",
            kind=PROFILE_KIND_OPENAI,
            base_url=None,
            api_key_env="OPENAI_API_KEY",
            builtin=True,
        ),
        TextLLMProfile(
            id=OPENROUTER_PROFILE_ID,
            name="OpenRouter",
            kind=PROFILE_KIND_OPENROUTER,
            base_url=config.OPENROUTER_BASE_URL,
            api_key_env="OPENROUTER_API_KEY",
            builtin=True,
        ),
    )


def builtin_profile(profile_id: str) -> Optional[TextLLMProfile]:
    """Return a built-in profile by id, or None."""
    for profile in builtin_profiles():
        if profile.id == profile_id:
            return profile
    return None


def normalize_base_url(url: str) -> str:
    """Validate and canonicalize an OpenAI-compatible base URL.

    Args:
        url: User-supplied URL.

    Returns:
        Canonical ``http(s)://host[:port][/path]`` without a trailing slash.

    Raises:
        ValueError: When the URL is empty or not http(s) with a host.
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("Base URL is required")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError("Base URL must use http or https")
    if not parts.netloc:
        raise ValueError("Base URL is missing a host")
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def validate_profile_name(name: str) -> str:
    """Return a stripped profile name, or raise ``ValueError``."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("Endpoint name is required")
    if len(cleaned) > MAX_PROFILE_NAME_LEN:
        raise ValueError(
            f"Endpoint name must be {MAX_PROFILE_NAME_LEN} characters or fewer"
        )
    return cleaned


def validate_api_key_env(name: str) -> str:
    """Return a stripped env-var name (possibly empty), or raise ``ValueError``."""
    cleaned = (name or "").strip()
    if not cleaned:
        return ""
    if not _ENV_NAME_RE.match(cleaned):
        raise ValueError(
            "API key variable must be empty or an uppercase identifier "
            "(A–Z, digits, underscore)"
        )
    return cleaned


def new_custom_profile_id() -> str:
    """Allocate a stable id for a user-created endpoint."""
    return f"{_CUSTOM_ID_PREFIX}{secrets.token_hex(4)}"


def snapshot_from_profile(profile: TextLLMProfile) -> TextLLMSnapshot:
    """Build a persistable snapshot from a live profile."""
    return TextLLMSnapshot(
        profile_id=profile.id,
        name=profile.name,
        kind=profile.kind,
        base_url=profile.base_url,
        api_key_env=profile.api_key_env,
    )


def snapshot_from_mapping(raw: Any) -> Optional[TextLLMSnapshot]:
    """Parse a stored snapshot mapping, or None when invalid."""
    if isinstance(raw, str) and raw.strip():
        try:
            import json

            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, dict):
        return None
    profile_id = raw.get("profile_id") or raw.get("id")
    name = raw.get("name")
    kind = raw.get("kind") or PROFILE_KIND_CUSTOM
    base_url = raw.get("base_url")
    api_key_env = raw.get("api_key_env") or ""
    if not isinstance(profile_id, str) or not profile_id.strip():
        return None
    if not isinstance(name, str) or not name.strip():
        name = profile_id
    if kind not in PROFILE_KINDS:
        kind = PROFILE_KIND_CUSTOM
    url = None
    if isinstance(base_url, str) and base_url.strip():
        try:
            url = normalize_base_url(base_url)
        except ValueError:
            return None
    if kind != PROFILE_KIND_OPENAI and not url:
        return None
    if not isinstance(api_key_env, str):
        api_key_env = ""
    else:
        try:
            api_key_env = validate_api_key_env(api_key_env)
        except ValueError:
            api_key_env = ""
    return TextLLMSnapshot(
        profile_id=profile_id.strip(),
        name=name.strip(),
        kind=kind,
        base_url=url,
        api_key_env=api_key_env,
    )


def _load_settings(settings: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if settings is not None:
        return settings
    try:
        from services.settings import settings_manager

        return settings_manager.load_all_settings()
    except Exception:
        return {}


def _custom_payload(settings: Dict[str, Any]) -> List[Any]:
    try:
        from services.settings import SettingsKey

        raw = settings.get(SettingsKey.TEXT_LLM_PROFILES)
    except Exception:
        raw = settings.get("text_llm_profiles")
    return raw if isinstance(raw, list) else []


def parse_custom_profile(raw: Any) -> Optional[TextLLMProfile]:
    """Validate one stored custom-profile dict."""
    if not isinstance(raw, dict):
        return None
    profile_id = raw.get("id")
    if not isinstance(profile_id, str) or not profile_id.startswith(
        _CUSTOM_ID_PREFIX
    ):
        return None
    try:
        name = validate_profile_name(str(raw.get("name") or ""))
        base_url = normalize_base_url(str(raw.get("base_url") or ""))
        api_key_env = validate_api_key_env(str(raw.get("api_key_env") or ""))
    except ValueError:
        return None
    return TextLLMProfile(
        id=profile_id,
        name=name,
        kind=PROFILE_KIND_CUSTOM,
        base_url=base_url,
        api_key_env=api_key_env,
        builtin=False,
    )


def list_custom_profiles(
    settings: Optional[Dict[str, Any]] = None,
) -> List[TextLLMProfile]:
    """Return validated custom profiles from settings, in stored order."""
    seen = set()
    profiles: List[TextLLMProfile] = []
    for raw in _custom_payload(_load_settings(settings)):
        profile = parse_custom_profile(raw)
        if profile is None or profile.id in seen:
            continue
        seen.add(profile.id)
        profiles.append(profile)
        if len(profiles) >= MAX_CUSTOM_PROFILES:
            break
    return profiles


def list_profiles(
    settings: Optional[Dict[str, Any]] = None,
) -> List[TextLLMProfile]:
    """Return built-in profiles followed by the user's custom endpoints."""
    return [*builtin_profiles(), *list_custom_profiles(settings)]


def get_profile(
    profile_id: Optional[str],
    settings: Optional[Dict[str, Any]] = None,
) -> Optional[TextLLMProfile]:
    """Look up a built-in or custom profile by id."""
    if not isinstance(profile_id, str) or not profile_id.strip():
        return None
    profile_id = profile_id.strip()
    builtin = builtin_profile(profile_id)
    if builtin is not None:
        return builtin
    for profile in list_custom_profiles(settings):
        if profile.id == profile_id:
            return profile
    return None


def known_profile_ids(settings: Optional[Dict[str, Any]] = None) -> Tuple[str, ...]:
    """Return every currently valid profile id."""
    return tuple(profile.id for profile in list_profiles(settings))


def custom_profiles_payload(profiles: Sequence[TextLLMProfile]) -> List[Dict[str, str]]:
    """Serialize custom profiles for the settings JSON file."""
    payload = []
    for profile in profiles:
        if profile.builtin or profile.kind != PROFILE_KIND_CUSTOM:
            continue
        payload.append({
            "id": profile.id,
            "name": profile.name,
            "base_url": profile.base_url or "",
            "api_key_env": profile.api_key_env,
        })
        if len(payload) >= MAX_CUSTOM_PROFILES:
            break
    return payload


def upsert_custom_profile(
    settings: Dict[str, Any],
    *,
    name: str,
    base_url: str,
    api_key_env: str = "",
    profile_id: Optional[str] = None,
) -> TextLLMProfile:
    """Create or replace a custom profile inside ``settings``.

    Args:
        settings: Mutable settings dict (caller persists it).
        name: Display name.
        base_url: OpenAI-compatible API root.
        api_key_env: Optional environment-variable name.
        profile_id: Existing custom id to replace; allocated when omitted.

    Returns:
        The stored ``TextLLMProfile``.

    Raises:
        ValueError: On invalid fields or when the custom-profile cap is hit.
    """
    from services.settings import SettingsKey

    name = validate_profile_name(name)
    base_url = normalize_base_url(base_url)
    api_key_env = validate_api_key_env(api_key_env)
    existing = list_custom_profiles(settings)
    if profile_id:
        if get_profile(profile_id, settings) is None or (
            builtin_profile(profile_id) is not None
        ):
            raise ValueError("Cannot edit a built-in endpoint")
        updated = [
            TextLLMProfile(
                id=profile_id,
                name=name,
                kind=PROFILE_KIND_CUSTOM,
                base_url=base_url,
                api_key_env=api_key_env,
            )
            if profile.id == profile_id else profile
            for profile in existing
        ]
        profile = next(p for p in updated if p.id == profile_id)
    else:
        if len(existing) >= MAX_CUSTOM_PROFILES:
            raise ValueError(
                f"At most {MAX_CUSTOM_PROFILES} custom endpoints can be saved"
            )
        profile = TextLLMProfile(
            id=new_custom_profile_id(),
            name=name,
            kind=PROFILE_KIND_CUSTOM,
            base_url=base_url,
            api_key_env=api_key_env,
        )
        updated = [*existing, profile]
    settings[SettingsKey.TEXT_LLM_PROFILES] = custom_profiles_payload(updated)
    return profile


def remove_custom_profile(settings: Dict[str, Any], profile_id: str) -> bool:
    """Delete a custom profile from ``settings``.

    Args:
        settings: Mutable settings dict (caller persists it).
        profile_id: Custom profile id.

    Returns:
        True when a custom profile was removed.

    Raises:
        ValueError: When the id belongs to a built-in profile.
    """
    from services.settings import SettingsKey

    if builtin_profile(profile_id) is not None:
        raise ValueError("Built-in endpoints cannot be deleted")
    remaining = [
        profile for profile in list_custom_profiles(settings)
        if profile.id != profile_id
    ]
    removed = len(remaining) != len(list_custom_profiles(settings))
    settings[SettingsKey.TEXT_LLM_PROFILES] = custom_profiles_payload(remaining)
    return removed


def profile_display_name(
    profile_id: Optional[str],
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """User-visible name for a profile id, falling back to the raw id."""
    profile = get_profile(profile_id, settings)
    if profile is not None:
        return profile.name
    return (profile_id or "").strip()


def default_model_for_profile(profile: TextLLMProfile) -> str:
    """Built-in default model id for a profile (empty for custom)."""
    if profile.id == OPENROUTER_PROFILE_ID or profile.kind == PROFILE_KIND_OPENROUTER:
        return config.TRANSCRIPT_CLEANUP_OPENROUTER_MODEL
    if profile.id == OPENAI_PROFILE_ID or profile.kind == PROFILE_KIND_OPENAI:
        return config.TRANSCRIPT_CLEANUP_MODEL
    return ""


def lookup_env_value(env_name: str) -> Optional[str]:
    """Read one environment / ``.env`` value without logging it."""
    if not env_name:
        return None
    value = os.getenv(env_name)
    if value:
        return value
    try:
        from dotenv import load_dotenv
        from config import env_file_path

        load_dotenv(env_file_path())
        return os.getenv(env_name)
    except ImportError:
        logger.warning("python-dotenv not installed. Skipping .env file loading.")
    except Exception as exc:
        logger.warning("Failed to load .env file: %s", exc)
    return None


def resolve_api_key(profile: TextLLMProfile) -> Optional[str]:
    """Return the SDK key for ``profile``, or None when a required key is missing."""
    if not profile.api_key_env:
        return AUTH_FREE_API_KEY
    return lookup_env_value(profile.api_key_env)


def credential_status(profile: TextLLMProfile) -> Tuple[bool, str]:
    """Return ``(available, status_copy)`` for the profile's credential."""
    if not profile.api_key_env:
        return True, "No API key required"
    if lookup_env_value(profile.api_key_env):
        return True, f"{profile.api_key_env} found"
    return False, f"Requires {profile.api_key_env}"


def connection_fingerprint(profile: TextLLMProfile) -> Tuple[Any, ...]:
    """Values that require rebuilding an OpenAI client when they change."""
    return (
        profile.id,
        profile.kind,
        profile.base_url,
        profile.api_key_env,
        resolve_api_key(profile),
    )


def provider_headers(profile: TextLLMProfile) -> Optional[Dict[str, str]]:
    """Extra default headers for a profile, if any."""
    if profile.kind == PROFILE_KIND_OPENROUTER:
        return dict(_OPENROUTER_HEADERS)
    return None


def create_openai_client(
    profile: TextLLMProfile,
    *,
    timeout: float = 15.0,
    api_key: Optional[str] = None,
) -> OpenAI:
    """Build an OpenAI SDK client for ``profile``.

    Args:
        profile: Resolved endpoint profile.
        timeout: Client timeout in seconds.
        api_key: Explicit key. Resolved from the environment when omitted.

    Returns:
        An ``OpenAI`` client.

    Raises:
        RuntimeError: When a required API key is missing.
    """
    key = api_key or resolve_api_key(profile)
    if not key:
        raise RuntimeError(
            f"No API key found for {profile.name} (set {profile.api_key_env})"
        )
    return OpenAI(
        api_key=key,
        base_url=profile.base_url,
        default_headers=provider_headers(profile),
        timeout=timeout,
    )


def filter_openai_chat_models(model_ids: Iterable[str]) -> List[str]:
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


def list_chat_models(
    profile: TextLLMProfile,
    *,
    api_key: Optional[str] = None,
    sort: Optional[str] = None,
    timeout: float = 15.0,
) -> List[str]:
    """Fetch chat model ids from the profile's ``/models`` endpoint.

    Catalog failure is the caller's problem: the UI still lets the user type
    a model id by hand.

    Args:
        profile: Resolved endpoint profile.
        api_key: Optional explicit API key.
        sort: Optional OpenRouter sort key. Ignored for other profiles.
        timeout: Client timeout in seconds.

    Returns:
        Model id strings.

    Raises:
        RuntimeError: When a required API key is missing.
        Exception: Network/API errors from the underlying client.
    """
    client = create_openai_client(profile, timeout=timeout, api_key=api_key)
    server_sort = (
        profile.kind == PROFILE_KIND_OPENROUTER
        and sort
        and sort != "alphabetical"
    )
    if server_sort:
        return [model.id for model in client.models.list(extra_query={"sort": sort})]
    model_ids = [model.id for model in client.models.list()]
    if profile.kind == PROFILE_KIND_OPENAI:
        model_ids = filter_openai_chat_models(model_ids)
    return sorted(model_ids)


def chat_request_options(profile: TextLLMProfile, reasoning: str = "off") -> dict:
    """Build per-request kwargs for cleanup-style chat calls.

    Custom endpoints never receive reasoning parameters; they get
    ``temperature=0`` so local servers that reject unknown fields still work.
    """
    if reasoning == "off" or profile.kind == PROFILE_KIND_CUSTOM:
        return {"temperature": 0}
    if profile.kind == PROFILE_KIND_OPENROUTER:
        return {"extra_body": {"reasoning": {"effort": reasoning}}}
    return {"reasoning_effort": reasoning}


def consent_destination(profile: TextLLMProfile) -> str:
    """Human-readable destination used by the meeting consent dialog."""
    if profile.is_local:
        host = urlsplit(profile.base_url or "").netloc or "localhost"
        return f"your local server at {host}"
    if profile.kind == PROFILE_KIND_OPENROUTER:
        return "OpenRouter (openrouter.ai)"
    if profile.kind == PROFILE_KIND_OPENAI:
        return "OpenAI (api.openai.com)"
    host = urlsplit(profile.base_url or "").netloc or profile.name
    return f"{profile.name} ({host})"


def destination_is_remote(profile: TextLLMProfile) -> bool:
    """True when transcript text would leave this machine."""
    return not profile.is_local


def snapshot_from_meeting(
    meeting: Optional[Dict[str, Any]],
    settings: Optional[Dict[str, Any]] = None,
    *,
    fallback_provider: str = OPENROUTER_PROFILE_ID,
) -> TextLLMSnapshot:
    """Resolve the endpoint snapshot stored on a meeting row.

    Prefers the persisted ``agent_endpoint_json`` snapshot so deleted or
    renamed custom profiles can still be retried. Older rows reconstruct a
    built-in snapshot from ``agent_provider``.
    """
    meeting = meeting or {}
    snapshot = snapshot_from_mapping(meeting.get("agent_endpoint_json"))
    if snapshot is not None:
        return snapshot
    provider = meeting.get("agent_provider") or fallback_provider
    profile = get_profile(provider, settings) or builtin_profile(
        fallback_provider
    ) or builtin_profiles()[1]
    return snapshot_from_profile(profile)


def profile_from_agent_config(
    provider: str,
    endpoint: Optional[Any] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> TextLLMProfile:
    """Resolve the profile an agent core should call.

    Args:
        provider: Profile id (legacy ``openai`` / ``openrouter`` still work).
        endpoint: Optional snapshot mapping stored on the meeting.
        settings: Optional settings dict for custom-profile lookup.
    """
    snapshot = snapshot_from_mapping(endpoint)
    if snapshot is not None:
        return snapshot.to_profile()
    profile = get_profile(provider, settings)
    if profile is not None:
        return profile
    return builtin_profile(OPENROUTER_PROFILE_ID) or builtin_profiles()[1]
