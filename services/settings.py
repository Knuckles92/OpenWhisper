"""Persistent application settings and validated resolvers."""
import json
import os
import logging
import threading
from typing import Dict, Any, Final, List, Tuple, Optional
from config import config

logger = logging.getLogger(__name__)


class SettingsKey:
    """Keys persisted in the settings JSON file."""
    HOTKEYS: Final[str] = "hotkeys"
    SELECTED_MODEL: Final[str] = "selected_model"
    AUDIO_INPUT_DEVICE: Final[str] = "audio_input_device"
    WINDOW_GEOMETRY: Final[str] = "window_geometry"
    COMPACT_WINDOW_GEOMETRY: Final[str] = "compact_window_geometry"
    COMPACT_MODE: Final[str] = "compact_mode"
    AUTO_PASTE: Final[str] = "auto_paste"
    COPY_CLIPBOARD: Final[str] = "copy_clipboard"
    TRANSCRIPT_CLEANUP_ENABLED: Final[str] = "transcript_cleanup_enabled"
    TRANSCRIPT_CLEANUP_PROMPT: Final[str] = "transcript_cleanup_prompt"
    TRANSCRIPT_CLEANUP_PROVIDER: Final[str] = "transcript_cleanup_provider"
    TRANSCRIPT_CLEANUP_MODEL: Final[str] = "transcript_cleanup_model"
    TRANSCRIPT_CLEANUP_MODEL_SORT: Final[str] = "transcript_cleanup_model_sort"
    TRANSCRIPT_CLEANUP_REASONING: Final[str] = "transcript_cleanup_reasoning"
    # Named OpenAI-compatible endpoints (custom profiles only; builtins omitted).
    TEXT_LLM_PROFILES: Final[str] = "text_llm_profiles"
    # JSON list of user-taught rule strings appended to the cleanup prompt
    TRANSCRIPT_CLEANUP_RULES: Final[str] = "transcript_cleanup_rules"
    MINIMIZE_TRAY: Final[str] = "minimize_tray"
    STREAMING_ENABLED: Final[str] = "streaming_enabled"
    STREAMING_CHUNK_DURATION: Final[str] = "streaming_chunk_duration"
    STREAMING_OVERLAY_FONT_SIZE: Final[str] = "streaming_overlay_font_size"
    # Legacy keys kept for reading/migrating older settings files
    STREAMING_OVERLAY_ENABLED: Final[str] = "streaming_overlay_enabled"
    STREAMING_PASTE_ENABLED: Final[str] = "streaming_paste_enabled"
    WHISPER_MODEL: Final[str] = "whisper_model"
    WHISPER_DEVICE: Final[str] = "whisper_device"
    WHISPER_COMPUTE_TYPE: Final[str] = "whisper_compute_type"
    HF_ACCESS_POLICY: Final[str] = "hf_access_policy"
    # Legacy boolean replaced by HF_ACCESS_POLICY; kept for migration only.
    HF_HUB_OFFLINE: Final[str] = "hf_hub_offline"
    LAST_TAB_INDEX: Final[str] = "last_tab_index"
    DEVELOPER_MODE: Final[str] = "developer_mode"
    # Recording retention: "keep_all" or "custom" (+ max_saved_recordings count)
    RECORDING_RETENTION_MODE: Final[str] = "recording_retention_mode"
    MAX_SAVED_RECORDINGS: Final[str] = "max_saved_recordings"
    # Record hotkey activation: "toggle" or "push_hold"
    RECORDING_TRIGGER_MODE: Final[str] = "recording_trigger_mode"
    CONFIRM_HISTORY_ENTRY_DELETE: Final[str] = "confirm_history_entry_delete"
    CONFIRM_MEETING_DELETE: Final[str] = "confirm_meeting_delete"
    # Meeting Mode
    MEETING_WHISPER_MODEL: Final[str] = "meeting_whisper_model"
    MEETING_LANGUAGE: Final[str] = "meeting_language"
    MEETING_LLM_PROVIDER: Final[str] = "meeting_llm_provider"
    MEETING_LLM_MODEL: Final[str] = "meeting_llm_model"
    MEETING_AGENT_CORE: Final[str] = "meeting_agent_core"
    MEETING_END_REDECODE: Final[str] = "meeting_end_redecode"
    MEETING_END_POLISH: Final[str] = "meeting_end_polish"
    MEETING_END_REPORT: Final[str] = "meeting_end_report"
    MEETING_REPORT_RIBBON: Final[str] = "meeting_report_ribbon"
    MEETING_REPORT_BRIEF: Final[str] = "meeting_report_brief"
    MEETING_REPORT_SIGNAL: Final[str] = "meeting_report_signal"
    MEETING_CLOUD_CONSENT_GIVEN: Final[str] = "meeting_cloud_consent_given"
    MEETING_CLOUD_LAST_ENABLED: Final[str] = "meeting_cloud_last_enabled"
    MEETING_SPEAKER_ID_BACKEND: Final[str] = "meeting_speaker_id_backend"
    MEETING_AUDIO_UPLOAD_CONSENT_GIVEN: Final[str] = (
        "meeting_audio_upload_consent_given"
    )
    MEETING_UNSUPPORTED_PLATFORM_ACK: Final[str] = (
        "meeting_unsupported_platform_ack"
    )
    MEETING_MODE_INTRO_SEEN: Final[str] = "meeting_mode_intro_seen"
    MEETING_PAST_RECALL_ENABLED: Final[str] = "meeting_past_recall_enabled"
    MEETING_CONTEXT_FOLDER_ENABLED: Final[str] = (
        "meeting_context_folder_enabled"
    )
    MEETING_CONTEXT_FOLDER_PATH: Final[str] = "meeting_context_folder_path"
    MEETING_SERVER_BIND: Final[str] = "meeting_server_bind"
    MEETING_SERVER_PORT: Final[str] = "meeting_server_port"
    # In-app updater. Absent keys mean both automatic check and notify are on.
    UPDATE_CHECK_ENABLED: Final[str] = "update_check_enabled"
    UPDATE_NOTIFY_ENABLED: Final[str] = "update_notify_enabled"
    UPDATE_LAST_CHECK_AT: Final[str] = "update_last_check_at"
    UPDATE_SKIPPED_VERSION: Final[str] = "update_skipped_version"


