"""
Settings management for the OpenWhisper application.
"""
import json
import os
import logging
import threading
from typing import Dict, Any, Final, List, Tuple, Optional
from config import config

logger = logging.getLogger(__name__)


class SettingsKey:
    """String keys used in the settings JSON file. Avoids magic strings at call sites."""
    HOTKEYS: Final[str] = "hotkeys"
    SELECTED_MODEL: Final[str] = "selected_model"
    AUDIO_INPUT_DEVICE: Final[str] = "audio_input_device"
    CURRENT_WAVEFORM_STYLE: Final[str] = "current_waveform_style"
    WAVEFORM_STYLE_CONFIGS: Final[str] = "waveform_style_configs"
    WINDOW_GEOMETRY: Final[str] = "window_geometry"
    COMPACT_WINDOW_GEOMETRY: Final[str] = "compact_window_geometry"
    COMPACT_MODE: Final[str] = "compact_mode"
    STREAMING_OVERLAY_POSITION: Final[str] = "streaming_overlay_position"
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
    CONFIRM_HISTORY_ENTRY_DELETE: Final[str] = "confirm_history_entry_delete"
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
    MEETING_PAST_RECALL_ENABLED: Final[str] = "meeting_past_recall_enabled"
    MEETING_CONTEXT_FOLDER_ENABLED: Final[str] = (
        "meeting_context_folder_enabled"
    )
    MEETING_CONTEXT_FOLDER_PATH: Final[str] = "meeting_context_folder_path"
    MEETING_SERVER_BIND: Final[str] = "meeting_server_bind"
    MEETING_SERVER_PORT: Final[str] = "meeting_server_port"


class RecordingRetentionMode:
    """Values for ``SettingsKey.RECORDING_RETENTION_MODE``."""
    KEEP_ALL: Final[str] = "keep_all"
    CUSTOM: Final[str] = "custom"


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
        """Initialize the settings manager.

        Args:
            settings_file: Path to settings file. Uses config default if None.
        """
        self.settings_file = settings_file or config.SETTINGS_FILE
        self._lock = threading.Lock()

    def load_all_settings(self) -> Dict[str, Any]:
        """Load all settings from file.

        Returns:
            Dictionary containing all settings, or empty dict on error.
        """
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load all settings: {e}")

        return {}

    def save_all_settings(self, settings: Dict[str, Any]) -> None:
        """Save all settings to file.

        Args:
            settings: Dictionary of all settings to save.

        Raises:
            Exception: If saving fails.
        """
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(settings, f, indent=2)
            logger.info("All settings saved successfully")
        except Exception as e:
            logger.error(f"Failed to save all settings: {e}")
            raise

    def get(self, key: str, default: Any = None) -> Any:
        """Read a single value from settings, with a default.

        Args:
            key: Setting key to read.
            default: Value to return when the key is missing.

        Returns:
            The stored value, or ``default`` if the key is absent or the file
            cannot be read.
        """
        return self.load_all_settings().get(key, default)

    def save_setting(self, key: str, value: Any) -> None:
        """Save a single setting value.

        Args:
            key: Setting key to save.
            value: Value to save for the key.

        Raises:
            Exception: If saving fails.
        """
        try:
            settings = self.load_all_settings()
            settings[key] = value
            self.save_all_settings(settings)
            logger.debug(f"Setting saved: {key}={value}")
        except Exception as e:
            logger.error(f"Failed to save setting '{key}': {e}")
            raise

    def load_hotkey_settings(self) -> Dict[str, str]:
        """Load hotkey settings from file, return defaults if file doesn't exist.

        Returns:
            Dictionary of hotkey mappings.
        """
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r') as f:
                    settings = json.load(f)
                    return settings.get(SettingsKey.HOTKEYS, config.DEFAULT_HOTKEYS)
        except Exception as e:
            logger.warning(f"Failed to load settings: {e}")

        return config.DEFAULT_HOTKEYS.copy()

    def save_hotkey_settings(self, hotkeys: Dict[str, str]) -> None:
        """Save hotkey settings to file.

        Args:
            hotkeys: Dictionary of hotkey mappings to save.

        Raises:
            Exception: If saving fails.
        """
        try:
            settings = self.load_all_settings()
            settings[SettingsKey.HOTKEYS] = hotkeys
            with open(self.settings_file, 'w') as f:
                json.dump(settings, f, indent=2)
            logger.info("Hotkey settings saved successfully")
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")
            raise

    def load_waveform_style_settings(self) -> Tuple[str, Dict[str, Dict]]:
        """Load waveform style settings from file.

        Returns:
            Tuple containing (current_style, all_style_configs).
            Falls back to defaults if file doesn't exist or is corrupted.
        """
        with self._lock:
            try:
                if os.path.exists(self.settings_file):
                    with open(self.settings_file, 'r') as f:
                        settings = json.load(f)

                    current_style = settings.get(SettingsKey.CURRENT_WAVEFORM_STYLE, config.CURRENT_WAVEFORM_STYLE)
                    saved_configs = settings.get(SettingsKey.WAVEFORM_STYLE_CONFIGS, {})

                    all_configs = config.WAVEFORM_STYLE_CONFIGS.copy()
                    for style_name, saved_config in saved_configs.items():
                        if style_name in all_configs and isinstance(saved_config, dict):
                            all_configs[style_name].update(saved_config)

                    if current_style not in all_configs:
                        logger.warning(f"Invalid current style '{current_style}', falling back to default")
                        current_style = config.CURRENT_WAVEFORM_STYLE

                    return current_style, all_configs

            except Exception as e:
                logger.warning(f"Failed to load waveform style settings: {e}")

            return config.CURRENT_WAVEFORM_STYLE, config.WAVEFORM_STYLE_CONFIGS.copy()

    def load_model_selection(self) -> str:
        """Load the saved model selection.

        Returns:
            The saved model selection internal value, or default if not found.
        """
        try:
            selected_model = self.get(SettingsKey.SELECTED_MODEL)
            if selected_model and selected_model in config.MODEL_VALUE_MAP.values():
                return selected_model
        except Exception as e:
            logger.warning(f"Failed to load model selection: {e}")

        return config.MODEL_VALUE_MAP[config.MODEL_CHOICES[0]]

    def save_model_selection(self, model_value: str) -> None:
        """Save the current model selection.

        Args:
            model_value: The internal model value to save (e.g., 'local_whisper')

        Raises:
            ValueError: If model_value is invalid
            Exception: If saving fails
        """
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
        """Persist the Hugging Face access policy.

        Args:
            policy: One of ``HuggingFaceAccessPolicy.ALL``.

        Raises:
            ValueError: If ``policy`` is not a recognized value.
            Exception: If saving fails.
        """
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
        """Load the saved audio input device ID.

        Returns:
            The saved device ID, or None to use system default.
        """
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


