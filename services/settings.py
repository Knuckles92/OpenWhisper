"""Persistent application settings and validated resolvers."""
import json
from copy import deepcopy
import os
import logging
import tempfile
import threading
from datetime import date
from types import MappingProxyType
from typing import Callable, Dict, Any, Final, List, Mapping, Tuple, Optional, TypeVar
from config import config
from services import openai_retirement
from services.batch_upload import BatchRelation

logger = logging.getLogger(__name__)
SettingsMutationResult = TypeVar("SettingsMutationResult")

LEGACY_API_MODELS = {
    "api_whisper": "whisper-1",
    "api_gpt4o": "gpt-4o-transcribe",
    "api_gpt4o_mini": "gpt-4o-mini-transcribe",
}


def api_model_choices(today: Optional[date] = None) -> Tuple[str, ...]:
    """API transcription models OpenAI still serves, in display order."""
    if not openai_retirement.retired(today):
        return config.API_MODEL_CHOICES
    return tuple(
        model for model in config.API_MODEL_CHOICES
        if model not in openai_retirement.RETIRING_TRANSCRIPTION_MODELS
    )


def api_model_label(model: str, today: Optional[date] = None) -> str:
    """Picker text for an API model; retiring ones carry the shutdown date."""
    if (
        model in openai_retirement.RETIRING_TRANSCRIPTION_MODELS
        and not openai_retirement.retired(today)
    ):
        return f"{model} (retiring {openai_retirement.SHUTDOWN_LABEL})"
    return model


def serving_api_model(model: str, today: Optional[date] = None) -> str:
    """``model`` while OpenAI serves it, otherwise the default API model."""
    if model in api_model_choices(today):
        return model
    return config.DEFAULT_API_MODEL


def resolve_api_transcription_model(
    settings: dict[str, Any], today: Optional[date] = None,
) -> str:
    model = settings.get(SettingsKey.API_TRANSCRIPTION_MODEL)
    if isinstance(model, str) and model in config.API_MODEL_CHOICES:
        return serving_api_model(model, today)
    legacy = settings.get(SettingsKey.SELECTED_MODEL)
    if isinstance(legacy, str) and legacy in LEGACY_API_MODELS:
        return serving_api_model(LEGACY_API_MODELS[legacy], today)
    return SETTING_DEFAULTS[SettingsKey.API_TRANSCRIPTION_MODEL]


# Bump whenever the Linux preview disclosure changes meaning. Persisting the
# version instead of a boolean prevents an older acknowledgement from silently
# authorizing newly added capture behavior.
MEETING_LINUX_PREVIEW_ACK_VERSION: Final[int] = 1