class RecordingRetentionMode:
    """Values for ``SettingsKey.RECORDING_RETENTION_MODE``."""
    KEEP_ALL: Final[str] = "keep_all"
    CUSTOM: Final[str] = "custom"


class RecordingTriggerMode:
    """Values for ``SettingsKey.RECORDING_TRIGGER_MODE``."""
    TOGGLE: Final[str] = "toggle"
    PUSH_HOLD: Final[str] = "push_hold"

    ALL: Final[Tuple[str, ...]] = (TOGGLE, PUSH_HOLD)


class TranscriptCleanupProvider:
    """Built-in values for ``SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER``.

    Custom OpenAI-compatible endpoints use ``custom_…`` profile ids stored in
    ``SettingsKey.TEXT_LLM_PROFILES``. Resolvers accept either a built-in id
    or a known custom profile id.
    """
    OPENAI: Final[str] = "openai"
    OPENROUTER: Final[str] = "openrouter"

    ALL: Final[Tuple[str, ...]] = (OPENAI, OPENROUTER)


class TranscriptCleanupModelSort:
    """Values for ``SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT``.

    "alphabetical" sorts the fetched model list client-side (A-Z). Every
    other value maps directly to the OpenRouter ``GET /models`` ``sort``
    query parameter and preserves the server's ranking. OpenAI's models
    endpoint has no server-side sort, so OpenAI always uses alphabetical.
    """
    ALPHABETICAL: Final[str] = "alphabetical"
    MOST_POPULAR: Final[str] = "most-popular"
    TOP_WEEKLY: Final[str] = "top-weekly"
    NEWEST: Final[str] = "newest"
    PRICING_LOW_TO_HIGH: Final[str] = "pricing-low-to-high"
    PRICING_HIGH_TO_LOW: Final[str] = "pricing-high-to-low"
    CONTEXT_HIGH_TO_LOW: Final[str] = "context-high-to-low"
    THROUGHPUT_HIGH_TO_LOW: Final[str] = "throughput-high-to-low"
    LATENCY_LOW_TO_HIGH: Final[str] = "latency-low-to-high"

    ALL: Final[Tuple[str, ...]] = (
        ALPHABETICAL,
        MOST_POPULAR,
        TOP_WEEKLY,
        NEWEST,
        PRICING_LOW_TO_HIGH,
        PRICING_HIGH_TO_LOW,
        CONTEXT_HIGH_TO_LOW,
        THROUGHPUT_HIGH_TO_LOW,
        LATENCY_LOW_TO_HIGH,
    )