# Global settings manager instance
settings_manager = SettingsManager()


def resolve_max_saved_recordings(
    settings: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Return how many saved recordings to keep, or ``None`` for unlimited.

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        Positive int when retention mode is custom, or ``None`` to keep all.
    """
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


def resolve_streaming_overlay_font_size(
    settings: Optional[Dict[str, Any]] = None,
) -> int:
    """Return the live streaming preview font size in points.

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        Font size clamped to a sensible range for the near-cursor overlay.
    """
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
    """Return the system prompt used for post-ASR transcript cleanup.

    Empty or missing values fall back to the built-in default prompt.

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        Non-empty cleanup system prompt string.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()

    prompt = settings.get(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT)
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()
    return config.TRANSCRIPT_CLEANUP_PROMPT


def _known_text_llm_profile_ids(
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[str, ...]:
    """Return built-in plus currently saved custom text-LLM profile ids."""
    try:
        from services.text_llm import known_profile_ids

        return known_profile_ids(settings)
    except Exception:
        return TranscriptCleanupProvider.ALL


def default_transcript_cleanup_model(provider: str) -> str:
    """Return the built-in default cleanup model for a provider.

    Args:
        provider: A built-in or custom text-LLM profile id.

    Returns:
        Default chat model id for that provider. Custom endpoints have no
        built-in default and return an empty string.
    """
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


def resolve_transcript_cleanup_provider(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the validated provider used for post-ASR transcript cleanup.

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        A built-in or custom text-LLM profile id, falling back to the config
        default when the stored value is missing or unknown.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()

    provider = settings.get(SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER)
    if isinstance(provider, str) and provider in _known_text_llm_profile_ids(
        settings
    ):
        return provider
    return config.TRANSCRIPT_CLEANUP_PROVIDER


def resolve_transcript_cleanup_model(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the chat model id used for post-ASR transcript cleanup.

    Empty or missing values fall back to the provider's default model.

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        Non-empty model id string.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()

    model = settings.get(SettingsKey.TRANSCRIPT_CLEANUP_MODEL)
    if isinstance(model, str) and model.strip():
        return model.strip()
    return default_transcript_cleanup_model(
        resolve_transcript_cleanup_provider(settings)
    )


def resolve_transcript_cleanup_model_sort(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the validated model-list sort order for Model Manager's Text tab.

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        A ``TranscriptCleanupModelSort`` value, falling back to the config
        default when the stored value is missing or unknown.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()

    sort = settings.get(SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT)
    if sort in TranscriptCleanupModelSort.ALL:
        return sort
    return config.TRANSCRIPT_CLEANUP_MODEL_SORT


def resolve_transcript_cleanup_reasoning(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the validated reasoning level for post-ASR transcript cleanup.

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        A ``TranscriptCleanupReasoning`` value, falling back to the config
        default when the stored value is missing or unknown.
    """
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

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        List of non-empty rule strings (possibly empty).
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
    """Return the validated Whisper model used by Meeting Mode ASR.

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        A ``config.WHISPER_MODEL_CHOICES`` value, falling back to the config
        default when the stored value is missing or unknown.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()

    model = settings.get(SettingsKey.MEETING_WHISPER_MODEL)
    if isinstance(model, str) and model in config.WHISPER_MODEL_CHOICES:
        return model
    return config.MEETING_WHISPER_MODEL


def resolve_meeting_language(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the validated Meeting Mode spoken-language preference.

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        ``"auto"`` or a supported ISO-639-1 Whisper language code.
    """
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

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        A text-LLM profile id, falling back to the config default when the
        stored value is missing or unknown.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()

    provider = settings.get(SettingsKey.MEETING_LLM_PROVIDER)
    if isinstance(provider, str) and provider in _known_text_llm_profile_ids(
        settings
    ):
        return provider
    return config.MEETING_LLM_PROVIDER


def resolve_transcript_cleanup_profile(
    settings: Optional[Dict[str, Any]] = None,
):
    """Return the ``TextLLMProfile`` used for post-ASR transcript cleanup."""
    from services.text_llm import builtin_profile, get_profile

    if settings is None:
        settings = settings_manager.load_all_settings()
    profile_id = resolve_transcript_cleanup_provider(settings)
    return get_profile(profile_id, settings) or builtin_profile(profile_id)


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
    """Return the chat model id used for meeting intelligence.

    Empty or missing values fall back to the config default.

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        Non-empty model id string.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()

    model = settings.get(SettingsKey.MEETING_LLM_MODEL)
    if isinstance(model, str) and model.strip():
        return model.strip()
    return config.MEETING_LLM_MODEL


def resolve_meeting_agent_core(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the validated meeting agent core kind.

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        A ``MeetingAgentCore`` value, falling back to the config default when
        the stored value is missing or unknown.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()

    core = settings.get(SettingsKey.MEETING_AGENT_CORE)
    if core in MeetingAgentCore.ALL:
        return core
    return config.MEETING_AGENT_CORE


def resolve_meeting_speaker_id_backend(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the validated post-meeting speaker-identification backend.

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        A ``MeetingSpeakerIdBackend`` value, falling back to the config
        default when the stored value is missing or unknown.
    """
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
    """Return the validated dashboard server bind mode.

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        A ``MeetingServerBind`` value, falling back to the config default when
        the stored value is missing or unknown.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()

    bind = settings.get(SettingsKey.MEETING_SERVER_BIND)
    if bind in MeetingServerBind.ALL:
        return bind
    return config.MEETING_SERVER_BIND


def resolve_meeting_server_port(
    settings: Optional[Dict[str, Any]] = None,
) -> int:
    """Return the validated dashboard server port.

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        Port clamped to 0-65535 (0 requests an ephemeral port), falling back
        to the config default on non-numeric values.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()

    raw = settings.get(SettingsKey.MEETING_SERVER_PORT, config.MEETING_SERVER_PORT)
    try:
        port = int(raw)
    except (TypeError, ValueError):
        port = config.MEETING_SERVER_PORT
    return max(0, min(65535, port))


def compose_transcript_cleanup_prompt(base_prompt: str, rules: List[str]) -> str:
    """Append numbered learned rules to the base cleanup prompt.

    Args:
        base_prompt: The base cleanup system prompt.
        rules: Learned rule strings (already validated).

    Returns:
        ``base_prompt`` unchanged when ``rules`` is empty; otherwise the base
        prompt followed by a numbered "user-taught rules" section.
    """
    if not rules:
        return base_prompt
    numbered = "\n".join(f"{i}. {rule}" for i, rule in enumerate(rules, start=1))
    return (
        f"{base_prompt}\n\n"
        f"Additional user-taught rules (always apply):\n{numbered}"
    )