class SettingsKey:
    """Keys persisted in the settings JSON file."""
    HOTKEYS: Final[str] = "hotkeys"
    SELECTED_MODEL: Final[str] = "selected_model"
    API_TRANSCRIPTION_MODEL: Final[str] = "api_transcription_model"
    AUDIO_INPUT_DEVICE: Final[str] = "audio_input_device"
    WINDOW_GEOMETRY: Final[str] = "window_geometry"
    COMPACT_WINDOW_GEOMETRY: Final[str] = "compact_window_geometry"
    COMPACT_MODE: Final[str] = "compact_mode"
    # View → Host Mode: the main window shows the host dashboard instead of
    # the recording tabs, with its own window position and size.
    HOST_MODE: Final[str] = "host_mode"
    HOST_WINDOW_GEOMETRY: Final[str] = "host_window_geometry"
    AUTO_PASTE: Final[str] = "auto_paste"
    MACOS_ACCESSIBILITY_INTRO_SEEN: Final[str] = "macos_accessibility_intro_seen"
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
    TRANSCRIPT_CLEANUP_PROFILES: Final[str] = "transcript_cleanup_profiles"
    QUICK_RECORD_PROFILE: Final[str] = "quick_record_profile"
    # Multi-file upload: last relation preset (BatchRelation) and the Custom
    # preset's description and combine choice
    TRANSCRIPT_BATCH_RELATION: Final[str] = "transcript_batch_relation"
    TRANSCRIPT_BATCH_CUSTOM_INSTRUCTIONS: Final[str] = (
        "transcript_batch_custom_instructions"
    )
    TRANSCRIPT_BATCH_CUSTOM_COMBINE: Final[str] = "transcript_batch_custom_combine"
    MINIMIZE_TRAY: Final[str] = "minimize_tray"
    STREAMING_ENABLED: Final[str] = "streaming_enabled"
    STREAMING_CHUNK_DURATION: Final[str] = "streaming_chunk_duration"
    STREAMING_OVERLAY_FONT_SIZE: Final[str] = "streaming_overlay_font_size"
    # Application chrome type size as a percent of the designed theme (90–130).
    UI_FONT_SCALE: Final[str] = "ui_font_scale"
    # Colour theme: "dark", "light", or "system" (follow the OS setting).
    UI_THEME: Final[str] = "ui_theme"
    # Legacy keys kept for reading/migrating older settings files
    STREAMING_OVERLAY_ENABLED: Final[str] = "streaming_overlay_enabled"
    STREAMING_PASTE_ENABLED: Final[str] = "streaming_paste_enabled"
    LOCAL_ASR_MODELS: Final[str] = "local_asr_models"
    LOCAL_ASR_DEVICES: Final[str] = "local_asr_devices"
    LOCAL_ASR_LANGUAGE: Final[str] = "local_asr_language"
    MEETING_ASR_MODEL: Final[str] = "meeting_asr_model"
    MEETING_ASR_SOURCE: Final[str] = "meeting_asr_source"  # local | remote
    WHISPER_MODEL: Final[str] = "whisper_model"
    CUSTOM_WHISPER_MODELS: Final[str] = "custom_whisper_models"
    WHISPER_DEVICE: Final[str] = "whisper_device"
    WHISPER_COMPUTE_TYPE: Final[str] = "whisper_compute_type"
    # "Keep using the CPU" on the "Use this GPU" offer; it isn't shown again.
    WHISPER_GPU_OFFER_DECLINED: Final[str] = "whisper_gpu_offer_declined"
    HF_ACCESS_POLICY: Final[str] = "hf_access_policy"
    # Legacy boolean replaced by HF_ACCESS_POLICY; kept for migration only.
    HF_HUB_OFFLINE: Final[str] = "hf_hub_offline"
    LAST_TAB_INDEX: Final[str] = "last_tab_index"
    DEVELOPER_MODE: Final[str] = "developer_mode"
    # Recording retention: "keep_all", "custom" (+ max_saved_recordings count),
    # or "size_limit" (+ max_saved_recordings_mb folder size)
    RECORDING_RETENTION_MODE: Final[str] = "recording_retention_mode"
    MAX_SAVED_RECORDINGS: Final[str] = "max_saved_recordings"
    MAX_SAVED_RECORDINGS_MB: Final[str] = "max_saved_recordings_mb"
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
    #: ``{agent_id: model}`` for installed agents; "" keeps the agent's default.
    MEETING_AGENT_MODELS: Final[str] = "meeting_agent_models"
    MEETING_END_REDECODE: Final[str] = "meeting_end_redecode"
    MEETING_REDECODE_COVERAGE_GUARD: Final[str] = "meeting_redecode_coverage_guard"
    MEETING_END_POLISH: Final[str] = "meeting_end_polish"
    MEETING_INSIGHT_REVIEW: Final[str] = "meeting_insight_review"
    MEETING_INSIGHT_REVIEW_CONSENT: Final[str] = "meeting_insight_review_consent"
    MEETING_INSIGHT_REVIEW_SENSITIVITY: Final[str] = "meeting_insight_review_sensitivity"
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
    MEETING_LINUX_PREVIEW_ACK_VERSION: Final[str] = (
        "meeting_linux_preview_ack_version"
    )
    MEETING_MODE_INTRO_SEEN: Final[str] = "meeting_mode_intro_seen"
    MEETING_PAST_RECALL_ENABLED: Final[str] = "meeting_past_recall_enabled"
    MEETING_CONTEXT_FOLDER_ENABLED: Final[str] = (
        "meeting_context_folder_enabled"
    )
    MEETING_CONTEXT_FOLDER_PATH: Final[str] = "meeting_context_folder_path"
    MEETING_SERVER_BIND: Final[str] = "meeting_server_bind"
    MEETING_SERVER_PORT: Final[str] = "meeting_server_port"
    MCP_ENABLED: Final[str] = "mcp_enabled"
    MCP_PORT: Final[str] = "mcp_port"
    MCP_TAILSCALE_ENABLED: Final[str] = "mcp_tailscale_enabled"
    MCP_RETITLE_TRANSCRIPTIONS: Final[str] = "mcp_retitle_transcriptions"
    MCP_RETITLE_MEETINGS: Final[str] = "mcp_retitle_meetings"
    MCP_SETTINGS_ACCESS: Final[str] = "mcp_settings_access"
    MCP_WRITABLE_SETTINGS: Final[str] = "mcp_writable_settings"
    # Remote engine (services/remote_asr). Client: the paired host's address,
    # pinned certificate fingerprint and name (the token is in the OS
    # credential store). Host: sharing switch, port, and paired devices,
    # stored as token digests.
    REMOTE_ENGINE_CLIENT: Final[str] = "remote_engine_client"
    REMOTE_HOST_ENABLED: Final[str] = "remote_host_enabled"
    # Explicit host opt-in for catalog access and model downloads by paired devices.
    REMOTE_HOST_MODEL_MANAGEMENT: Final[str] = "remote_host_model_management"
    REMOTE_HOST_PORT: Final[str] = "remote_host_port"
    REMOTE_HOST_DEVICES: Final[str] = "remote_host_devices"
    # Let computers signed in to the same Tailscale account pair without a code.
    REMOTE_HOST_TAILSCALE_TRUST: Final[str] = "remote_host_tailscale_trust"
    # Host opt-in: keep paired computers' records (history, meetings) here.
    REMOTE_HOST_KEEP_RECORDS: Final[str] = "remote_host_keep_records"
    # Client: where this computer's records are kept while paired:
    # "local" (default), "host" (moved there), or "both" (copied there).
    REMOTE_RECORDS_LOCATION: Final[str] = "remote_records_location"
    REMOTE_CLIENT_HISTORY: Final[str] = "remote_client_history"
    # TypeSafe fast judgments. The master switch gates every remote judgment;
    # each feature has its own switch so one can be trialled at a time.
    TYPESAFE_ENABLED: Final[str] = "typesafe_enabled"
    TYPESAFE_CITATIONS_ENABLED: Final[str] = "typesafe_citations_enabled"
    TYPESAFE_SEMANTIC_SEARCH_ENABLED: Final[str] = "typesafe_semantic_search_enabled"
    TYPESAFE_QUESTION_RADAR_ENABLED: Final[str] = "typesafe_question_radar_enabled"
    TYPESAFE_HIGHLIGHTS_ENABLED: Final[str] = "typesafe_highlights_enabled"
    TYPESAFE_TOPIC_SHIFT_ENABLED: Final[str] = "typesafe_topic_shift_enabled"
    TYPESAFE_VOICE_COMMANDS_ENABLED: Final[str] = (
        "typesafe_voice_commands_enabled"
    )
    TYPESAFE_VOICE_COMMAND_NAMES: Final[str] = "typesafe_voice_command_names"
    # In-app updater. Absent keys mean both automatic check and notify are on.
    UPDATE_CHECK_ENABLED: Final[str] = "update_check_enabled"
    UPDATE_NOTIFY_ENABLED: Final[str] = "update_notify_enabled"
    UPDATE_LAST_CHECK_AT: Final[str] = "update_last_check_at"
    UPDATE_SKIPPED_VERSION: Final[str] = "update_skipped_version"