class TranscriptCleanupReasoning:
    """Values for ``SettingsKey.TRANSCRIPT_CLEANUP_REASONING``.

    "off" sends a plain temperature-0 request; the other levels request the
    provider's reasoning/thinking effort (only meaningful on reasoning models).
    """
    OFF: Final[str] = "off"
    LOW: Final[str] = "low"
    MEDIUM: Final[str] = "medium"
    HIGH: Final[str] = "high"

    ALL: Final[Tuple[str, ...]] = (OFF, LOW, MEDIUM, HIGH)


class MeetingAgentCore:
    """Values for ``SettingsKey.MEETING_AGENT_CORE``."""
    PI: Final[str] = "pi"          # Bundled Node sidecar running the Pi SDK
    DIRECT: Final[str] = "direct"  # Direct OpenRouter tool-calling loop

    ALL: Final[Tuple[str, ...]] = (PI, DIRECT)


class MeetingSpeakerIdBackend:
    """Values for ``SettingsKey.MEETING_SPEAKER_ID_BACKEND``."""
    LOCAL: Final[str] = "local"    # On-device WeSpeaker clustering
    OPENAI: Final[str] = "openai"  # Post-meeting gpt-4o-transcribe-diarize

    ALL: Final[Tuple[str, ...]] = (LOCAL, OPENAI)


class MeetingLanguage:
    """Spoken-language choices exposed by Meeting Mode settings."""

    AUTO: Final[str] = "auto"
    CHOICES: Final[Tuple[Tuple[str, str], ...]] = (
        (AUTO, "Detect automatically"),
        ("en", "English"),
        ("es", "Spanish"),
        ("fr", "French"),
        ("de", "German"),
        ("it", "Italian"),
        ("pt", "Portuguese"),
        ("nl", "Dutch"),
        ("pl", "Polish"),
        ("ru", "Russian"),
        ("uk", "Ukrainian"),
        ("tr", "Turkish"),
        ("ar", "Arabic"),
        ("he", "Hebrew"),
        ("hi", "Hindi"),
        ("zh", "Chinese"),
        ("ja", "Japanese"),
        ("ko", "Korean"),
        ("vi", "Vietnamese"),
        ("th", "Thai"),
        ("id", "Indonesian"),
        ("sv", "Swedish"),
        ("da", "Danish"),
        ("no", "Norwegian"),
        ("fi", "Finnish"),
        ("cs", "Czech"),
        ("el", "Greek"),
        ("ro", "Romanian"),
        ("hu", "Hungarian"),
    )
    ALL: Final[Tuple[str, ...]] = (
        "auto", "en", "es", "fr", "de", "it", "pt", "nl", "pl",
        "ru", "uk", "tr", "ar", "he", "hi", "zh", "ja", "ko",
        "vi", "th", "id", "sv", "da", "no", "fi", "cs", "el",
        "ro", "hu",
    )


class MeetingServerBind:
    """Values for ``SettingsKey.MEETING_SERVER_BIND``."""
    LOCALHOST: Final[str] = "localhost"  # Dashboard reachable on this machine only
    LAN: Final[str] = "lan"              # Explicitly shared on the local network

    ALL: Final[Tuple[str, ...]] = (LOCALHOST, LAN)