#: Keys the single live-preview toggle replaced. Dropped whenever that toggle
#: is written, so an old settings file cannot resurrect the split switches.
LEGACY_STREAMING_KEYS: Final[tuple[str, ...]] = (
    SettingsKey.STREAMING_OVERLAY_ENABLED,
    SettingsKey.STREAMING_PASTE_ENABLED,
    "streaming_tiny_model_enabled",
    "live_typing_enabled",
)


class RecordingRetentionMode:
    """Values for ``SettingsKey.RECORDING_RETENTION_MODE``."""
    KEEP_ALL: Final[str] = "keep_all"
    # Keep the newest ``MAX_SAVED_RECORDINGS`` files.
    CUSTOM: Final[str] = "custom"
    # Keep the newest files that fit in ``MAX_SAVED_RECORDINGS_MB``.
    SIZE_LIMIT: Final[str] = "size_limit"


class RecordingTriggerMode:
    """Values for ``SettingsKey.RECORDING_TRIGGER_MODE``."""
    TOGGLE: Final[str] = "toggle"
    PUSH_HOLD: Final[str] = "push_hold"

    ALL: Final[Tuple[str, ...]] = (TOGGLE, PUSH_HOLD)


class UiFontScale:
    """Values for ``SettingsKey.UI_FONT_SCALE``.

    Percents of the designed theme. Labels are what Settings shows.
    """
    SMALL: Final[int] = 90
    DEFAULT: Final[int] = 100
    LARGE: Final[int] = 115
    EXTRA_LARGE: Final[int] = 130

    ALL: Final[Tuple[int, ...]] = (SMALL, DEFAULT, LARGE, EXTRA_LARGE)
    LABELS: Final[Dict[int, str]] = {
        SMALL: "Small",
        DEFAULT: "Default",
        LARGE: "Large",
        EXTRA_LARGE: "Extra large",
    }


class UiTheme:
    """Values for ``SettingsKey.UI_THEME``. Labels are what Settings shows."""
    DARK: Final[str] = "dark"
    LIGHT: Final[str] = "light"
    SYSTEM: Final[str] = "system"
    OMARCHY: Final[str] = "omarchy"

    ALL: Final[Tuple[str, ...]] = (DARK, LIGHT, SYSTEM, OMARCHY)
    LABELS: Final[Dict[str, str]] = {
        DARK: "Dark",
        LIGHT: "Light",
        SYSTEM: "Match system",
        OMARCHY: "Omarchy desktop",
    }


class TranscriptCleanupProvider:
    """Built-in values for ``SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER``.

    Custom OpenAI-compatible endpoints use ``custom_…`` profile ids stored in
    ``SettingsKey.TEXT_LLM_PROFILES``. Resolvers accept either a built-in id
    or a known custom profile id.
    """
    OPENAI: Final[str] = "openai"
    OPENROUTER: Final[str] = "openrouter"

    OLLAMA: Final[str] = "ollama"
    GROQ: Final[str] = "groq"
    OPENCODE_GO: Final[str] = "opencode_go"
    OPENCODE_ZEN: Final[str] = "opencode_zen"

    ALL: Final[Tuple[str, ...]] = (OPENAI, OPENROUTER, OLLAMA, GROQ, OPENCODE_GO, OPENCODE_ZEN)


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
    """Values for ``SettingsKey.MEETING_AGENT_CORE``.

    ``PI`` and ``OPENCODE`` use an agent SDK with OpenWhisper's text endpoint and API key.
    The retired ``DIRECT`` value is read only for compatibility and resolves to Pi.
    The ``INSTALLED`` values drive a coding agent the user already has set up,
    with its own sign-in, providers, and models.
    """
    PI: Final[str] = "pi"          # Bundled Node sidecar running the Pi SDK
    DIRECT: Final[str] = "direct"  # Legacy saved value; migrated to Pi, never selectable
    CLAUDE_CODE: Final[str] = "claude_code"  # Installed Claude Code, headless
    CODEX: Final[str] = "codex"              # Installed Codex CLI, headless
    OPENCODE: Final[str] = "opencode"      # Packaged OpenCode SDK; preserves saved settings
    OPENCODE_CLI: Final[str] = "opencode_cli"  # Installed OpenCode, over ACP

    INSTALLED: Final[Tuple[str, ...]] = (CLAUDE_CODE, CODEX, OPENCODE_CLI)
    ALL: Final[Tuple[str, ...]] = (PI, OPENCODE, *INSTALLED)


class MeetingSpeakerIdBackend:
    """Values for ``SettingsKey.MEETING_SPEAKER_ID_BACKEND``."""
    OFF: Final[str] = "off"        # Me / Others channel labels only
    LOCAL: Final[str] = "local"    # On-device WeSpeaker clustering
    OPENAI: Final[str] = "openai"  # Post-meeting gpt-4o-transcribe-diarize, until 2027-02-26

    ALL: Final[Tuple[str, ...]] = (OFF, LOCAL, OPENAI)


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
    ALL: Final[Tuple[str, ...]] = tuple(code for code, _label in CHOICES)


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


#: The default of every scalar setting, taken from ``config`` wherever config
#: defines it. Resolvers fall back to these, and call sites read them through
#: :func:`setting_value` instead of repeating literals.
SETTING_DEFAULTS: Final[Mapping[str, Any]] = MappingProxyType({
    # Dictation and window
    SettingsKey.SELECTED_MODEL: config.DEFAULT_BACKEND,
    SettingsKey.API_TRANSCRIPTION_MODEL: config.DEFAULT_API_MODEL,
    SettingsKey.AUTO_PASTE: True,
    SettingsKey.COPY_CLIPBOARD: True,
    SettingsKey.MACOS_ACCESSIBILITY_INTRO_SEEN: False,
    SettingsKey.COMPACT_MODE: False,
    SettingsKey.HOST_MODE: False,
    SettingsKey.MINIMIZE_TRAY: True,
    SettingsKey.RECORDING_TRIGGER_MODE: config.RECORDING_TRIGGER_MODE,
    SettingsKey.STREAMING_ENABLED: config.STREAMING_ENABLED,
    SettingsKey.STREAMING_CHUNK_DURATION: config.STREAMING_CHUNK_DURATION_SEC,
    SettingsKey.STREAMING_OVERLAY_FONT_SIZE: config.STREAMING_OVERLAY_FONT_SIZE,
    SettingsKey.UI_FONT_SCALE: config.UI_FONT_SCALE,
    SettingsKey.UI_THEME: config.UI_THEME,
    SettingsKey.QUICK_RECORD_PROFILE: "",
    SettingsKey.DEVELOPER_MODE: config.DEVELOPER_MODE,
    # Local engines
    SettingsKey.WHISPER_MODEL: config.DEFAULT_WHISPER_MODEL,
    SettingsKey.CUSTOM_WHISPER_MODELS: [],
    SettingsKey.WHISPER_DEVICE: config.FASTER_WHISPER_DEVICE,
    SettingsKey.WHISPER_COMPUTE_TYPE: config.FASTER_WHISPER_COMPUTE_TYPE,
    SettingsKey.WHISPER_GPU_OFFER_DECLINED: False,
    SettingsKey.LOCAL_ASR_LANGUAGE: "en",
    # Transcript cleanup and uploads. With no saved choice, cleanup uses
    # OpenRouter's free router; config.TRANSCRIPT_CLEANUP_MODEL is the OpenAI
    # profile's default model, not this setting's.
    SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: config.TRANSCRIPT_CLEANUP_ENABLED,
    SettingsKey.TRANSCRIPT_CLEANUP_PROMPT: config.TRANSCRIPT_CLEANUP_PROMPT,
    SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: config.TRANSCRIPT_CLEANUP_PROVIDER,
    SettingsKey.TRANSCRIPT_CLEANUP_MODEL: config.TRANSCRIPT_CLEANUP_OPENROUTER_MODEL,
    SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT: config.TRANSCRIPT_CLEANUP_MODEL_SORT,
    SettingsKey.TRANSCRIPT_CLEANUP_REASONING: config.TRANSCRIPT_CLEANUP_REASONING,
    SettingsKey.TRANSCRIPT_BATCH_RELATION: config.TRANSCRIPT_BATCH_RELATION,
    SettingsKey.TRANSCRIPT_BATCH_CUSTOM_COMBINE: config.TRANSCRIPT_BATCH_CUSTOM_COMBINE,
    # History and recordings
    SettingsKey.RECORDING_RETENTION_MODE: RecordingRetentionMode.CUSTOM,
    SettingsKey.MAX_SAVED_RECORDINGS: config.MAX_SAVED_RECORDINGS,
    SettingsKey.MAX_SAVED_RECORDINGS_MB: config.MAX_SAVED_RECORDINGS_MB,
    SettingsKey.CONFIRM_HISTORY_ENTRY_DELETE: True,
    SettingsKey.CONFIRM_MEETING_DELETE: True,
    # Meeting Mode
    SettingsKey.MEETING_WHISPER_MODEL: config.MEETING_WHISPER_MODEL,
    SettingsKey.MEETING_LANGUAGE: config.MEETING_LANGUAGE,
    SettingsKey.MEETING_LLM_PROVIDER: TranscriptCleanupProvider.OPENROUTER,
    SettingsKey.MEETING_LLM_MODEL: config.MEETING_LLM_MODEL,
    SettingsKey.MEETING_AGENT_CORE: config.MEETING_AGENT_CORE,
    SettingsKey.MEETING_AGENT_MODELS: {},
    SettingsKey.MEETING_SPEAKER_ID_BACKEND: config.MEETING_SPEAKER_ID_BACKEND,
    SettingsKey.MEETING_END_REDECODE: config.MEETING_END_REDECODE,
    SettingsKey.MEETING_REDECODE_COVERAGE_GUARD: False,
    SettingsKey.MEETING_END_POLISH: config.MEETING_END_POLISH,
    SettingsKey.MEETING_END_REPORT: config.MEETING_END_REPORT,
    SettingsKey.MEETING_REPORT_RIBBON: config.MEETING_REPORT_RIBBON,
    SettingsKey.MEETING_REPORT_BRIEF: config.MEETING_REPORT_BRIEF,
    SettingsKey.MEETING_REPORT_SIGNAL: config.MEETING_REPORT_SIGNAL,
    SettingsKey.MEETING_INSIGHT_REVIEW: False,
    SettingsKey.MEETING_INSIGHT_REVIEW_SENSITIVITY: "normal",
    SettingsKey.MEETING_CLOUD_CONSENT_GIVEN: False,
    SettingsKey.MEETING_CLOUD_LAST_ENABLED: False,
    SettingsKey.MEETING_AUDIO_UPLOAD_CONSENT_GIVEN: False,
    SettingsKey.MEETING_UNSUPPORTED_PLATFORM_ACK: False,
    SettingsKey.MEETING_MODE_INTRO_SEEN: False,
    SettingsKey.MEETING_PAST_RECALL_ENABLED: False,
    SettingsKey.MEETING_CONTEXT_FOLDER_ENABLED: False,
    SettingsKey.MEETING_CONTEXT_FOLDER_PATH: "",
    SettingsKey.MEETING_SERVER_BIND: config.MEETING_SERVER_BIND,
    SettingsKey.MEETING_SERVER_PORT: config.MEETING_SERVER_PORT,
    # TypeSafe: every feature is off until chosen, except topic shifts,
    # which only apply once TypeSafe itself is on.
    SettingsKey.TYPESAFE_ENABLED: False,
    SettingsKey.TYPESAFE_CITATIONS_ENABLED: False,
    SettingsKey.TYPESAFE_SEMANTIC_SEARCH_ENABLED: False,
    SettingsKey.TYPESAFE_QUESTION_RADAR_ENABLED: False,
    SettingsKey.TYPESAFE_HIGHLIGHTS_ENABLED: False,
    SettingsKey.TYPESAFE_TOPIC_SHIFT_ENABLED: True,
    SettingsKey.TYPESAFE_VOICE_COMMANDS_ENABLED: False,
    # In-app updater
    SettingsKey.UPDATE_CHECK_ENABLED: config.UPDATE_CHECK_ENABLED,
    SettingsKey.UPDATE_NOTIFY_ENABLED: config.UPDATE_NOTIFY_ENABLED,
    SettingsKey.UPDATE_SKIPPED_VERSION: "",
})