class HuggingFaceAccessPolicy:
    """Values for ``SettingsKey.HF_ACCESS_POLICY``.

    Cached models always load locally regardless of policy; the policy only
    governs whether Hugging Face may be contacted to download a missing model.
    """
    ASK: Final[str] = "ask"          # Prompt before downloading a missing model
    ALWAYS: Final[str] = "always"    # Download missing models without prompting
    NEVER: Final[str] = "never"      # Stay offline unless explicitly overridden once

    ALL: Final[Tuple[str, ...]] = (ASK, ALWAYS, NEVER)


_HF_HUB_OFFLINE_ENV: Final[str] = "HF_HUB_OFFLINE"
_HF_HUB_OFFLINE_TRUTHY: Final[Tuple[str, ...]] = ("1", "on", "true", "yes")


class SettingsManager:
    """Handles loading and saving application settings."""

    def __init__(self, settings_file: str = None):
        self.settings_file = settings_file or config.SETTINGS_FILE
        self._lock = threading.Lock()

    def load_all_settings(self) -> Dict[str, Any]:
        """Load settings, returning an empty dict on failure."""
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load all settings: {e}")

        return {}

    def save_all_settings(self, settings: Dict[str, Any]) -> None:
        """Persist the complete settings mapping."""
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(settings, f, indent=2)
            logger.info("All settings saved successfully")
        except Exception as e:
            logger.error(f"Failed to save all settings: {e}")
            raise

    def get(self, key: str, default: Any = None) -> Any:
        return self.load_all_settings().get(key, default)

    def save_setting(self, key: str, value: Any) -> None:
        try:
            settings = self.load_all_settings()
            settings[key] = value
            self.save_all_settings(settings)
            logger.debug(f"Setting saved: {key}={value}")
        except Exception as e:
            logger.error(f"Failed to save setting '{key}': {e}")
            raise

    def load_hotkey_settings(self) -> Dict[str, str]:
        """Load saved hotkeys or platform defaults."""
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r') as f:
                    settings = json.load(f)
                    return settings.get(SettingsKey.HOTKEYS, config.DEFAULT_HOTKEYS)
        except Exception as e:
            logger.warning(f"Failed to load settings: {e}")

        return config.DEFAULT_HOTKEYS.copy()

    def save_hotkey_settings(self, hotkeys: Dict[str, str]) -> None:
        try:
            settings = self.load_all_settings()
            settings[SettingsKey.HOTKEYS] = hotkeys
            with open(self.settings_file, 'w') as f:
                json.dump(settings, f, indent=2)
            logger.info("Hotkey settings saved successfully")
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")
            raise

    def load_model_selection(self) -> str:
        """Load a valid backend selection or the default."""
        try:
            selected_model = self.get(SettingsKey.SELECTED_MODEL)
            if selected_model and selected_model in config.MODEL_VALUE_MAP.values():
                return selected_model
        except Exception as e:
            logger.warning(f"Failed to load model selection: {e}")

        return config.MODEL_VALUE_MAP[config.MODEL_CHOICES[0]]

    def save_model_selection(self, model_value: str) -> None:
        """Validate and save a backend selection."""
        if not isinstance(model_value, str) or not model_value:
            raise ValueError("model_value must be a non-empty string")

        if model_value not in config.MODEL_VALUE_MAP.values():
            valid_models = list(config.MODEL_VALUE_MAP.values())
            raise ValueError(f"Invalid model '{model_value}'. Valid models: {valid_models}")

        try:
            self.save_setting(SettingsKey.SELECTED_MODEL, model_value)
            logger.info(f"Model selection saved: {model_value}")
        except Exception as e:
            logger.error(f"Failed to save model selection: {e}")
            raise

    def load_hf_access_policy(self) -> str:
        """Load the Hugging Face access policy, migrating the legacy setting.

        Legacy migration: ``hf_hub_offline: true`` becomes ``never``; ``false``
        or absent becomes ``ask`` (including existing installations). When a
        legacy key or an invalid policy value is found, the migrated policy is
        persisted and the legacy key removed.

        Returns:
            One of ``HuggingFaceAccessPolicy.ALL`` (defaults to ``ask``).
        """
        settings = self.load_all_settings()
        policy = settings.get(SettingsKey.HF_ACCESS_POLICY)
        if policy in HuggingFaceAccessPolicy.ALL:
            return policy

        legacy = settings.get(SettingsKey.HF_HUB_OFFLINE)
        migrated = (
            HuggingFaceAccessPolicy.NEVER if legacy
            else HuggingFaceAccessPolicy.ASK
        )
        if SettingsKey.HF_HUB_OFFLINE in settings or policy is not None:
            try:
                settings[SettingsKey.HF_ACCESS_POLICY] = migrated
                settings.pop(SettingsKey.HF_HUB_OFFLINE, None)
                self.save_all_settings(settings)
                logger.info(f"Migrated HuggingFace access policy to '{migrated}'")
            except Exception as e:
                logger.warning(f"Failed to persist HF policy migration: {e}")
        return migrated

    def save_hf_access_policy(self, policy: str) -> None:
        """Validate and persist the Hugging Face access policy."""
        if policy not in HuggingFaceAccessPolicy.ALL:
            raise ValueError(
                f"Invalid HF access policy '{policy}'. "
                f"Valid values: {list(HuggingFaceAccessPolicy.ALL)}"
            )
        settings = self.load_all_settings()
        settings[SettingsKey.HF_ACCESS_POLICY] = policy
        settings.pop(SettingsKey.HF_HUB_OFFLINE, None)
        self.save_all_settings(settings)
        logger.info(f"HuggingFace access policy saved: {policy}")

    def load_audio_input_device(self) -> Optional[int]:
        """Load the device ID, or None for the system default."""
        try:
            device_id = self.get(SettingsKey.AUDIO_INPUT_DEVICE)
            if device_id is not None and isinstance(device_id, int):
                return device_id
        except Exception as e:
            logger.warning(f"Failed to load audio input device: {e}")
        return None


def is_hf_hub_offline_env_set() -> bool:
    """Return whether ``HF_HUB_OFFLINE`` is set in the process env.

    An externally supplied ``HF_HUB_OFFLINE=1`` is a hard override: model
    downloads are disabled regardless of the persisted access policy.
    """
    return os.environ.get(_HF_HUB_OFFLINE_ENV, "").strip().lower() in _HF_HUB_OFFLINE_TRUTHY


settings_manager = SettingsManager()


def resolve_max_saved_recordings(
    settings: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Return a positive retention count, or None to keep all."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    mode = settings.get(
        SettingsKey.RECORDING_RETENTION_MODE,
        RecordingRetentionMode.CUSTOM,
    )
    if mode == RecordingRetentionMode.KEEP_ALL:
        return None

    raw = settings.get(SettingsKey.MAX_SAVED_RECORDINGS, config.MAX_SAVED_RECORDINGS)
    try:
        count = int(raw)
    except (TypeError, ValueError):
        count = config.MAX_SAVED_RECORDINGS
    return max(1, count)


def resolve_recording_trigger_mode(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return a valid record hotkey activation mode."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    mode = settings.get(SettingsKey.RECORDING_TRIGGER_MODE)
    if mode in RecordingTriggerMode.ALL:
        return mode
    return config.RECORDING_TRIGGER_MODE


def resolve_streaming_overlay_font_size(
    settings: Optional[Dict[str, Any]] = None,
) -> int:
    """Return the preview font size clamped to 10–48 points."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    raw = settings.get(
        SettingsKey.STREAMING_OVERLAY_FONT_SIZE,
        config.STREAMING_OVERLAY_FONT_SIZE,
    )
    try:
        size = int(raw)
    except (TypeError, ValueError):
        size = config.STREAMING_OVERLAY_FONT_SIZE
    return max(10, min(48, size))


def resolve_transcript_cleanup_prompt(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return a non-empty cleanup prompt, falling back to the built-in."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    prompt = settings.get(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT)
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()
    return config.TRANSCRIPT_CLEANUP_PROMPT


def _known_text_llm_profile_ids(
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[str, ...]:
    try:
        from services.text_llm import known_profile_ids

        return known_profile_ids(settings)
    except Exception:
        return TranscriptCleanupProvider.ALL


def default_transcript_cleanup_model(provider: str) -> str:
    """Return the provider default; custom endpoints have no default."""
    try:
        from services.text_llm import default_model_for_profile, get_profile

        profile = get_profile(provider)
        if profile is not None:
            return default_model_for_profile(profile)
    except Exception:
        pass
    if provider == TranscriptCleanupProvider.OPENROUTER:
        return config.TRANSCRIPT_CLEANUP_OPENROUTER_MODEL
    return config.TRANSCRIPT_CLEANUP_MODEL


def _resolve_text_llm_assignment(
    provider_key: str,
    model_key: str,
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Return a known provider/model pair, or the OpenRouter fallback.

    The last chosen assignment is kept only when both halves are present
    and the provider is still a known profile. A leftover model after a
    deleted custom endpoint is discarded with the missing provider.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()

    provider = settings.get(provider_key)
    model = settings.get(model_key)
    known = _known_text_llm_profile_ids(settings)
    if (
        isinstance(provider, str)
        and provider in known
        and isinstance(model, str)
        and model.strip()
    ):
        return provider, model.strip()
    return (
        TranscriptCleanupProvider.OPENROUTER,
        config.TRANSCRIPT_CLEANUP_OPENROUTER_MODEL,
    )


def resolve_transcript_cleanup_provider(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return a known cleanup profile ID or the configured default."""
    provider, _model = _resolve_text_llm_assignment(
        SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER,
        SettingsKey.TRANSCRIPT_CLEANUP_MODEL,
        settings,
    )
    return provider


def resolve_transcript_cleanup_model(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the last chosen cleanup model, or the OpenRouter fallback."""
    _provider, model = _resolve_text_llm_assignment(
        SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER,
        SettingsKey.TRANSCRIPT_CLEANUP_MODEL,
        settings,
    )
    return model


def resolve_transcript_cleanup_reasoning(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return a valid cleanup reasoning level."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    reasoning = settings.get(SettingsKey.TRANSCRIPT_CLEANUP_REASONING)
    if reasoning in TranscriptCleanupReasoning.ALL:
        return reasoning
    return config.TRANSCRIPT_CLEANUP_REASONING


def resolve_transcript_cleanup_rules(
    settings: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Return the validated list of learned cleanup rules.

    Non-list values yield an empty list; non-string and blank entries are
    dropped, remaining entries are stripped, and the list is capped at
    ``config.MAX_TRANSCRIPT_CLEANUP_RULES``.

    """
    if settings is None:
        settings = settings_manager.load_all_settings()

    raw = settings.get(SettingsKey.TRANSCRIPT_CLEANUP_RULES)
    if not isinstance(raw, list):
        return []
    rules = [r.strip() for r in raw if isinstance(r, str) and r.strip()]
    return rules[: config.MAX_TRANSCRIPT_CLEANUP_RULES]


def resolve_meeting_whisper_model(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return a valid Meeting Mode Whisper model."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    model = settings.get(SettingsKey.MEETING_WHISPER_MODEL)
    if isinstance(model, str) and model in config.WHISPER_MODEL_CHOICES:
        return model
    return config.MEETING_WHISPER_MODEL


def resolve_meeting_language(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return ``auto`` or a supported Whisper language code."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    language = settings.get(SettingsKey.MEETING_LANGUAGE)
    if isinstance(language, str):
        language = language.strip().lower()
        if language in MeetingLanguage.ALL:
            return language
    return config.MEETING_LANGUAGE


def resolve_meeting_llm_provider(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the validated LLM provider for meeting intelligence.

    Reuses the text-LLM profile vocabulary (built-in ``openai`` /
    ``openrouter`` plus custom ``custom_…`` ids) so API keys and base URLs
    resolve through the same plumbing.

    """
    provider, _model = _resolve_text_llm_assignment(
        SettingsKey.MEETING_LLM_PROVIDER,
        SettingsKey.MEETING_LLM_MODEL,
        settings,
    )
    return provider


def resolve_meeting_llm_profile(
    settings: Optional[Dict[str, Any]] = None,
):
    """Return the ``TextLLMProfile`` used for meeting intelligence."""
    from services.text_llm import builtin_profile, get_profile

    if settings is None:
        settings = settings_manager.load_all_settings()
    profile_id = resolve_meeting_llm_provider(settings)
    return get_profile(profile_id, settings) or builtin_profile(profile_id)


def resolve_meeting_llm_endpoint(
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a non-secret meeting endpoint snapshot dict."""
    from services.text_llm import (
        builtin_profiles,
        snapshot_from_profile,
    )

    profile = resolve_meeting_llm_profile(settings)
    if profile is None:
        profile = builtin_profiles()[1]
    return snapshot_from_profile(profile).to_dict()


def resolve_meeting_llm_model(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the last chosen meeting model, or the OpenRouter fallback."""
    _provider, model = _resolve_text_llm_assignment(
        SettingsKey.MEETING_LLM_PROVIDER,
        SettingsKey.MEETING_LLM_MODEL,
        settings,
    )
    return model


def resolve_meeting_agent_core(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return a valid meeting agent core kind."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    core = settings.get(SettingsKey.MEETING_AGENT_CORE)
    if core in MeetingAgentCore.ALL:
        return core
    return config.MEETING_AGENT_CORE


def resolve_meeting_speaker_id_backend(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return a valid speaker-identification backend."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    backend = settings.get(SettingsKey.MEETING_SPEAKER_ID_BACKEND)
    if backend in MeetingSpeakerIdBackend.ALL:
        return backend
    return config.MEETING_SPEAKER_ID_BACKEND


def resolve_meeting_audio_upload_consent(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether the user has approved uploading meeting audio."""
    return _resolve_bool_setting(
        settings, SettingsKey.MEETING_AUDIO_UPLOAD_CONSENT_GIVEN, False,
    )


def resolve_meeting_unsupported_platform_ack(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether the user acknowledged unsupported-platform Meeting Mode.

    Off by default. On macOS and Linux the Meeting Mode tab stays muted
    until this is granted once; later launches skip the warning.
    """
    return _resolve_bool_setting(
        settings, SettingsKey.MEETING_UNSUPPORTED_PLATFORM_ACK, False,
    )


def resolve_meeting_mode_intro_seen(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether the first-visit Meeting Mode intro was dismissed.

    Off by default. After Skip or Got it, later visits do not show it.
    """
    return _resolve_bool_setting(
        settings, SettingsKey.MEETING_MODE_INTRO_SEEN, False,
    )


def resolve_meeting_past_recall_enabled(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether meeting agents may search past transcripts.

    Off by default. When enabled, cloud intelligence may send excerpts from
    earlier meetings to the model. Distinct from cloud-intelligence consent,
    which covers only the current meeting.
    """
    return _resolve_bool_setting(
        settings, SettingsKey.MEETING_PAST_RECALL_ENABLED, False,
    )


def resolve_meeting_context_folder_enabled(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether meeting agents may search a local knowledge folder.

    Off by default. When enabled, cloud intelligence may send excerpts from
    files in the configured folder to the model. Distinct from both
    cloud-intelligence consent and past-meeting recall.
    """
    return _resolve_bool_setting(
        settings, SettingsKey.MEETING_CONTEXT_FOLDER_ENABLED, False,
    )


def resolve_meeting_context_folder_path(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the normalized knowledge-folder path, or empty when unset.

    Expands ``~`` and makes the path absolute. Does not require the folder
    to exist; the search module treats a missing directory as unavailable.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()
    raw = settings.get(SettingsKey.MEETING_CONTEXT_FOLDER_PATH, "")
    if not isinstance(raw, str):
        return ""
    cleaned = raw.strip()
    if not cleaned:
        return ""
    expanded = os.path.expanduser(cleaned)
    if not os.path.isabs(expanded):
        expanded = os.path.abspath(expanded)
    return os.path.normpath(expanded)


def _resolve_bool_setting(
    settings: Optional[Dict[str, Any]],
    key: str,
    default: bool,
) -> bool:
    if settings is None:
        settings = settings_manager.load_all_settings()
    raw = settings.get(key, default)
    if isinstance(raw, bool):
        return raw
    return default


def resolve_developer_mode(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether developer tools (demo meeting, etc.) are unlocked."""
    return _resolve_bool_setting(
        settings, SettingsKey.DEVELOPER_MODE, config.DEVELOPER_MODE,
    )


def resolve_update_check_enabled(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether background GitHub update checks are allowed."""
    return _resolve_bool_setting(
        settings, SettingsKey.UPDATE_CHECK_ENABLED, config.UPDATE_CHECK_ENABLED,
    )


def resolve_update_notify_enabled(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether an update-available dialog may be shown automatically."""
    return _resolve_bool_setting(
        settings,
        SettingsKey.UPDATE_NOTIFY_ENABLED,
        config.UPDATE_NOTIFY_ENABLED,
    )


def resolve_update_skipped_version(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the release version the user dismissed with Later, if any."""
    if settings is None:
        settings = settings_manager.load_all_settings()
    raw = settings.get(SettingsKey.UPDATE_SKIPPED_VERSION, "")
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def resolve_meeting_end_redecode(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether End should re-decode session audio with longer pauses."""
    return _resolve_bool_setting(
        settings, SettingsKey.MEETING_END_REDECODE, config.MEETING_END_REDECODE,
    )


def resolve_meeting_end_polish(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether End should run the LLM transcript polish."""
    return _resolve_bool_setting(
        settings, SettingsKey.MEETING_END_POLISH, config.MEETING_END_POLISH,
    )


def resolve_meeting_end_report(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether End should run the sidecar final report."""
    return _resolve_bool_setting(
        settings, SettingsKey.MEETING_END_REPORT, config.MEETING_END_REPORT,
    )


DEFAULT_REPORT_VIEWS: Final[Tuple[str, ...]] = ("ribbon", "brief", "signal")


def resolve_meeting_report_ribbon(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether the Ribbon report view is enabled."""
    return _resolve_bool_setting(
        settings, SettingsKey.MEETING_REPORT_RIBBON, config.MEETING_REPORT_RIBBON,
    )


def resolve_meeting_report_brief(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether the Brief report view is enabled."""
    return _resolve_bool_setting(
        settings, SettingsKey.MEETING_REPORT_BRIEF, config.MEETING_REPORT_BRIEF,
    )


def resolve_meeting_report_signal(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether the Signal report view is enabled."""
    return _resolve_bool_setting(
        settings, SettingsKey.MEETING_REPORT_SIGNAL, config.MEETING_REPORT_SIGNAL,
    )


def resolve_meeting_report_views(
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[str, ...]:
    """Return the enabled post-meeting report views, in display order.

    Falls back to ``("ribbon",)`` when every view is off so a meeting never
    ends with an empty report.
    """
    views = tuple(
        name for name, key, default in (
            ("ribbon", SettingsKey.MEETING_REPORT_RIBBON, config.MEETING_REPORT_RIBBON),
            ("brief", SettingsKey.MEETING_REPORT_BRIEF, config.MEETING_REPORT_BRIEF),
            ("signal", SettingsKey.MEETING_REPORT_SIGNAL, config.MEETING_REPORT_SIGNAL),
        )
        if _resolve_bool_setting(settings, key, default)
    )
    return views or ("ribbon",)


def resolve_meeting_server_bind(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return a valid dashboard bind mode."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    bind = settings.get(SettingsKey.MEETING_SERVER_BIND)
    if bind in MeetingServerBind.ALL:
        return bind
    return config.MEETING_SERVER_BIND


def resolve_meeting_server_port(
    settings: Optional[Dict[str, Any]] = None,
) -> int:
    """Return a dashboard port clamped to 0–65535."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    raw = settings.get(SettingsKey.MEETING_SERVER_PORT, config.MEETING_SERVER_PORT)
    try:
        port = int(raw)
    except (TypeError, ValueError):
        port = config.MEETING_SERVER_PORT
    return max(0, min(65535, port))


def compose_transcript_cleanup_prompt(base_prompt: str, rules: List[str]) -> str:
    """Append validated learned rules to the base prompt."""
    if not rules:
        return base_prompt
    numbered = "\n".join(f"{i}. {rule}" for i, rule in enumerate(rules, start=1))
    return (
        f"{base_prompt}\n\n"
        f"Additional user-taught rules (always apply):\n{numbered}"
    )