_HF_HUB_OFFLINE_ENV: Final[str] = "HF_HUB_OFFLINE"
_HF_HUB_OFFLINE_TRUTHY: Final[Tuple[str, ...]] = ("1", "on", "true", "yes")


class SettingsManager:
    """Handles loading and saving application settings."""

    def __init__(self, settings_file: str = None):
        self.settings_file = settings_file or config.SETTINGS_FILE
        # A setting mutation is a read-modify-write transaction.  Several UI
        # and background workers share this singleton, so the lock must cover
        # the complete transaction rather than only the final write.
        self._lock = threading.RLock()
        self._cached_signature = None
        self._cached_settings = None

    def _file_signature(self):
        """Detect changed paths, in-place edits, and atomic external replaces."""
        path = os.path.abspath(self.settings_file)
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            return (path, None)
        except OSError:
            return None
        return (path, stat.st_dev, stat.st_ino, stat.st_size,
                stat.st_mtime_ns, stat.st_ctime_ns)

    def _load_all_settings_unlocked(self, *, strict: bool = False) -> Dict[str, Any]:
        """Read the settings mapping while the caller owns ``_lock``.

        ``strict`` is used by read-modify-write operations: a malformed file
        must not be mistaken for an empty mapping and then overwritten with a
        single new preference.
        """
        if not os.path.exists(self.settings_file):
            return {}
        try:
            with open(self.settings_file, 'r', encoding='utf-8') as handle:
                settings = json.load(handle)
            if not isinstance(settings, dict):
                raise ValueError("settings root must be a JSON object")
            return settings
        except Exception as exc:
            logger.warning(f"Failed to load all settings: {exc}")
            if strict:
                raise
            return {}

    def _save_all_settings_unlocked(self, settings: Dict[str, Any]) -> None:
        """Atomically replace the settings file while ``_lock`` is held."""
        if not isinstance(settings, dict):
            raise TypeError("settings must be a mapping")

        absolute_path = os.path.abspath(self.settings_file)
        directory = os.path.dirname(absolute_path) or os.curdir
        prefix = f".{os.path.basename(absolute_path)}."
        temp_fd = -1
        temp_path = ""
        try:
            temp_fd, temp_path = tempfile.mkstemp(
                prefix=prefix,
                suffix=".tmp",
                dir=directory,
            )
            with os.fdopen(temp_fd, 'w', encoding='utf-8') as handle:
                temp_fd = -1
                json.dump(settings, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, absolute_path)
            # Never label the caller's mutable mapping with a signature from
            # a concurrent external writer. The next read owns its snapshot.
            self._cached_settings = None
            self._cached_signature = None
            temp_path = ""
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            if temp_path:
                try:
                    os.remove(temp_path)
                except FileNotFoundError:
                    pass

    def load_all_settings(self) -> Dict[str, Any]:
        """Load settings, returning an empty dict on failure."""
        with self._lock:
            signature = self._file_signature()
            if (signature is not None and signature == self._cached_signature
                    and self._cached_settings is not None):
                return deepcopy(self._cached_settings)
            settings = self._load_all_settings_unlocked()
            if signature is not None and signature == self._file_signature():
                self._cached_signature = signature
                self._cached_settings = deepcopy(settings)
            else:
                self._cached_signature = None
                self._cached_settings = None
            return settings

    def save_all_settings(self, settings: Dict[str, Any]) -> None:
        """Persist the complete settings mapping."""
        try:
            with self._lock:
                self._save_all_settings_unlocked(settings)
            logger.info("All settings saved successfully")
        except Exception as e:
            logger.error(f"Failed to save all settings: {e}")
            raise

    def get(self, key: str, default: Any = None) -> Any:
        return self.load_all_settings().get(key, default)

    def update_settings(
        self,
        updates: Dict[str, Any],
        *,
        remove: Tuple[str, ...] = (),
    ) -> Dict[str, Any]:
        """Atomically merge keys into the persisted settings mapping.

        Returns a copy of the complete mapping that was committed.  Callers
        changing several related preferences should use this method instead of
        a separate ``load_all_settings`` / ``save_all_settings`` pair.
        """
        if not isinstance(updates, dict):
            raise TypeError("updates must be a mapping")
        def apply_updates(settings: Dict[str, Any]) -> Dict[str, Any]:
            settings.update(updates)
            for key in remove:
                settings.pop(key, None)
            return dict(settings)

        return self.mutate_settings(apply_updates)

    def mutate_settings(
        self,
        mutator: Callable[[Dict[str, Any]], SettingsMutationResult],
    ) -> SettingsMutationResult:
        """Atomically mutate settings with a caller-supplied transaction.

        This covers structured values whose helper functions edit a settings
        mapping in place, such as custom endpoint profiles.  The callback runs
        while the settings lock is held; if it raises, nothing is written.
        """
        if not callable(mutator):
            raise TypeError("mutator must be callable")
        with self._lock:
            settings = self._load_all_settings_unlocked(strict=True)
            result = mutator(settings)
            self._save_all_settings_unlocked(settings)
            return result

    def save_setting(self, key: str, value: Any) -> None:
        try:
            self.update_settings({key: value})
            logger.debug(f"Setting saved: {key}={value}")
        except Exception as e:
            logger.error(f"Failed to save setting '{key}': {e}")
            raise

    def load_hotkey_settings(self) -> Dict[str, str]:
        """Load saved hotkeys or platform defaults."""
        try:
            settings = self.load_all_settings()
            return settings.get(SettingsKey.HOTKEYS, config.DEFAULT_HOTKEYS)
        except Exception as e:
            logger.warning(f"Failed to load settings: {e}")

        return config.DEFAULT_HOTKEYS.copy()

    def save_hotkey_settings(self, hotkeys: Dict[str, str]) -> None:
        try:
            self.update_settings({SettingsKey.HOTKEYS: hotkeys})
            logger.info("Hotkey settings saved successfully")
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")
            raise

    def load_model_selection(self) -> str:
        """Load a valid backend selection or the default."""
        try:
            settings = self.load_all_settings()
            selected_model = settings.get(SettingsKey.SELECTED_MODEL)
            if isinstance(selected_model, str) and selected_model in LEGACY_API_MODELS:
                self.update_settings({
                    SettingsKey.SELECTED_MODEL: "api",
                    SettingsKey.API_TRANSCRIPTION_MODEL: resolve_api_transcription_model(settings),
                })
                return "api"
            if selected_model and selected_model in config.MODEL_VALUE_MAP.values():
                return selected_model
        except Exception as e:
            logger.warning(f"Failed to load model selection: {e}")

        return SETTING_DEFAULTS[SettingsKey.SELECTED_MODEL]

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
                self.update_settings(
                    {SettingsKey.HF_ACCESS_POLICY: migrated},
                    remove=(SettingsKey.HF_HUB_OFFLINE,),
                )
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
        self.update_settings(
            {SettingsKey.HF_ACCESS_POLICY: policy},
            remove=(SettingsKey.HF_HUB_OFFLINE,),
        )
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


def setting_value(key: str, settings: Optional[Mapping[str, Any]] = None) -> Any:
    """Return the stored value of ``key``, or its default when unset.

    Unvalidated, like ``settings.get(key, default)``; a resolver validates.
    ``settings`` is a loaded mapping; None reads the settings file.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()
    return settings.get(key, SETTING_DEFAULTS[key])


def resolve_bool_setting(
    key: str, settings: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Return ``key``'s stored bool, or its default when unset or not a bool."""
    raw = setting_value(key, settings)
    return raw if isinstance(raw, bool) else SETTING_DEFAULTS[key]


def resolve_choice_setting(
    key: str,
    choices: Tuple[Any, ...],
    settings: Optional[Mapping[str, Any]] = None,
) -> Any:
    """Return ``key``'s stored value when it is one of ``choices``, else its default."""
    raw = setting_value(key, settings)
    return raw if raw in choices else SETTING_DEFAULTS[key]


def _int_setting(key: str, settings: Optional[Mapping[str, Any]]) -> int:
    raw = setting_value(key, settings)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return SETTING_DEFAULTS[key]


def _bool_resolver(key: str) -> Callable[..., bool]:
    def resolve(settings: Optional[Mapping[str, Any]] = None) -> bool:
        return resolve_bool_setting(key, settings)

    resolve.__doc__ = (
        f"Return the ``{key}`` setting, or {SETTING_DEFAULTS[key]} when unset "
        "or not a bool."
    )
    return resolve


def _choice_resolver(key: str, choices: Tuple[Any, ...]) -> Callable[..., Any]:
    def resolve(settings: Optional[Mapping[str, Any]] = None) -> Any:
        return resolve_choice_setting(key, choices, settings)

    resolve.__doc__ = (
        f"Return the ``{key}`` setting when it is one of {choices}, else "
        f"{SETTING_DEFAULTS[key]!r}."
    )
    return resolve


# Settings with a fixed set of values: the stored one when valid, else the
# default. Each is called as ``resolve_x(settings=None)``.
resolve_recording_trigger_mode = _choice_resolver(
    SettingsKey.RECORDING_TRIGGER_MODE, RecordingTriggerMode.ALL)
def resolve_ui_theme(settings: Optional[Mapping[str, Any]] = None) -> str:
    from services.desktop_session import use_omarchy_ui

    if settings is None:
        settings = settings_manager.load_all_settings()
    if SettingsKey.UI_THEME not in settings and use_omarchy_ui():
        return UiTheme.OMARCHY
    return resolve_choice_setting(SettingsKey.UI_THEME, UiTheme.ALL, settings)


resolve_transcript_cleanup_reasoning = _choice_resolver(
    SettingsKey.TRANSCRIPT_CLEANUP_REASONING, TranscriptCleanupReasoning.ALL)
resolve_transcript_batch_relation = _choice_resolver(
    SettingsKey.TRANSCRIPT_BATCH_RELATION, BatchRelation.ALL)
resolve_meeting_agent_core = _choice_resolver(
    SettingsKey.MEETING_AGENT_CORE, MeetingAgentCore.ALL)
resolve_meeting_server_bind = _choice_resolver(
    SettingsKey.MEETING_SERVER_BIND, MeetingServerBind.ALL)

# On/off settings: the stored bool, else the default.
resolve_transcript_batch_custom_combine = _bool_resolver(
    SettingsKey.TRANSCRIPT_BATCH_CUSTOM_COMBINE)
resolve_developer_mode = _bool_resolver(SettingsKey.DEVELOPER_MODE)
resolve_update_check_enabled = _bool_resolver(SettingsKey.UPDATE_CHECK_ENABLED)
resolve_update_notify_enabled = _bool_resolver(SettingsKey.UPDATE_NOTIFY_ENABLED)
# Meeting consents and acknowledgements, off until given. On unsupported
# platforms (old macOS, unsupported Linux architectures, other OSes) the
# Meeting Mode tab stays muted until the ack is granted once; supported Linux
# uses the versioned resolve_meeting_linux_preview_ack instead.
resolve_meeting_audio_upload_consent = _bool_resolver(
    SettingsKey.MEETING_AUDIO_UPLOAD_CONSENT_GIVEN)
resolve_meeting_cloud_consent = _bool_resolver(SettingsKey.MEETING_CLOUD_CONSENT_GIVEN)
resolve_meeting_unsupported_platform_ack = _bool_resolver(
    SettingsKey.MEETING_UNSUPPORTED_PLATFORM_ACK)
resolve_meeting_mode_intro_seen = _bool_resolver(SettingsKey.MEETING_MODE_INTRO_SEEN)
# Off by default. Past recall lets AI insights send excerpts of earlier
# meetings to the model, and the context folder excerpts of files in that
# folder; each is separate from the current meeting's AI insights consent.
resolve_meeting_past_recall_enabled = _bool_resolver(
    SettingsKey.MEETING_PAST_RECALL_ENABLED)
resolve_meeting_context_folder_enabled = _bool_resolver(
    SettingsKey.MEETING_CONTEXT_FOLDER_ENABLED)
# End-of-meeting steps and report views. The coverage guard is an opt-in
# word-count safeguard for live and retried re-transcription.
resolve_meeting_end_redecode = _bool_resolver(SettingsKey.MEETING_END_REDECODE)
resolve_meeting_redecode_coverage_guard = _bool_resolver(
    SettingsKey.MEETING_REDECODE_COVERAGE_GUARD)
resolve_meeting_end_polish = _bool_resolver(SettingsKey.MEETING_END_POLISH)
resolve_meeting_end_report = _bool_resolver(SettingsKey.MEETING_END_REPORT)
resolve_meeting_report_ribbon = _bool_resolver(SettingsKey.MEETING_REPORT_RIBBON)
resolve_meeting_report_brief = _bool_resolver(SettingsKey.MEETING_REPORT_BRIEF)
resolve_meeting_report_signal = _bool_resolver(SettingsKey.MEETING_REPORT_SIGNAL)
# TypeSafe is a separate remote decision service: each judgment sends a short
# excerpt of text and gets a typed answer back, not generated text. Off by
# default; every TypeSafe feature also needs it, and meeting features need
# the meeting's cloud consent too.
resolve_typesafe_enabled = _bool_resolver(SettingsKey.TYPESAFE_ENABLED)


def resolve_max_saved_recordings(
    settings: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Return a positive retention count, or None when no count limit applies."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    mode = setting_value(SettingsKey.RECORDING_RETENTION_MODE, settings)
    if mode in (RecordingRetentionMode.KEEP_ALL, RecordingRetentionMode.SIZE_LIMIT):
        return None
    return max(1, _int_setting(SettingsKey.MAX_SAVED_RECORDINGS, settings))


def resolve_max_saved_recordings_bytes(
    settings: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Return the saved-recordings size cap in bytes, or None when no size limit applies."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    mode = settings.get(SettingsKey.RECORDING_RETENTION_MODE)
    if mode != RecordingRetentionMode.SIZE_LIMIT:
        return None

    megabytes = _int_setting(SettingsKey.MAX_SAVED_RECORDINGS_MB, settings)
    return max(1, megabytes) * 1024 * 1024


def resolve_ui_font_scale(
    settings: Optional[Dict[str, Any]] = None,
) -> int:
    """Return a valid UI font-scale percent, defaulting to 100."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    percent = _int_setting(SettingsKey.UI_FONT_SCALE, settings)
    if percent in UiFontScale.ALL:
        return percent
    return SETTING_DEFAULTS[SettingsKey.UI_FONT_SCALE]


def resolve_streaming_overlay_font_size(
    settings: Optional[Dict[str, Any]] = None,
) -> int:
    """Return the preview font size clamped to 10–48 points."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    size = _int_setting(SettingsKey.STREAMING_OVERLAY_FONT_SIZE, settings)
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
    return SETTING_DEFAULTS[SettingsKey.TRANSCRIPT_CLEANUP_PROMPT]


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
    return SETTING_DEFAULTS[provider_key], SETTING_DEFAULTS[model_key]


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


def resolve_transcript_batch_custom_instructions(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the remembered Custom description, stripped and capped."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    raw = settings.get(SettingsKey.TRANSCRIPT_BATCH_CUSTOM_INSTRUCTIONS)
    if not isinstance(raw, str):
        return ""
    return raw.strip()[: config.MAX_TRANSCRIPT_BATCH_INSTRUCTION_CHARS]


def resolve_meeting_asr_source(settings: Optional[Dict[str, Any]] = None) -> str:
    """Speech location is independent of the dictation engine and AI insights."""
    if settings is None:
        settings = settings_manager.load_all_settings()
    return "remote" if settings.get(SettingsKey.MEETING_ASR_SOURCE) == "remote" else "local"


def resolve_meeting_whisper_model(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return a valid Meeting Mode Whisper model."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    from services.local_asr.catalog import MODELS
    extra = settings.get(SettingsKey.MEETING_ASR_MODEL)
    if isinstance(extra, str) and extra in MODELS and MODELS[extra].meeting:
        return extra
    model = settings.get(SettingsKey.MEETING_WHISPER_MODEL)
    if isinstance(model, str):
        from services.whisper_sources import custom_models
        if model in [*config.WHISPER_MODEL_CHOICES, *custom_models(settings)]:
            return model
    return SETTING_DEFAULTS[SettingsKey.MEETING_WHISPER_MODEL]


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
    return SETTING_DEFAULTS[SettingsKey.MEETING_LANGUAGE]


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
    return snapshot_from_profile(profile, resolve_meeting_llm_model(settings)).to_dict()


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


def resolve_meeting_agent_models(
    settings: Optional[Mapping[str, Any]] = None,
) -> Dict[str, str]:
    """Return the model chosen for each installed agent, as a fresh dict.

    Missing agents and non-string values are dropped; an empty string means
    the agent keeps the default from its own configuration.
    """
    raw = setting_value(SettingsKey.MEETING_AGENT_MODELS, settings)
    if not isinstance(raw, dict):
        return {}
    return {
        agent: model.strip()
        for agent, model in raw.items()
        if agent in MeetingAgentCore.INSTALLED and isinstance(model, str)
    }


def resolve_meeting_agent_model(
    agent_id: str, settings: Optional[Mapping[str, Any]] = None,
) -> str:
    """Return the model chosen for ``agent_id``, or "" for its own default."""
    return resolve_meeting_agent_models(settings).get(agent_id, "")


def resolve_meeting_speaker_id_backend(
    settings: Optional[Dict[str, Any]] = None,
    today: Optional[date] = None,
) -> str:
    """Return a valid speaker-identification backend.

    OpenAI speaker identification becomes on-device once OpenAI retires
    gpt-4o-transcribe-diarize.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()

    backend = settings.get(SettingsKey.MEETING_SPEAKER_ID_BACKEND)
    if backend == MeetingSpeakerIdBackend.OPENAI and openai_retirement.retired(today):
        return MeetingSpeakerIdBackend.LOCAL
    if backend in MeetingSpeakerIdBackend.ALL:
        return backend
    return SETTING_DEFAULTS[SettingsKey.MEETING_SPEAKER_ID_BACKEND]


def resolve_meeting_linux_preview_ack(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether the current Linux Meeting Mode preview was accepted.

    Legacy booleans and stale/future versions deliberately fail closed. A
    changed disclosure must bump :data:`MEETING_LINUX_PREVIEW_ACK_VERSION` so
    users see it once before newly authorized capture behavior can start.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()
    value = settings.get(SettingsKey.MEETING_LINUX_PREVIEW_ACK_VERSION)
    return (
        type(value) is int
        and value == MEETING_LINUX_PREVIEW_ACK_VERSION
    )


#: Names participants can use to address the note-taking assistant. The
#: bare word "whisper" is deliberately absent: it is ordinary speech.
DEFAULT_VOICE_COMMAND_NAMES: Final[Tuple[str, ...]] = (
    "note taker", "notetaker", "assistant", "openwhisper", "open whisper",
)


def resolve_typesafe_feature_enabled(feature: str, settings=None) -> bool:
    keys = {
        "citations": SettingsKey.TYPESAFE_CITATIONS_ENABLED,
        "semantic_search": SettingsKey.TYPESAFE_SEMANTIC_SEARCH_ENABLED,
        "question_radar": SettingsKey.TYPESAFE_QUESTION_RADAR_ENABLED,
        "highlights": SettingsKey.TYPESAFE_HIGHLIGHTS_ENABLED,
    }
    return resolve_typesafe_enabled(settings) and resolve_bool_setting(
        keys[feature], settings,
    )


def resolve_typesafe_topic_shift_enabled(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether the checkpoint scheduler may ask TypeSafe about topic shifts.

    On by default once TypeSafe is enabled; the lexical Jaccard rule remains
    the fallback whenever a judgment is unavailable.
    """
    return resolve_typesafe_enabled(settings) and resolve_bool_setting(
        SettingsKey.TYPESAFE_TOPIC_SHIFT_ENABLED, settings,
    )


def resolve_typesafe_voice_commands_enabled(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether spoken instructions to the note taker are acted on.

    Off by default. Only segments that name the assistant are ever judged.
    """
    return resolve_typesafe_enabled(settings) and resolve_bool_setting(
        SettingsKey.TYPESAFE_VOICE_COMMANDS_ENABLED, settings,
    )


def resolve_typesafe_voice_command_names(
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[str, ...]:
    """Return the lowercased wake names for voice commands, defaults when unset."""
    if settings is None:
        settings = settings_manager.load_all_settings()
    raw = settings.get(SettingsKey.TYPESAFE_VOICE_COMMAND_NAMES)
    if not isinstance(raw, list):
        return DEFAULT_VOICE_COMMAND_NAMES
    names = tuple(
        " ".join(item.strip().lower().split())
        for item in raw
        if isinstance(item, str) and item.strip()
    )
    return names or DEFAULT_VOICE_COMMAND_NAMES


def resolve_meeting_context_folder_path(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the normalized knowledge-folder path, or empty when unset.

    Expands ``~`` and makes the path absolute. Does not require the folder
    to exist; the search module treats a missing directory as unavailable.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()
    raw = setting_value(SettingsKey.MEETING_CONTEXT_FOLDER_PATH, settings)
    if not isinstance(raw, str):
        return ""
    cleaned = raw.strip()
    if not cleaned:
        return ""
    expanded = os.path.expanduser(cleaned)
    if not os.path.isabs(expanded):
        expanded = os.path.abspath(expanded)
    return os.path.normpath(expanded)


def resolve_update_skipped_version(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the release version the user dismissed with Later, if any."""
    if settings is None:
        settings = settings_manager.load_all_settings()
    raw = setting_value(SettingsKey.UPDATE_SKIPPED_VERSION, settings)
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def resolve_meeting_report_views(
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[str, ...]:
    """Return the enabled post-meeting report views, in display order.

    Falls back to ``("ribbon",)`` when every view is off so a meeting never
    ends with an empty report.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()
    views = tuple(
        name for name, key in (
            ("ribbon", SettingsKey.MEETING_REPORT_RIBBON),
            ("brief", SettingsKey.MEETING_REPORT_BRIEF),
            ("signal", SettingsKey.MEETING_REPORT_SIGNAL),
        )
        if resolve_bool_setting(key, settings)
    )
    return views or ("ribbon",)


def resolve_meeting_server_port(
    settings: Optional[Dict[str, Any]] = None,
) -> int:
    """Return a dashboard port clamped to 0–65535."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    port = _int_setting(SettingsKey.MEETING_SERVER_PORT, settings)
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


def resolve_meeting_insight_review(settings=None):
    if settings is None:
        settings = settings_manager.load_all_settings()
    enabled = resolve_bool_setting(SettingsKey.MEETING_INSIGHT_REVIEW, settings)
    consent = settings.get(SettingsKey.MEETING_INSIGHT_REVIEW_CONSENT) == "typesafe-text-v1"
    sensitivity = resolve_choice_setting(
        SettingsKey.MEETING_INSIGHT_REVIEW_SENSITIVITY, ("normal", "thorough"), settings,
    )
    return {"enabled": enabled and consent and resolve_typesafe_enabled(settings), "consent": "typesafe-text-v1" if consent else "",
            "sensitivity": sensitivity}
