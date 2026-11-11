"""Persistent application settings and validated resolvers."""
import json
from copy import deepcopy
from dataclasses import dataclass
import os
import logging
import sys
import tempfile
import threading
from datetime import date
from types import MappingProxyType
from typing import Callable, Dict, Any, Final, Iterable, List, Mapping, Tuple, Optional, TypeVar
from config import config
from services import openai_retirement
from services.batch_upload import BatchRelation
from services.setting_types import (
    BoolSetting,
    ChoiceSetting,
    DictSetting,
    FloatSetting,
    IntChoiceSetting,
    IntSetting,
    ListSetting,
    OptionalDictSetting,
    OptionalListSetting,
    RawSetting,
    Setting,
    StrSetting,
)

logger = logging.getLogger(__name__)
SettingsMutationResult = TypeVar("SettingsMutationResult")

#: Fallbacks already reported at warning; repeats go to debug, because the
#: resolvers that hit them run on every settings read.
_reported_fallbacks: set[str] = set()


def _log_fallback(message: str) -> None:
    """Log the exception being handled: the first time at warning, then at debug."""
    if message in _reported_fallbacks:
        logger.debug(message, exc_info=True)
        return
    _reported_fallbacks.add(message)
    logger.warning(message, exc_info=True)

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
    settings: Mapping[str, Any], today: Optional[date] = None,
) -> str:
    model = SettingsKey.API_TRANSCRIPTION_MODEL.stored(settings)
    if model is not None and model in config.API_MODEL_CHOICES:
        return serving_api_model(model, today)
    # The raw selection: the typed read already maps these names to "api".
    legacy = settings.get(SettingsKey.SELECTED_MODEL)
    if isinstance(legacy, str) and legacy in LEGACY_API_MODELS:
        return serving_api_model(LEGACY_API_MODELS[legacy], today)
    return SettingsKey.API_TRANSCRIPTION_MODEL.default


# Bump whenever the Linux preview disclosure changes meaning. Persisting the
# version instead of a boolean prevents an older acknowledgement from silently
# authorizing newly added capture behavior.
MEETING_LINUX_PREVIEW_ACK_VERSION: Final[int] = 1


class RecordingRetentionMode:
    """Values for ``SettingsKey.RECORDING_RETENTION_MODE``."""
    KEEP_ALL: Final[str] = "keep_all"
    # Keep the newest ``MAX_SAVED_RECORDINGS`` files.
    CUSTOM: Final[str] = "custom"
    # Keep the newest files that fit in ``MAX_SAVED_RECORDINGS_MB``.
    SIZE_LIMIT: Final[str] = "size_limit"

    ALL: Final[Tuple[str, ...]] = (KEEP_ALL, CUSTOM, SIZE_LIMIT)


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


class WalkthroughCopyFormat:
    """Clipboard contents when a walkthrough finishes."""

    BOTH: Final[str] = "both"
    AGENT: Final[str] = "agent"  # Legacy Markdown copy preference.
    AGENT_TASK: Final[str] = "agent_task"
    PDF: Final[str] = "pdf"
    WORD: Final[str] = "word"

    ALL: Final[tuple[str, ...]] = (PDF, AGENT_TASK, BOTH, AGENT, WORD)
    LABELS: Final[dict[str, str]] = {
        PDF: "PDF copy",
        AGENT_TASK: "Agent (PDF + instructions)",
        BOTH: "PDF and Markdown copy",
        AGENT: "Markdown copy",
        WORD: "Word document copy",
    }


class WalkthroughSnapMode:
    """Values for ``SettingsKey.WALKTHROUGH_SNAP_MODE``. Labels are what the Snap menu shows."""

    REGION: Final[str] = "region"  # draw one on the frozen screen
    WINDOW: Final[str] = "window"  # the window under the pointer
    SCREEN: Final[str] = "screen"  # the whole screen under the pointer
    DELAYED: Final[str] = "delayed"  # draw a region on the screen as it is 3 seconds on

    ALL: Final[Tuple[str, ...]] = (REGION, WINDOW, SCREEN, DELAYED)
    LABELS: Final[Dict[str, str]] = {
        REGION: "Region",
        WINDOW: "Window under the pointer",
        SCREEN: "Whole screen",
        DELAYED: "In 3 seconds",
    }


class WalkthroughPageSize:
    """Values for ``SettingsKey.WALKTHROUGH_PAGE_SIZE``. Labels are what Settings shows."""
    AUTO: Final[str] = "auto"  # Letter where the locale measures in US units, else A4
    LETTER: Final[str] = "letter"
    A4: Final[str] = "a4"

    ALL: Final[Tuple[str, ...]] = (AUTO, LETTER, A4)
    LABELS: Final[Dict[str, str]] = {
        AUTO: "Automatic (Letter or A4 for your region)",
        LETTER: "Letter",
        A4: "A4",
    }


class RecordingOverlayLook:
    """Values for ``SettingsKey.RECORDING_OVERLAY_LOOK``; see ui_qt/overlays/recording_looks.py."""
    RIBBON: Final[str] = "ribbon"
    BARS: Final[str] = "bars"
    WAVE: Final[str] = "wave"
    AURORA: Final[str] = "aurora"
    PULSE: Final[str] = "pulse"
    DOTS: Final[str] = "dots"
    BAIT_BALL: Final[str] = "baitball"
    FISH: Final[str] = "fish"
    GLOW: Final[str] = "glow"
    FIZZ: Final[str] = "fizz"
    LEGACY: Final[str] = "legacy"

    ALL: Final[Tuple[str, ...]] = (RIBBON, BARS, WAVE, AURORA, PULSE, DOTS, BAIT_BALL, FISH, GLOW, FIZZ, LEGACY)
    LABELS: Final[Dict[str, str]] = {
        RIBBON: "Ribbon",
        BARS: "Bars",
        WAVE: "Mirror wave",
        AURORA: "Aurora",
        PULSE: "Pulse",
        DOTS: "Dots",
        BAIT_BALL: "Bait ball",
        FISH: "Real fish",
        GLOW: "Deep-sea glow",
        FIZZ: "Fizz",
        LEGACY: "Classic",
    }
    DETAILS: Final[Dict[str, str]] = {
        RIBBON: "A glowing trace of your voice",
        BARS: "A scrolling level meter",
        WAVE: "Your voice's outline, mirrored",
        AURORA: "Flowing waves",
        PULSE: "A ring that swells and ripples",
        DOTS: "Dots that ripple as you speak",
        BAIT_BALL: "A spinning school lifts the swell",
        FISH: "Fish school under the waves",
        GLOW: "Glowing fish in the deep",
        FIZZ: "Glasses filling with fizz",
        LEGACY: "The original particles",
    }
    #: Names saved by earlier builds, and what they mean now.
    ALIASES: Final[Dict[str, str]] = {"live": RIBBON, "orb": PULSE, "bubbles": BAIT_BALL}


class StreamingOverlayLayout:
    """Values for ``SettingsKey.STREAMING_OVERLAY_LAYOUT``: the overlay with preview words."""
    #: A slim row over the words: the live dot, the clock and a small copy of the look.
    HEADER: Final[str] = "header"
    #: The words on top and the full-size look under them, as before any words.
    FOOTER: Final[str] = "footer"

    ALL: Final[Tuple[str, ...]] = (HEADER, FOOTER)
    LABELS: Final[Dict[str, str]] = {HEADER: "Header", FOOTER: "Footer"}
    DETAILS: Final[Dict[str, str]] = {
        HEADER: "Clock and a small meter above the words",
        FOOTER: "The full look under the words",
    }


class TranscriptCleanupProvider:
    """Built-in values for ``SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER``.

    Custom OpenAI-compatible endpoints use ``custom_…`` profile ids stored in
    ``SettingsKey.TEXT_LLM_PROFILES``. Resolvers accept either a built-in id
    or a known custom profile id. Cleanup also accepts ``INSTALLED`` agent
    ids with an empty model to use the agent's default.
    """
    OPENAI: Final[str] = "openai"
    OPENROUTER: Final[str] = "openrouter"

    OLLAMA: Final[str] = "ollama"
    LM_STUDIO: Final[str] = "lm_studio"
    VLLM: Final[str] = "vllm"
    LLAMA_CPP: Final[str] = "llama_cpp"
    LOCALAI: Final[str] = "localai"
    GROQ: Final[str] = "groq"
    OPENCODE_GO: Final[str] = "opencode_go"
    OPENCODE_ZEN: Final[str] = "opencode_zen"

    CLAUDE_CODE: Final[str] = "claude_code"
    CODEX: Final[str] = "codex"
    OPENCODE_CLI: Final[str] = "opencode_cli"
    INSTALLED: Final[Tuple[str, ...]] = (CLAUDE_CODE, CODEX, OPENCODE_CLI)

    ALL: Final[Tuple[str, ...]] = (
        OPENAI, OPENROUTER, OLLAMA, LM_STUDIO, VLLM, LLAMA_CPP, LOCALAI,
        GROQ, OPENCODE_GO, OPENCODE_ZEN,
    )


class TranscriptCleanupLevel:
    """Values for ``SettingsKey.TRANSCRIPT_CLEANUP_LEVEL``."""
    LIGHT: Final[str] = "light"
    MEDIUM: Final[str] = "medium"
    HIGH: Final[str] = "high"

    ALL: Final[Tuple[str, ...]] = (LIGHT, MEDIUM, HIGH)


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
    Each runner accepts a subset, in this order; see
    :mod:`services.thinking_levels`.
    """
    OFF: Final[str] = "off"
    MINIMAL: Final[str] = "minimal"
    LOW: Final[str] = "low"
    MEDIUM: Final[str] = "medium"
    HIGH: Final[str] = "high"
    XHIGH: Final[str] = "xhigh"
    MAX: Final[str] = "max"
    ULTRA: Final[str] = "ultra"

    ALL: Final[Tuple[str, ...]] = (OFF, MINIMAL, LOW, MEDIUM, HIGH, XHIGH, MAX, ULTRA)


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


class TypeSafeProvider:
    """Values for ``SettingsKey.TYPESAFE_PROVIDER``.

    The first two run the same pinned Jev model; they differ in which key
    pays for it and in whether OpenRouter sits between this computer and
    TypeSafe. The third asks the same questions of a text model the user
    already set up (``SettingsKey.TYPESAFE_TEXT_MODEL`` says which), which
    may run on this computer.
    """
    TYPESAFE: Final[str] = "typesafe"      # api.typesafe.ai with TYPESAFE_API_KEY
    OPENROUTER: Final[str] = "openrouter"  # openrouter.ai with OPENROUTER_API_KEY
    TEXT_MODEL: Final[str] = "text_model"  # a services.text_llm profile and model

    ALL: Final[Tuple[str, ...]] = (TYPESAFE, OPENROUTER, TEXT_MODEL)


class TypeSafeTextModel:
    """Values for ``SettingsKey.TYPESAFE_TEXT_MODEL``: whose text model judges."""
    MEETING: Final[str] = "meeting"  # Meeting Mode → Intelligence (AI insights)
    CLEANUP: Final[str] = "cleanup"  # AI cleanup's model

    ALL: Final[Tuple[str, ...]] = (MEETING, CLEANUP)


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


def _lower_stripped(value: str) -> str:
    return value.strip().lower()


def _batch_instructions(value: str) -> str:
    return value.strip()[: config.MAX_TRANSCRIPT_BATCH_INSTRUCTION_CHARS]


#: Every backend a saved selection may name, and the pre-2.0 API model names
#: that now mean the API backend (see ``LEGACY_API_MODELS``).
_BACKEND_CHOICES: Final[Tuple[str, ...]] = tuple(config.MODEL_VALUE_MAP.values())
_LEGACY_BACKENDS: Final[Dict[str, str]] = {name: "api" for name in LEGACY_API_MODELS}
#: The servers' own defaults, repeated here because their modules import this one.
MCP_DEFAULT_PORT: Final[int] = 8767
REMOTE_HOST_DEFAULT_PORT: Final[int] = 47821
WALKTHROUGH_CAPTURE_DELAYS: Final[Tuple[int, ...]] = (0, 1, 2, 3, 5, 10)


class SettingsKey:
    """Keys persisted in the settings JSON file."""
    HOTKEYS: Final = OptionalDictSetting("hotkeys")
    SELECTED_MODEL: Final = ChoiceSetting(
        "selected_model", config.DEFAULT_BACKEND, _BACKEND_CHOICES, aliases=_LEGACY_BACKENDS,
    )
    API_TRANSCRIPTION_MODEL: Final = StrSetting(
        "api_transcription_model", config.DEFAULT_API_MODEL,
    )
    # The API backend's server and the self-hosted servers' overrides; see
    # services/speech_endpoints.py.
    API_TRANSCRIPTION_ENDPOINT: Final = StrSetting(
        "api_transcription_endpoint", "openai",
    )
    API_TRANSCRIPTION_SERVERS: Final = OptionalDictSetting("api_transcription_servers")
    AUDIO_INPUT_DEVICE: Final = RawSetting("audio_input_device")
    WINDOW_GEOMETRY: Final = RawSetting("window_geometry")
    COMPACT_WINDOW_GEOMETRY: Final = RawSetting("compact_window_geometry")
    COMPACT_MODE: Final = BoolSetting("compact_mode", False)
    # View → Host Mode: the main window shows the host dashboard instead of
    # the recording tabs, with its own window position and size.
    HOST_MODE: Final = BoolSetting("host_mode", False)
    HOST_WINDOW_GEOMETRY: Final = RawSetting("host_window_geometry")
    AUTO_PASTE: Final = BoolSetting("auto_paste", True)
    MACOS_ACCESSIBILITY_INTRO_SEEN: Final = BoolSetting(
        "macos_accessibility_intro_seen", False,
    )
    COPY_CLIPBOARD: Final = BoolSetting("copy_clipboard", True)
    # Put back what the clipboard held before an auto-paste borrowed it; off
    # leaves the pasted text on the clipboard.
    RESTORE_CLIPBOARD: Final = BoolSetting("restore_clipboard", False)
    TRANSCRIPT_CLEANUP_ENABLED: Final = BoolSetting(
        "transcript_cleanup_enabled", config.TRANSCRIPT_CLEANUP_ENABLED,
    )
    TRANSCRIPT_CLEANUP_PROMPT: Final = StrSetting("transcript_cleanup_prompt", "")
    TRANSCRIPT_CLEANUP_PROVIDER: Final = StrSetting(
        "transcript_cleanup_provider", config.TRANSCRIPT_CLEANUP_PROVIDER,
    )
    TRANSCRIPT_CLEANUP_MODEL: Final = StrSetting(
        "transcript_cleanup_model", config.TRANSCRIPT_CLEANUP_OPENROUTER_MODEL,
    )
    TRANSCRIPT_CLEANUP_MODEL_SORT: Final = ChoiceSetting(
        "transcript_cleanup_model_sort", config.TRANSCRIPT_CLEANUP_MODEL_SORT, TranscriptCleanupModelSort.ALL,
    )
    TRANSCRIPT_CLEANUP_REASONING: Final = ChoiceSetting(
        "transcript_cleanup_reasoning", config.TRANSCRIPT_CLEANUP_REASONING, TranscriptCleanupReasoning.ALL,
    )
    # Named OpenAI-compatible endpoints (custom profiles only; builtins omitted).
    TEXT_LLM_PROFILES: Final = ListSetting("text_llm_profiles", in_table=False)
    # JSON list of user-taught rule strings appended to the cleanup prompt
    TRANSCRIPT_CLEANUP_RULES: Final = ListSetting(
        "transcript_cleanup_rules", in_table=False,
    )
    TRANSCRIPT_CLEANUP_PROFILES: Final = RawSetting("transcript_cleanup_profiles")
    QUICK_RECORD_PROFILE: Final = StrSetting("quick_record_profile", "")
    # Multi-file upload: last relation preset (BatchRelation) and the Custom
    # preset's description and combine choice
    TRANSCRIPT_BATCH_RELATION: Final = ChoiceSetting(
        "transcript_batch_relation", config.TRANSCRIPT_BATCH_RELATION, BatchRelation.ALL,
    )
    TRANSCRIPT_BATCH_CUSTOM_INSTRUCTIONS: Final = StrSetting(
        "transcript_batch_custom_instructions", "",
        normalize=_batch_instructions, in_table=False,
    )
    TRANSCRIPT_BATCH_CUSTOM_COMBINE: Final = BoolSetting(
        "transcript_batch_custom_combine", config.TRANSCRIPT_BATCH_CUSTOM_COMBINE,
    )
    MINIMIZE_TRAY: Final = BoolSetting("minimize_tray", True)
    STREAMING_ENABLED: Final = BoolSetting(
        "streaming_enabled", config.STREAMING_ENABLED,
    )
    STREAMING_CHUNK_DURATION: Final = FloatSetting(
        "streaming_chunk_duration", config.STREAMING_CHUNK_DURATION_SEC,
    )
    STREAMING_OVERLAY_FONT_SIZE: Final = IntSetting(
        "streaming_overlay_font_size", config.STREAMING_OVERLAY_FONT_SIZE, clamp=(10, 48),
    )
    # Where the overlay puts its look once preview words arrive; see
    # StreamingOverlayLayout.
    STREAMING_OVERLAY_LAYOUT: Final = ChoiceSetting(
        "streaming_overlay_layout", config.STREAMING_OVERLAY_LAYOUT, StreamingOverlayLayout.ALL,
    )
    # The recording overlay's look; see RecordingOverlayLook. "legacy" is Classic.
    RECORDING_OVERLAY_LOOK: Final = ChoiceSetting(
        "recording_overlay_look", config.RECORDING_OVERLAY_LOOK, RecordingOverlayLook.ALL, aliases=RecordingOverlayLook.ALIASES,
    )
    # Parts of a live look's label: the pulsing dot, "Recording", the clock.
    RECORDING_OVERLAY_DOT: Final = BoolSetting("recording_overlay_dot", True)
    RECORDING_OVERLAY_TEXT: Final = BoolSetting("recording_overlay_text", True)
    RECORDING_OVERLAY_CLOCK: Final = BoolSetting("recording_overlay_clock", True)
    # Application chrome type size as a percent of the designed theme (90–130).
    UI_FONT_SCALE: Final = IntChoiceSetting(
        "ui_font_scale", config.UI_FONT_SCALE, UiFontScale.ALL,
    )
    # Colour theme: "dark", "light", or "system" (follow the OS setting).
    UI_THEME: Final = ChoiceSetting("ui_theme", config.UI_THEME, UiTheme.ALL)
    # Optional dictation onboarding; absence on an established install is not new.
    GUIDED_SETUP_STATE: Final = StrSetting("guided_setup_state", "", in_table=False)
    MEETING_SETUP_COMPLETE: Final = BoolSetting(
        "meeting_setup_complete", False, in_table=False,
    )
    # Legacy keys kept for reading/migrating older settings files
    STREAMING_OVERLAY_ENABLED: Final = RawSetting("streaming_overlay_enabled")
    STREAMING_PASTE_ENABLED: Final = RawSetting("streaming_paste_enabled")
    # {backend: model} and {backend: device}; see services/local_asr/catalog.py.
    LOCAL_ASR_MODELS: Final = DictSetting("local_asr_models", in_table=False)
    LOCAL_ASR_DEVICES: Final = DictSetting("local_asr_devices", in_table=False)
    LOCAL_ASR_LANGUAGE: Final = StrSetting("local_asr_language", "en")
    MEETING_ASR_MODEL: Final = StrSetting("meeting_asr_model", config.MEETING_ASR_MODEL)
    # "local" or "remote" (the paired computer).
    MEETING_ASR_SOURCE: Final = ChoiceSetting(
        "meeting_asr_source", "local", ("local", "remote"), in_table=False,
    )
    # False: meetings use the dictation engine. True: the two keys above and
    # MEETING_WHISPER_MODEL choose a separate one.
    MEETING_SPEECH_SEPARATE: Final = BoolSetting("meeting_speech_separate", False)
    WHISPER_MODEL: Final = StrSetting("whisper_model", config.DEFAULT_WHISPER_MODEL)
    CUSTOM_WHISPER_MODELS: Final = ListSetting("custom_whisper_models")
    WHISPER_DEVICE: Final = StrSetting("whisper_device", config.FASTER_WHISPER_DEVICE)
    WHISPER_COMPUTE_TYPE: Final = StrSetting(
        "whisper_compute_type", config.FASTER_WHISPER_COMPUTE_TYPE,
    )
    # "Keep using the CPU" on the "Use this GPU" offer; it isn't shown again.
    WHISPER_GPU_OFFER_DECLINED: Final = BoolSetting("whisper_gpu_offer_declined", False)
    HF_ACCESS_POLICY: Final = ChoiceSetting(
        "hf_access_policy", HuggingFaceAccessPolicy.ASK, HuggingFaceAccessPolicy.ALL, in_table=False,
    )
    # Legacy boolean replaced by HF_ACCESS_POLICY; kept for migration only.
    HF_HUB_OFFLINE: Final = RawSetting("hf_hub_offline")
    LAST_TAB_INDEX: Final = RawSetting("last_tab_index")
    DEVELOPER_MODE: Final = BoolSetting("developer_mode", config.DEVELOPER_MODE)
    # Recording retention: "keep_all", "custom" (+ max_saved_recordings count),
    # or "size_limit" (+ max_saved_recordings_mb folder size)
    RECORDING_RETENTION_MODE: Final = ChoiceSetting(
        "recording_retention_mode", RecordingRetentionMode.CUSTOM, RecordingRetentionMode.ALL,
    )
    MAX_SAVED_RECORDINGS: Final = IntSetting(
        "max_saved_recordings", config.MAX_SAVED_RECORDINGS, clamp=(1, None),
    )
    MAX_SAVED_RECORDINGS_MB: Final = IntSetting(
        "max_saved_recordings_mb", config.MAX_SAVED_RECORDINGS_MB, clamp=(1, None),
    )
    # Record hotkey activation: "toggle" or "push_hold"
    RECORDING_TRIGGER_MODE: Final = ChoiceSetting(
        "recording_trigger_mode", config.RECORDING_TRIGGER_MODE, RecordingTriggerMode.ALL,
    )
    CONFIRM_HISTORY_ENTRY_DELETE: Final = BoolSetting(
        "confirm_history_entry_delete", True,
    )
    CONFIRM_MEETING_DELETE: Final = BoolSetting("confirm_meeting_delete", True)
    # Meeting Mode
    MEETING_WHISPER_MODEL: Final = StrSetting(
        "meeting_whisper_model", config.MEETING_WHISPER_MODEL,
    )
    MEETING_LANGUAGE: Final = ChoiceSetting(
        "meeting_language", config.MEETING_LANGUAGE, MeetingLanguage.ALL, normalize=_lower_stripped,
    )
    MEETING_LLM_PROVIDER: Final = StrSetting(
        "meeting_llm_provider", TranscriptCleanupProvider.OPENROUTER,
    )
    MEETING_LLM_MODEL: Final = StrSetting("meeting_llm_model", config.MEETING_LLM_MODEL)
    MEETING_AGENT_CORE: Final = ChoiceSetting(
        "meeting_agent_core", config.MEETING_AGENT_CORE, MeetingAgentCore.ALL,
    )
    MEETING_BACKUP_AGENT: Final = ChoiceSetting(
        "meeting_backup_agent", config.MEETING_BACKUP_AGENT, ("", *MeetingAgentCore.INSTALLED),
    )
    #: ``{agent_id: model}`` for installed agents; "" keeps the agent's default.
    MEETING_AGENT_MODELS: Final = DictSetting("meeting_agent_models")
    #: ``TranscriptCleanupReasoning`` values for an installed agent's live
    #: passes and its final report.
    MEETING_LIVE_THINKING: Final = ChoiceSetting(
        "meeting_live_thinking", config.MEETING_LIVE_THINKING, TranscriptCleanupReasoning.ALL,
    )
    MEETING_FINAL_THINKING: Final = ChoiceSetting(
        "meeting_final_thinking", config.MEETING_FINAL_THINKING, TranscriptCleanupReasoning.ALL,
    )
    MEETING_END_REDECODE: Final = BoolSetting(
        "meeting_end_redecode", config.MEETING_END_REDECODE,
    )
    MEETING_REDECODE_COVERAGE_GUARD: Final = BoolSetting(
        "meeting_redecode_coverage_guard", False,
    )
    MEETING_END_POLISH: Final = BoolSetting(
        "meeting_end_polish", config.MEETING_END_POLISH,
    )
    MEETING_INSIGHT_REVIEW: Final = BoolSetting("meeting_insight_review", False)
    MEETING_INSIGHT_REVIEW_CONSENT: Final = StrSetting(
        "meeting_insight_review_consent", "", in_table=False,
    )
    MEETING_INSIGHT_REVIEW_SENSITIVITY: Final = ChoiceSetting(
        "meeting_insight_review_sensitivity", "normal", ("normal", "thorough"),
    )
    MEETING_END_REPORT: Final = BoolSetting(
        "meeting_end_report", config.MEETING_END_REPORT,
    )
    MEETING_REPORT_RIBBON: Final = BoolSetting(
        "meeting_report_ribbon", config.MEETING_REPORT_RIBBON,
    )
    MEETING_REPORT_BRIEF: Final = BoolSetting(
        "meeting_report_brief", config.MEETING_REPORT_BRIEF,
    )
    MEETING_REPORT_SIGNAL: Final = BoolSetting(
        "meeting_report_signal", config.MEETING_REPORT_SIGNAL,
    )
    MEETING_CLOUD_CONSENT_GIVEN: Final = BoolSetting(
        "meeting_cloud_consent_given", False,
    )
    MEETING_CLOUD_LAST_ENABLED: Final = BoolSetting("meeting_cloud_last_enabled", False)
    MEETING_SPEAKER_ID_BACKEND: Final = ChoiceSetting(
        "meeting_speaker_id_backend", config.MEETING_SPEAKER_ID_BACKEND, MeetingSpeakerIdBackend.ALL,
    )
    MEETING_AUDIO_UPLOAD_CONSENT_GIVEN: Final = BoolSetting(
        "meeting_audio_upload_consent_given", False,
    )
    MEETING_UNSUPPORTED_PLATFORM_ACK: Final = BoolSetting(
        "meeting_unsupported_platform_ack", False,
    )
    MEETING_LINUX_PREVIEW_ACK_VERSION: Final = IntChoiceSetting(
        "meeting_linux_preview_ack_version", 0, (MEETING_LINUX_PREVIEW_ACK_VERSION,), strict=True, in_table=False,
    )
    MEETING_MODE_INTRO_SEEN: Final = BoolSetting("meeting_mode_intro_seen", False)
    MEETING_PAST_RECALL_ENABLED: Final = BoolSetting(
        "meeting_past_recall_enabled", False,
    )
    MEETING_CONTEXT_FOLDER_ENABLED: Final = BoolSetting(
        "meeting_context_folder_enabled", False,
    )
    MEETING_CONTEXT_FOLDER_PATH: Final = StrSetting("meeting_context_folder_path", "")
    MEETING_SERVER_BIND: Final = ChoiceSetting(
        "meeting_server_bind", config.MEETING_SERVER_BIND, MeetingServerBind.ALL,
    )
    MEETING_SERVER_PORT: Final = IntSetting(
        "meeting_server_port", config.MEETING_SERVER_PORT, clamp=(0, 65535),
    )
    MCP_ENABLED: Final = BoolSetting("mcp_enabled", False, in_table=False)
    MCP_PORT: Final = IntSetting(
        "mcp_port", MCP_DEFAULT_PORT, strict=True, valid=(1, 65535), in_table=False,
    )
    MCP_TAILSCALE_ENABLED: Final = BoolSetting(
        "mcp_tailscale_enabled", False, in_table=False,
    )
    MCP_RETITLE_TRANSCRIPTIONS: Final = BoolSetting(
        "mcp_retitle_transcriptions", False, in_table=False,
    )
    MCP_RETITLE_MEETINGS: Final = BoolSetting(
        "mcp_retitle_meetings", False, in_table=False,
    )
    MCP_SETTINGS_ACCESS: Final = BoolSetting(
        "mcp_settings_access", False, in_table=False,
    )
    # {setting key: True} for the settings MCP agents may change.
    MCP_WRITABLE_SETTINGS: Final = DictSetting("mcp_writable_settings", in_table=False)
    # Remote engine (services/remote_asr). Client: the paired host's address,
    # pinned certificate fingerprint and name (the token is in the OS
    # credential store). Host: sharing switch, port, and paired devices,
    # stored as token digests.
    REMOTE_ENGINE_CLIENT: Final = OptionalDictSetting("remote_engine_client")
    REMOTE_HOST_ENABLED: Final = BoolSetting(
        "remote_host_enabled", False, in_table=False,
    )
    # Explicit host opt-in for catalog access and model downloads by paired devices.
    REMOTE_HOST_MODEL_MANAGEMENT: Final = BoolSetting(
        "remote_host_model_management", False, in_table=False,
    )
    REMOTE_HOST_PORT: Final = IntSetting(
        "remote_host_port", REMOTE_HOST_DEFAULT_PORT, valid=(1, 65535), in_table=False,
    )
    REMOTE_HOST_DEVICES: Final = ListSetting("remote_host_devices", in_table=False)
    # Let computers signed in to the same Tailscale account pair without a code.
    REMOTE_HOST_TAILSCALE_TRUST: Final = BoolSetting(
        "remote_host_tailscale_trust", True, in_table=False,
    )
    # Host opt-in: keep paired computers' records (history, meetings) here.
    REMOTE_HOST_KEEP_RECORDS: Final = BoolSetting(
        "remote_host_keep_records", False, in_table=False,
    )
    # Host opt-in: paired computers may switch this computer's MCP server and
    # read its connection details (services/agent_mcp/host_control.py).
    REMOTE_HOST_MANAGE_MCP: Final = BoolSetting(
        "remote_host_manage_mcp", False, in_table=False,
    )
    # Client: where this computer's records are kept while paired:
    # "local" (default), "host" (moved there), or "both" (copied there).
    REMOTE_RECORDS_LOCATION: Final = ChoiceSetting(
        "remote_records_location", "local", ("local", "host", "both"), in_table=False,
    )
    # Client opt-in: the paired host's MCP may query local history while online.
    # Independent of record storage and permission to manage the host's MCP.
    REMOTE_CLIENT_HISTORY: Final = BoolSetting(
        "remote_client_history", False, in_table=False,
    )
    # TypeSafe fast judgments. The master switch gates every remote judgment;
    # each feature has its own switch so one can be trialled at a time.
    TYPESAFE_ENABLED: Final = BoolSetting("typesafe_enabled", False)
    # Where judgments go: TypeSafe directly, the same model through
    # OpenRouter with the OpenRouter key, or one of the user's own text
    # models (see TypeSafeProvider).
    TYPESAFE_PROVIDER: Final = ChoiceSetting(
        "typesafe_provider", TypeSafeProvider.TYPESAFE, TypeSafeProvider.ALL,
    )
    # Which text model answers on the text-model route (TypeSafeTextModel).
    TYPESAFE_TEXT_MODEL: Final = ChoiceSetting(
        "typesafe_text_model", TypeSafeTextModel.MEETING, TypeSafeTextModel.ALL,
    )
    TYPESAFE_CITATIONS_ENABLED: Final = BoolSetting("typesafe_citations_enabled", False)
    TYPESAFE_SEMANTIC_SEARCH_ENABLED: Final = BoolSetting(
        "typesafe_semantic_search_enabled", False,
    )
    TYPESAFE_QUESTION_RADAR_ENABLED: Final = BoolSetting(
        "typesafe_question_radar_enabled", False,
    )
    TYPESAFE_HIGHLIGHTS_ENABLED: Final = BoolSetting(
        "typesafe_highlights_enabled", False,
    )
    TYPESAFE_TOPIC_SHIFT_ENABLED: Final = BoolSetting(
        "typesafe_topic_shift_enabled", True,
    )
    TYPESAFE_VOICE_COMMANDS_ENABLED: Final = BoolSetting(
        "typesafe_voice_commands_enabled", False,
    )
    TYPESAFE_VOICE_COMMAND_NAMES: Final = OptionalListSetting(
        "typesafe_voice_command_names",
    )
    # In-app updater. Absent keys mean both automatic check and notify are on.
    UPDATE_CHECK_ENABLED: Final = BoolSetting(
        "update_check_enabled", config.UPDATE_CHECK_ENABLED,
    )
    UPDATE_NOTIFY_ENABLED: Final = BoolSetting(
        "update_notify_enabled", config.UPDATE_NOTIFY_ENABLED,
    )
    UPDATE_RESTART_PROMPT_ENABLED: Final = BoolSetting(
        "update_restart_prompt_enabled", config.UPDATE_RESTART_PROMPT_ENABLED,
    )
    # Opt-in: also offer GitHub prereleases (the beta release track).
    UPDATE_BETA_TRACK_ENABLED: Final = BoolSetting("update_beta_track_enabled", False)
    UPDATE_LAST_CHECK_AT: Final = RawSetting("update_last_check_at")
    UPDATE_LAST_FAILED_CHECK_AT: Final = RawSetting("update_last_failed_check_at")
    UPDATE_API_RETRY_AT: Final = RawSetting("update_api_retry_at")
    # Personalization (docs/design/personalize/design.md).
    # Knowing which app has focus is native, local and on by default.
    APP_CONTEXT_ENABLED: Final = BoolSetting("app_context_enabled", True)
    # Reading text near the caret leaves this computer for AI cleanup, so it
    # is opt-in.
    APP_CONTEXT_READ_TEXT: Final = BoolSetting("app_context_read_text", False)
    # App ids whose text is never read.
    APP_CONTEXT_EXCLUDED_APPS: Final = ListSetting("app_context_excluded_apps")
    APP_STYLES_ENABLED: Final = BoolSetting("app_styles_enabled", True)
    # {category: tone}. Absent means every category is Formal, which is the
    # output existing installs had; new installs are seeded once.
    APP_STYLE_TONES: Final = OptionalDictSetting("app_style_tones")
    # [{match, category}] user corrections to the built-in app catalogue.
    APP_STYLE_OVERRIDES: Final = ListSetting("app_style_overrides")
    # Preset used while AI cleanup is on; the cleanup switch stays the
    # master on/off, so there is no stored "none".
    TRANSCRIPT_CLEANUP_LEVEL: Final = ChoiceSetting(
        "transcript_cleanup_level", config.TRANSCRIPT_CLEANUP_LEVEL, TranscriptCleanupLevel.ALL,
    )
    # Saved rewrite instructions. Absent means the starters, so deleting
    # every transform leaves an empty list rather than bringing them back.
    TEXT_TRANSFORMS: Final = OptionalListSetting("text_transforms")
    # Command Mode with nothing selected writes new text at the cursor.
    COMMAND_MODE_INSERT_WITHOUT_SELECTION: Final = BoolSetting(
        "command_mode_insert_without_selection", True,
    )
    # [{id, term, starred, heard, learned, new}]
    DICTATION_DICTIONARY: Final = ListSetting("dictation_dictionary")
    DICTIONARY_LEARN_ENABLED: Final = BoolSetting("dictionary_learn_enabled", True)
    # Send dictionary terms to speech engines that accept hints.
    DICTIONARY_STEER_RECOGNITION: Final = BoolSetting(
        "dictionary_steer_recognition", True,
    )
    # [{id, trigger, text, formatted}]; exempt from the backup URL scrub
    # because the expansion text is the user's own content.
    DICTATION_SNIPPETS: Final = ListSetting("dictation_snippets")
    SNIPPETS_ENABLED: Final = BoolSetting("snippets_enabled", True)
    # Push-and-hold: a quick second press keeps recording hands-free.
    RECORDING_HANDS_FREE_LATCH: Final = BoolSetting("recording_hands_free_latch", True)
    # Ordered [{name, hostapi}] microphones; the single source of truth once
    # migrated from the legacy AUDIO_INPUT_DEVICE index (kept for downgrades).
    AUDIO_INPUT_PRIORITY: Final = ListSetting("audio_input_priority")
    # Language codes the user dictates in, and the one currently active ("" =
    # the engine's own setting). Never written into LOCAL_ASR_LANGUAGE.
    DICTATION_LANGUAGES: Final = ListSetting("dictation_languages")
    DICTATION_ACTIVE_LANGUAGE: Final = StrSetting("dictation_active_language", "")
    SCRATCHPAD_ALWAYS_ON_TOP: Final = BoolSetting("scratchpad_always_on_top", True)
    # {x, y, width, height} of the Scratchpad window; no default.
    SCRATCHPAD_GEOMETRY: Final = RawSetting("scratchpad_geometry")
    # The ``updated`` date of the "New in OpenWhisper" note last dismissed
    # (ui_qt/whats_new.py); "" = none yet.
    WHATS_NEW_SEEN: Final = StrSetting("whats_new_seen", "")
    # Automation (docs/design/automation/design.md). Saved actions: what a
    # hotkey, a spoken name or the local API does with the words.
    ACTIONS: Final = ListSetting("actions")
    # Spoken routing asks Fast judgments which action the words name.
    VOICE_ROUTING_ENABLED: Final = BoolSetting("voice_routing_enabled", False)
    # Also route a normal dictation that starts with an action's name.
    VOICE_ROUTING_ON_DICTATION: Final = BoolSetting("voice_routing_on_dictation", False)
    # The Models pages' memory: the model last chosen per provider, and the
    # provider the cleanup endpoint picker last showed. No defaults; absent
    # until a choice is made.
    CLEANUP_MODEL_MEMORY: Final = DictSetting("cleanup_model_memory", in_table=False)
    MEETING_MODEL_MEMORY: Final = DictSetting("meeting_model_memory", in_table=False)
    CLEANUP_ENDPOINT_PROVIDER: Final = StrSetting(
        "cleanup_endpoint_provider", config.TRANSCRIPT_CLEANUP_PROVIDER, in_table=False,
    )
    # Live API permissions, rechecked on every request. Reading events is
    # the same trust as reading history; the rest can act for the user.
    API_LIVE_EVENTS: Final = BoolSetting("api_live_events", True)
    API_RUN_ACTIONS: Final = BoolSetting("api_run_actions", False)
    API_CONTROL_RECORDING: Final = BoolSetting("api_control_recording", False)
    API_TEXT_FILTERS: Final = BoolSetting("api_text_filters", False)
    # On top of running actions: let a program send words to the user's
    # coding agent (an agent or Claude Code channel action), which acts on them.
    API_AGENT_PROMPTS: Final = BoolSetting("api_agent_prompts", False)
    # Saved hooks: [{id, name, url, events, enabled}] and so on; secrets are
    # in the credential store.
    AUTOMATION_WEBHOOKS: Final = ListSetting("automation_webhooks")
    AUTOMATION_PROGRAMS: Final = ListSetting("automation_programs")
    AUTOMATION_FILTERS: Final = ListSetting("automation_filters")
    # The MCP Events draft extension (events/subscribe with webhook delivery).
    MCP_EVENTS_ENABLED: Final = BoolSetting("mcp_events_enabled", False)
    # How a walkthrough's PDF and Markdown look (services/walkthrough/document.py):
    # the heading ("" for none), the line with the date, length and screenshot
    # count, the note on how to read it, screenshot captions, and the paper.
    WALKTHROUGH_TITLE: Final = StrSetting(
        "walkthrough_title", "Walkthrough", normalize=str.strip,
    )
    WALKTHROUGH_SHOW_DETAILS: Final = BoolSetting("walkthrough_show_details", True)
    WALKTHROUGH_SHOW_ABOUT: Final = BoolSetting("walkthrough_show_about", True)
    WALKTHROUGH_SHOW_CAPTIONS: Final = BoolSetting("walkthrough_show_captions", True)
    WALKTHROUGH_PAGE_SIZE: Final = ChoiceSetting(
        "walkthrough_page_size", WalkthroughPageSize.AUTO, WalkthroughPageSize.ALL,
    )
    WALKTHROUGH_COPY_FORMAT: Final = ChoiceSetting(
        "walkthrough_copy_format", WalkthroughCopyFormat.PDF, WalkthroughCopyFormat.ALL,
    )
    # The "New" badge on Quick Record's walkthrough button gives way to a
    # "?" once a walkthrough has started.
    WALKTHROUGH_INTRO_SEEN: Final = BoolSetting("walkthrough_intro_seen", False)
    # Whether the main window goes to the tray as a walkthrough starts, out
    # of the way of what is being shown.
    WALKTHROUGH_MINIMIZE_TO_TRAY: Final = BoolSetting(
        "walkthrough_minimize_to_tray", False,
    )
    # Whether an installed agent writes a finished walkthrough up, which one
    # (a MeetingAgentCore.INSTALLED id), and what it is told to make ("" for
    # the default instructions; see services/walkthrough/write_up.py).
    WALKTHROUGH_WRITE_UP: Final = BoolSetting("walkthrough_write_up", False)
    WALKTHROUGH_WRITE_UP_AGENT: Final = ChoiceSetting(
        "walkthrough_write_up_agent", MeetingAgentCore.CLAUDE_CODE, MeetingAgentCore.INSTALLED,
    )
    WALKTHROUGH_WRITE_UP_INSTRUCTIONS: Final = StrSetting(
        "walkthrough_write_up_instructions", "", normalize=str.strip,
    )
    # Saved write-up styles, [{id, name, instructions}], beside the built-in
    # default. Absent means the starters, so deleting every style leaves an
    # empty list rather than bringing them back.
    WALKTHROUGH_WRITE_UP_STYLES: Final = OptionalListSetting(
        "walkthrough_write_up_styles",
    )
    # Whether the write-up follows notes the user speaks to the agent
    # ("note for the agent: make the text purple") instead of writing them up.
    WALKTHROUGH_WRITE_UP_NOTES: Final = BoolSetting("walkthrough_write_up_notes", False)
    # What a walkthrough's Snap takes, as last picked from the column's Snap
    # menu: the Snap button and the Screenshot shortcut repeat it.
    WALKTHROUGH_SNAP_MODE: Final = ChoiceSetting(
        "walkthrough_snap_mode", WalkthroughSnapMode.REGION, WalkthroughSnapMode.ALL,
    )
    WALKTHROUGH_AUTO_MODE: Final = BoolSetting("walkthrough_auto_mode", False)
    WALKTHROUGH_CAPTURE_DELAY: Final = IntChoiceSetting(
        "walkthrough_capture_delay", 0, WALKTHROUGH_CAPTURE_DELAYS, strict=True,
    )
    WALKTHROUGH_KEEP_MARKS: Final = BoolSetting("walkthrough_keep_marks", False)
    SNAP_TO_EDGES: Final = BoolSetting("snap_to_edges", True)
    SNAP_BOX_INFILL: Final = BoolSetting("snap_box_infill", False)
    # The capture folder name of the walkthrough last closed on the Walkthrough
    # tab: while it is still the newest, the tab opens without one.
    WALKTHROUGH_CLOSED: Final = StrSetting("walkthrough_closed", "")


#: Keys the single live-preview toggle replaced. Dropped whenever that toggle
#: is written, so an old settings file cannot resurrect the split switches.
LEGACY_STREAMING_KEYS: Final[tuple[str, ...]] = (
    SettingsKey.STREAMING_OVERLAY_ENABLED,
    SettingsKey.STREAMING_PASTE_ENABLED,
    "streaming_tiny_model_enabled",
    "live_typing_enabled",
)


#: Every declared setting, in declaration order.
ALL_SETTINGS: Final[Tuple[Setting[Any], ...]] = tuple(
    value for name, value in vars(SettingsKey).items()
    if name.isupper() and isinstance(value, Setting)
)


#: The default of every setting that has one, derived from the declarations
#: above. Settings declared ``in_table=False`` are left out: either their
#: absence has its own meaning (``APP_STYLE_TONES``, ``TEXT_TRANSFORMS``), or
#: their read default lives with the owning module. Resolvers fall back to
#: these, and code that needs a default reads it from here or from
#: ``SettingsKey.X.default``.
SETTING_DEFAULTS: Final[Mapping[str, Any]] = MappingProxyType({
    setting: setting.default for setting in ALL_SETTINGS if setting.in_table
})


_HF_HUB_OFFLINE_ENV: Final[str] = "HF_HUB_OFFLINE"
_HF_HUB_OFFLINE_TRUTHY: Final[Tuple[str, ...]] = ("1", "on", "true", "yes")


#: Stands for a key that is not saved, which is not the same as saved None.
_UNSET: Final[Any] = object()


class SettingsManager:
    """Handles loading and saving application settings."""

    def __init__(self, settings_file: Optional[str] = None):
        self.settings_file = settings_file or config.SETTINGS_FILE
        # A setting mutation is a read-modify-write transaction.  Several UI
        # and background workers share this singleton, so the lock must cover
        # the complete transaction rather than only the final write.
        self._lock = threading.RLock()
        self._cached_signature: Optional[Tuple[Any, ...]] = None
        self._cached_settings: Optional[Dict[str, Any]] = None
        # (keys, callback) pairs told when a save changes one of their keys.
        self._watchers: List[Tuple[frozenset[str], Callable[[frozenset[str]], None]]] = []
        # The module singleton is built when settings are first imported,
        # after any pending backup restore and before anything this launch
        # can write the file, so this tells a brand-new install apart.
        self._launch_check = (
            os.path.abspath(self.settings_file),
            os.path.exists(self.settings_file),
        )

    def is_new_install(self) -> bool:
        """Whether this launch started without a settings file.

        Only for the file checked when the manager was built: one pointed at
        another file since (tests, a data-root change) is never new.
        """
        path, existed = self._launch_check
        return not existed and os.path.abspath(self.settings_file) == path

    def _file_signature(self) -> Optional[Tuple[Any, ...]]:
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
        # Deliberately broad. Every reader in the app relies on a non-strict
        # load never raising, whatever is wrong with the file (unreadable, not
        # UTF-8, not JSON, not an object, nested too deeply, too large); a
        # missed case here would break all of them at once.
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

    def file_signature(self) -> Optional[Tuple[Any, ...]]:
        """A cheap token that changes when the settings file does, or None.

        For a caller that polls: compare it before paying for
        ``load_all_settings``, which copies every setting.
        """
        with self._lock:
            return self._file_signature()

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

    def watch(
        self, keys: Iterable[str], callback: Callable[[frozenset[str]], None],
    ) -> Callable[[], None]:
        """Call ``callback(changed_keys)`` after a save changes any of ``keys``.

        Whoever writes a setting need not know who shows it. The callback
        runs on the thread that saved, after the file is replaced; a save
        through this manager is the only change it hears of. Returns a
        function that stops the watch.
        """
        entry = (frozenset(keys), callback)
        with self._lock:
            self._watchers.append(entry)

        def stop() -> None:
            with self._lock:
                if entry in self._watchers:
                    self._watchers.remove(entry)

        return stop

    def _watched_values(self, settings: Mapping[str, Any]) -> Dict[str, Any]:
        """Copies of the watched keys' values, taken before a save changes them."""
        return {
            key: deepcopy(settings[key]) if key in settings else _UNSET
            for keys, _callback in self._watchers for key in keys
        }

    def _tell_watchers(self, before: Dict[str, Any], settings: Mapping[str, Any]) -> None:
        changed = frozenset(
            key for key, value in before.items() if settings.get(key, _UNSET) != value
        )
        if not changed:
            return
        with self._lock:
            watchers = list(self._watchers)
        for keys, callback in watchers:
            if keys & changed:
                try:
                    callback(keys & changed)
                except Exception:
                    logger.exception("A settings watcher failed")

    def save_all_settings(self, settings: Dict[str, Any]) -> None:
        """Persist the complete settings mapping."""
        try:
            with self._lock:
                before = (self._watched_values(self._load_all_settings_unlocked())
                          if self._watchers else {})
                self._save_all_settings_unlocked(settings)
            self._tell_watchers(before, settings)
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
            before = self._watched_values(settings)
            result = mutator(settings)
            self._save_all_settings_unlocked(settings)
        self._tell_watchers(before, settings)
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
            hotkeys: Dict[str, str] = settings.get(
                SettingsKey.HOTKEYS, config.DEFAULT_HOTKEYS.copy())
            return hotkeys
        except Exception as e:
            logger.warning(f"Failed to load settings: {e}; using the default hotkeys",
                           exc_info=True)

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
            if isinstance(selected_model, str) and selected_model in config.MODEL_VALUE_MAP.values():
                return selected_model
        except Exception as e:
            logger.warning(f"Failed to load model selection: {e}; using the default",
                           exc_info=True)

        return SettingsKey.SELECTED_MODEL.default

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
        # Raw, not typed: an invalid stored policy is rewritten below.
        policy = settings.get(SettingsKey.HF_ACCESS_POLICY)
        if isinstance(policy, str) and policy in HuggingFaceAccessPolicy.ALL:
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
                logger.warning(f"Failed to persist HF policy migration: {e}",
                               exc_info=True)
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


def is_hf_hub_offline_env_set() -> bool:
    """Return whether ``HF_HUB_OFFLINE`` is set in the process env.

    An externally supplied ``HF_HUB_OFFLINE=1`` is a hard override: model
    downloads are disabled regardless of the persisted access policy.
    """
    return os.environ.get(_HF_HUB_OFFLINE_ENV, "").strip().lower() in _HF_HUB_OFFLINE_TRUTHY


settings_manager = SettingsManager()


def seed_new_install_settings(manager: Optional["SettingsManager"] = None) -> bool:
    """Write the defaults a brand-new install starts with; True when written.

    Existing installs keep today's output (no app_style_tones key means
    every category is Formal); only a launch that found no settings file
    gets the casual chat tones, and only once.
    """
    manager = settings_manager if manager is None else manager
    is_new = getattr(manager, "is_new_install", None)
    if not callable(is_new) or not is_new():
        return False
    from services.app_styles import NEW_INSTALL_TONES

    def seed(settings: Dict[str, Any]) -> bool:
        if SettingsKey.APP_STYLE_TONES in settings:
            return False
        settings[SettingsKey.APP_STYLE_TONES] = dict(NEW_INSTALL_TONES)
        return True

    try:
        return bool(manager.mutate_settings(seed))
    except Exception as exc:
        logger.warning("Could not seed new-install settings: %s", exc, exc_info=True)
        return False


def keep_unchosen_engine(manager: Optional["SettingsManager"] = None) -> bool:
    """Save the engine of an install that never chose one; True when written.

    Such an install has no saved selection and follows
    ``config.DEFAULT_BACKEND``, so moving a platform's default would switch
    it to an engine it hasn't downloaded. A new install saves the default
    it starts with. An existing Linux x86_64 install keeps Local Whisper,
    its default until Parakeet replaced it (October 2026). Run before the
    main window reads the selection.
    """
    manager = settings_manager if manager is None else manager
    is_new = getattr(manager, "is_new_install", None)
    if not callable(is_new):
        return False
    if is_new():
        engine = config.DEFAULT_BACKEND
    elif sys.platform.startswith("linux") and config.DEFAULT_BACKEND == "parakeet":
        engine = "local_whisper"
    else:
        return False
    if SettingsKey.SELECTED_MODEL in manager.load_all_settings():
        return False

    def keep(settings: Dict[str, Any]) -> bool:
        if SettingsKey.SELECTED_MODEL in settings:
            return False
        settings[SettingsKey.SELECTED_MODEL] = engine
        return True

    try:
        return bool(manager.mutate_settings(keep))
    except Exception as exc:
        logger.warning("Could not save the install's engine: %s", exc, exc_info=True)
        return False


#: Every declared setting by its bare key, for code that is handed a key name
#: (backup restore, the MCP settings tools, ``resolve_bool_setting``).
SETTINGS_BY_KEY: Final[Mapping[str, Setting[Any]]] = MappingProxyType({
    setting.key: setting for setting in ALL_SETTINGS
})


def setting_value(key: str, settings: Optional[Mapping[str, Any]] = None) -> Any:
    """Return the stored value of ``key``, or its default when unset.

    Unvalidated, like ``settings.get(key, default)``. Read a setting with
    ``SettingsKey.X.read(settings)``, which validates and is typed; this is
    the raw lookup the resolvers in this module are built on.
    ``settings`` is a loaded mapping; None reads the settings file.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()
    return settings.get(key, SETTING_DEFAULTS[key])


def resolve_bool_setting(
    key: str, settings: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Return ``key``'s stored bool, or its default when unset or not a bool.

    For code that holds a key name; with the key itself, use
    ``SettingsKey.X.read(settings)``.
    """
    setting = SETTINGS_BY_KEY[key]
    if not isinstance(setting, BoolSetting):
        raise TypeError(f"{key} is not an on/off setting")
    return setting.read(settings)


def resolve_choice_setting(
    key: str,
    choices: Tuple[Any, ...],
    settings: Optional[Mapping[str, Any]] = None,
) -> Any:
    """Return ``key``'s stored value when it is one of ``choices``, else its default.

    For a choice list other than the setting's declared one; otherwise use
    ``SettingsKey.X.read(settings)``.
    """
    raw = setting_value(key, settings)
    return raw if raw in choices else SETTING_DEFAULTS[key]


# Single-key resolvers: the stored value when valid, else the default, by the
# rules each setting declares on SettingsKey. They are the declarations' own
# ``read`` methods, kept under their established names; each is called as
# ``resolve_x(settings=None)``. New code reads ``SettingsKey.X.read(settings)``.
resolve_recording_trigger_mode = SettingsKey.RECORDING_TRIGGER_MODE.read
# A look named by an earlier build maps to its successor (RecordingOverlayLook.ALIASES).
resolve_recording_overlay_look = SettingsKey.RECORDING_OVERLAY_LOOK.read
resolve_streaming_overlay_layout = SettingsKey.STREAMING_OVERLAY_LAYOUT.read
resolve_recording_overlay_dot = SettingsKey.RECORDING_OVERLAY_DOT.read
resolve_recording_overlay_text = SettingsKey.RECORDING_OVERLAY_TEXT.read
resolve_recording_overlay_clock = SettingsKey.RECORDING_OVERLAY_CLOCK.read


def resolve_ui_theme(settings: Optional[Mapping[str, Any]] = None) -> str:
    """The saved theme; with none saved, Omarchy's own on an Omarchy desktop."""
    from services.desktop_session import use_omarchy_ui

    if settings is None:
        settings = settings_manager.load_all_settings()
    if SettingsKey.UI_THEME not in settings and use_omarchy_ui():
        return UiTheme.OMARCHY
    return SettingsKey.UI_THEME.read(settings)


resolve_transcript_cleanup_reasoning = SettingsKey.TRANSCRIPT_CLEANUP_REASONING.read
resolve_transcript_batch_relation = SettingsKey.TRANSCRIPT_BATCH_RELATION.read
resolve_meeting_agent_core = SettingsKey.MEETING_AGENT_CORE.read
resolve_meeting_backup_agent = SettingsKey.MEETING_BACKUP_AGENT.read
resolve_meeting_live_thinking = SettingsKey.MEETING_LIVE_THINKING.read
resolve_meeting_final_thinking = SettingsKey.MEETING_FINAL_THINKING.read
resolve_meeting_server_bind = SettingsKey.MEETING_SERVER_BIND.read
resolve_transcript_cleanup_level = SettingsKey.TRANSCRIPT_CLEANUP_LEVEL.read

# On/off settings.
resolve_transcript_batch_custom_combine = SettingsKey.TRANSCRIPT_BATCH_CUSTOM_COMBINE.read
resolve_developer_mode = SettingsKey.DEVELOPER_MODE.read
resolve_update_check_enabled = SettingsKey.UPDATE_CHECK_ENABLED.read
resolve_update_notify_enabled = SettingsKey.UPDATE_NOTIFY_ENABLED.read
resolve_update_restart_prompt_enabled = SettingsKey.UPDATE_RESTART_PROMPT_ENABLED.read
resolve_update_beta_track_enabled = SettingsKey.UPDATE_BETA_TRACK_ENABLED.read
# Meeting consents and acknowledgements, off until given. On unsupported
# platforms (old macOS, unsupported Linux architectures, other OSes) the
# Meeting Mode tab stays muted until the ack is granted once; supported Linux
# uses the versioned resolve_meeting_linux_preview_ack instead.
resolve_meeting_audio_upload_consent = SettingsKey.MEETING_AUDIO_UPLOAD_CONSENT_GIVEN.read
resolve_meeting_cloud_consent = SettingsKey.MEETING_CLOUD_CONSENT_GIVEN.read
resolve_meeting_unsupported_platform_ack = SettingsKey.MEETING_UNSUPPORTED_PLATFORM_ACK.read
resolve_meeting_mode_intro_seen = SettingsKey.MEETING_MODE_INTRO_SEEN.read
# Off by default. Past recall lets AI insights send excerpts of earlier
# meetings to the model, and the context folder excerpts of files in that
# folder; each is separate from the current meeting's AI insights consent.
resolve_meeting_past_recall_enabled = SettingsKey.MEETING_PAST_RECALL_ENABLED.read
resolve_meeting_context_folder_enabled = SettingsKey.MEETING_CONTEXT_FOLDER_ENABLED.read
# End-of-meeting steps and report views. The coverage guard is an opt-in
# word-count safeguard for live and retried re-transcription.
resolve_meeting_end_redecode = SettingsKey.MEETING_END_REDECODE.read
resolve_meeting_redecode_coverage_guard = SettingsKey.MEETING_REDECODE_COVERAGE_GUARD.read
resolve_meeting_end_polish = SettingsKey.MEETING_END_POLISH.read
resolve_meeting_end_report = SettingsKey.MEETING_END_REPORT.read
resolve_meeting_report_ribbon = SettingsKey.MEETING_REPORT_RIBBON.read
resolve_meeting_report_brief = SettingsKey.MEETING_REPORT_BRIEF.read
resolve_meeting_report_signal = SettingsKey.MEETING_REPORT_SIGNAL.read
# Fast judgments send a short excerpt of text and get a typed answer back,
# not generated text: to TypeSafe, or to the user's own text model. Off by
# default; every feature also needs it, and meeting features need the
# meeting's AI insights consent too unless the judge runs on this computer.
resolve_typesafe_enabled = SettingsKey.TYPESAFE_ENABLED.read
resolve_typesafe_provider = SettingsKey.TYPESAFE_PROVIDER.read
resolve_typesafe_text_model = SettingsKey.TYPESAFE_TEXT_MODEL.read
# Personalization switches.
resolve_app_context_enabled = SettingsKey.APP_CONTEXT_ENABLED.read
resolve_app_context_read_text = SettingsKey.APP_CONTEXT_READ_TEXT.read
resolve_app_styles_enabled = SettingsKey.APP_STYLES_ENABLED.read
resolve_command_mode_insert_without_selection = (
    SettingsKey.COMMAND_MODE_INSERT_WITHOUT_SELECTION.read)
resolve_dictionary_learn_enabled = SettingsKey.DICTIONARY_LEARN_ENABLED.read
resolve_dictionary_steer_recognition = SettingsKey.DICTIONARY_STEER_RECOGNITION.read
resolve_snippets_enabled = SettingsKey.SNIPPETS_ENABLED.read
resolve_recording_hands_free_latch = SettingsKey.RECORDING_HANDS_FREE_LATCH.read
resolve_scratchpad_always_on_top = SettingsKey.SCRATCHPAD_ALWAYS_ON_TOP.read
# Automation switches.
resolve_voice_routing_enabled = SettingsKey.VOICE_ROUTING_ENABLED.read
resolve_voice_routing_on_dictation = SettingsKey.VOICE_ROUTING_ON_DICTATION.read
resolve_mcp_events_enabled = SettingsKey.MCP_EVENTS_ENABLED.read
resolve_walkthrough_show_details = SettingsKey.WALKTHROUGH_SHOW_DETAILS.read
resolve_walkthrough_show_about = SettingsKey.WALKTHROUGH_SHOW_ABOUT.read
resolve_walkthrough_show_captions = SettingsKey.WALKTHROUGH_SHOW_CAPTIONS.read
resolve_walkthrough_page_size = SettingsKey.WALKTHROUGH_PAGE_SIZE.read
resolve_walkthrough_copy_format = SettingsKey.WALKTHROUGH_COPY_FORMAT.read
resolve_walkthrough_intro_seen = SettingsKey.WALKTHROUGH_INTRO_SEEN.read
resolve_walkthrough_minimize_to_tray = SettingsKey.WALKTHROUGH_MINIMIZE_TO_TRAY.read
resolve_walkthrough_write_up = SettingsKey.WALKTHROUGH_WRITE_UP.read
resolve_walkthrough_write_up_agent = SettingsKey.WALKTHROUGH_WRITE_UP_AGENT.read
resolve_walkthrough_write_up_notes = SettingsKey.WALKTHROUGH_WRITE_UP_NOTES.read
resolve_walkthrough_snap_mode = SettingsKey.WALKTHROUGH_SNAP_MODE.read
resolve_walkthrough_auto_mode = SettingsKey.WALKTHROUGH_AUTO_MODE.read
resolve_walkthrough_keep_marks = SettingsKey.WALKTHROUGH_KEEP_MARKS.read
resolve_snap_to_edges = SettingsKey.SNAP_TO_EDGES.read
resolve_snap_box_infill = SettingsKey.SNAP_BOX_INFILL.read
# Seconds of quiet after an automatic click or drawing; zero keeps instant capture.
resolve_walkthrough_capture_delay = SettingsKey.WALKTHROUGH_CAPTURE_DELAY.read
# The walkthrough documents' heading, trimmed; "" leaves it out.
resolve_walkthrough_title = SettingsKey.WALKTHROUGH_TITLE.read
# The write-up's own instructions, trimmed; "" means the default ones.
resolve_walkthrough_write_up_instructions = SettingsKey.WALKTHROUGH_WRITE_UP_INSTRUCTIONS.read


def resolve_max_saved_recordings(
    settings: Optional[Mapping[str, Any]] = None,
) -> Optional[int]:
    """Return a positive retention count, or None when no count limit applies."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    mode = SettingsKey.RECORDING_RETENTION_MODE.read(settings)
    if mode in (RecordingRetentionMode.KEEP_ALL, RecordingRetentionMode.SIZE_LIMIT):
        return None
    return SettingsKey.MAX_SAVED_RECORDINGS.read(settings)


def resolve_max_saved_recordings_bytes(
    settings: Optional[Mapping[str, Any]] = None,
) -> Optional[int]:
    """Return the saved-recordings size cap in bytes, or None when no size limit applies."""
    if settings is None:
        settings = settings_manager.load_all_settings()

    if SettingsKey.RECORDING_RETENTION_MODE.read(settings) != RecordingRetentionMode.SIZE_LIMIT:
        return None
    return SettingsKey.MAX_SAVED_RECORDINGS_MB.read(settings) * 1024 * 1024


# A valid UI font-scale percent (UiFontScale.ALL), defaulting to 100.
resolve_ui_font_scale = SettingsKey.UI_FONT_SCALE.read
# The preview font size clamped to 10–48 points.
resolve_streaming_overlay_font_size = SettingsKey.STREAMING_OVERLAY_FONT_SIZE.read


def resolve_transcript_cleanup_prompt(
    settings: Optional[Mapping[str, Any]] = None,
) -> str:
    """The prompt standard cleanup starts from: the saved custom prompt, or
    the saved level's preset when there is none."""
    if settings is None:
        settings = settings_manager.load_all_settings()
    # cleanup_prompts imports this module.
    from services import cleanup_prompts

    return cleanup_prompts.base_prompt(
        settings, resolve_transcript_cleanup_level(settings)
    )


def _known_text_llm_profile_ids(
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[str, ...]:
    try:
        from services.text_llm import known_profile_ids

        return known_profile_ids(settings)
    except Exception:
        _log_fallback("Could not list text-model profiles; using the built-in ones")
        return TranscriptCleanupProvider.ALL


def default_transcript_cleanup_model(provider: str) -> str:
    """Return the provider default; custom endpoints have no default."""
    if provider in TranscriptCleanupProvider.INSTALLED:
        return ""
    try:
        from services.text_llm import default_model_for_profile, get_profile

        profile = get_profile(provider)
        if profile is not None:
            return default_model_for_profile(profile)
    except Exception:
        _log_fallback("Could not look up a text-model profile's default model; "
                      "using the built-in default")
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

    provider = SETTINGS_BY_KEY[provider_key].stored(settings)
    model = SETTINGS_BY_KEY[model_key].stored(settings)
    if (provider_key == SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER
            and provider in TranscriptCleanupProvider.INSTALLED
            and isinstance(model, str)):
        return provider, model.strip()
    known = _known_text_llm_profile_ids(settings)
    if (
        isinstance(provider, str)
        and provider in known
        and isinstance(model, str)
        and model.strip()
    ):
        return provider, model.strip()
    return SETTINGS_BY_KEY[provider_key].default, SETTINGS_BY_KEY[model_key].default


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
    """Resolve cleanup, replacing the retiring OpenAI default at read time."""
    provider, model = _resolve_text_llm_assignment(
        SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER,
        SettingsKey.TRANSCRIPT_CLEANUP_MODEL,
        settings,
    )
    if provider == TranscriptCleanupProvider.OPENAI and model == "gpt-4o-mini":
        return config.TRANSCRIPT_CLEANUP_MODEL
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

    raw = SettingsKey.TRANSCRIPT_CLEANUP_RULES.read(settings)
    rules = [r.strip() for r in raw if isinstance(r, str) and r.strip()]
    return rules[: config.MAX_TRANSCRIPT_CLEANUP_RULES]


# The remembered Custom description, stripped and capped.
resolve_transcript_batch_custom_instructions = (
    SettingsKey.TRANSCRIPT_BATCH_CUSTOM_INSTRUCTIONS.read)


@dataclass(frozen=True)
class MeetingSpeech:
    """Where a meeting's speech is transcribed, and with which model."""

    #: "local" or "remote" (the paired computer).
    source: str
    #: The local model. With a remote source the host's engine decides, and
    #: this is only what a local run of the meeting would use.
    model: str
    #: The meeting has an engine of its own rather than dictation's.
    separate: bool = False
    #: Why a meeting that follows dictation is not on the dictation engine.
    note: str = ""


def meeting_model_label(model: str) -> str:
    """How a meeting speech model reads in a sentence."""
    from services.local_asr.catalog import MODELS

    if model in MODELS:
        return MODELS[model].label
    return "Local Whisper" if model == "auto" else f"Local Whisper {model}"


def resolve_meeting_speech(settings: Optional[Dict[str, Any]] = None) -> MeetingSpeech:
    """The engine a meeting uses: dictation's, unless it was given its own.

    An engine that can't transcribe meetings (the API, or a model without
    the catalog's ``meeting`` flag) leaves a following meeting on the
    platform's default meeting model, with ``note`` saying why.
    """
    if settings is None:
        settings = settings_manager.load_all_settings()
    own = _own_meeting_model(settings)
    if SettingsKey.MEETING_SPEECH_SEPARATE.read(settings):
        remote = SettingsKey.MEETING_ASR_SOURCE.read(settings) == "remote"
        return MeetingSpeech("remote" if remote else "local", own, separate=True)

    from services.local_asr.catalog import BACKENDS, MODELS, selected_model

    # A pre-2.0 API model name reads as the API backend.
    engine = SettingsKey.SELECTED_MODEL.read(settings)
    if engine == "remote":
        return MeetingSpeech("remote", own)
    if engine == "local_whisper":
        from services.whisper_sources import custom_models

        model = SettingsKey.WHISPER_MODEL.read(settings)
        if model not in [*config.WHISPER_MODEL_CHOICES, *custom_models(settings)]:
            model = SettingsKey.WHISPER_MODEL.default
        return MeetingSpeech("local", model)
    if engine in BACKENDS:
        model = selected_model(engine, settings)
        if MODELS[model].meeting:
            return MeetingSpeech("local", model)
        name = MODELS[model].label
    else:
        name = "The API engine"
    fallback = (SettingsKey.MEETING_ASR_MODEL.default
                or SettingsKey.MEETING_WHISPER_MODEL.default)
    return MeetingSpeech(
        "local", fallback,
        note=f"{name} can't transcribe meetings, so meetings use {meeting_model_label(fallback)}.",
    )


def own_meeting_speech(settings: Optional[Dict[str, Any]] = None) -> MeetingSpeech:
    """The engine saved for meetings alone, whether or not it is in use."""
    if settings is None:
        settings = settings_manager.load_all_settings()
    return resolve_meeting_speech({**settings, SettingsKey.MEETING_SPEECH_SEPARATE: True})


def keep_separate_meeting_speech(manager: Optional["SettingsManager"] = None) -> bool:
    """Save whether an install's meetings have their own engine; True when written.

    Meetings used to have their own model and source always. An existing
    install whose saved choice differs from its dictation engine keeps it
    as a separate engine; every other install follows dictation. Decided
    once, so a later change of dictation engine can't turn a follower into
    a separate engine. Run after ``keep_unchosen_engine``.
    """
    manager = settings_manager if manager is None else manager
    is_new = getattr(manager, "is_new_install", None)
    if not callable(is_new):
        return False
    new = is_new()
    if SettingsKey.MEETING_SPEECH_SEPARATE in manager.load_all_settings():
        return False

    def keep(settings: Dict[str, Any]) -> bool:
        if SettingsKey.MEETING_SPEECH_SEPARATE in settings:
            return False
        own = own_meeting_speech(settings)
        follows = resolve_meeting_speech(settings)
        same = own.source == follows.source and (
            own.source == "remote" or own.model == follows.model
        )
        settings[SettingsKey.MEETING_SPEECH_SEPARATE] = not (new or same)
        return True

    try:
        return bool(manager.mutate_settings(keep))
    except Exception as exc:
        logger.warning("Could not save the install's meeting engine: %s", exc,
                       exc_info=True)
        return False


def resolve_meeting_asr_source(settings: Optional[Dict[str, Any]] = None) -> str:
    """Whether a meeting's speech runs here or on the paired computer."""
    return resolve_meeting_speech(settings).source


def resolve_meeting_whisper_model(
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """The local speech model a meeting uses."""
    return resolve_meeting_speech(settings).model


def _own_meeting_model(settings: Dict[str, Any]) -> str:
    """The model saved for meetings alone, then a compatible Parakeet default."""
    from services.local_asr.catalog import MODELS, selected_model
    # Only a saved choice counts here; with none, the defaults come last.
    extra = SettingsKey.MEETING_ASR_MODEL.stored(settings)
    if extra is not None and extra in MODELS and MODELS[extra].meeting:
        return extra
    model = SettingsKey.MEETING_WHISPER_MODEL.stored(settings)
    if model is not None:
        from services.whisper_sources import custom_models
        if model in [*config.WHISPER_MODEL_CHOICES, *custom_models(settings)]:
            return model
    from config import parakeet_default_supported

    # The stored value, not the validated one: a malformed selection has
    # never counted as Parakeet here.
    if (settings.get(SettingsKey.SELECTED_MODEL, SettingsKey.SELECTED_MODEL.default) == "parakeet"
            and (sys.platform == "darwin" or parakeet_default_supported())):
        return selected_model("parakeet", settings)
    return (SettingsKey.MEETING_ASR_MODEL.default
            or SettingsKey.MEETING_WHISPER_MODEL.default)


# ``auto`` or a supported Whisper language code.
resolve_meeting_language = SettingsKey.MEETING_LANGUAGE.read


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
) -> Any:
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
    from services.text_llm import snapshot_from_profile

    profile = resolve_meeting_llm_profile(settings)
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
    raw = SettingsKey.MEETING_AGENT_MODELS.read(settings)
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
    backend = SettingsKey.MEETING_SPEAKER_ID_BACKEND.read(settings)
    if backend == MeetingSpeakerIdBackend.OPENAI and openai_retirement.retired(today):
        return MeetingSpeakerIdBackend.LOCAL
    return backend


def resolve_meeting_linux_preview_ack(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether the current Linux Meeting Mode preview was accepted.

    Legacy booleans and stale/future versions deliberately fail closed. A
    changed disclosure must bump :data:`MEETING_LINUX_PREVIEW_ACK_VERSION` so
    users see it once before newly authorized capture behavior can start.
    """
    return (SettingsKey.MEETING_LINUX_PREVIEW_ACK_VERSION.read(settings)
            == MEETING_LINUX_PREVIEW_ACK_VERSION)


#: Names participants can use to address the note-taking assistant. The
#: bare word "whisper" is deliberately absent: it is ordinary speech.
DEFAULT_VOICE_COMMAND_NAMES: Final[Tuple[str, ...]] = (
    "note taker", "notetaker", "assistant", "openwhisper", "open whisper",
)


def resolve_typesafe_feature_enabled(
    feature: str, settings: Optional[Mapping[str, Any]] = None,
) -> bool:
    keys = {
        "citations": SettingsKey.TYPESAFE_CITATIONS_ENABLED,
        "semantic_search": SettingsKey.TYPESAFE_SEMANTIC_SEARCH_ENABLED,
        "question_radar": SettingsKey.TYPESAFE_QUESTION_RADAR_ENABLED,
        "highlights": SettingsKey.TYPESAFE_HIGHLIGHTS_ENABLED,
    }
    return resolve_typesafe_enabled(settings) and keys[feature].read(settings)


def resolve_typesafe_topic_shift_enabled(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether the checkpoint scheduler may ask TypeSafe about topic shifts.

    On by default once TypeSafe is enabled; the lexical Jaccard rule remains
    the fallback whenever a judgment is unavailable.
    """
    return (resolve_typesafe_enabled(settings)
            and SettingsKey.TYPESAFE_TOPIC_SHIFT_ENABLED.read(settings))


def resolve_typesafe_voice_commands_enabled(
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether spoken instructions to the note taker are acted on.

    Off by default. Only segments that name the assistant are ever judged.
    """
    return (resolve_typesafe_enabled(settings)
            and SettingsKey.TYPESAFE_VOICE_COMMANDS_ENABLED.read(settings))


def resolve_typesafe_voice_command_names(
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[str, ...]:
    """Return the lowercased wake names for voice commands, defaults when unset."""
    raw = SettingsKey.TYPESAFE_VOICE_COMMAND_NAMES.read(settings)
    if raw is None:
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
    cleaned = SettingsKey.MEETING_CONTEXT_FOLDER_PATH.read(settings).strip()
    if not cleaned:
        return ""
    expanded = os.path.expanduser(cleaned)
    if not os.path.isabs(expanded):
        expanded = os.path.abspath(expanded)
    return os.path.normpath(expanded)


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
        if key.read(settings)
    )
    return views or ("ribbon",)


# The dashboard port clamped to 0–65535.
resolve_meeting_server_port = SettingsKey.MEETING_SERVER_PORT.read


def compose_transcript_cleanup_prompt(base_prompt: str, rules: List[str]) -> str:
    """Append validated learned rules to the base prompt."""
    if not rules:
        return base_prompt
    numbered = "\n".join(f"{i}. {rule}" for i, rule in enumerate(rules, start=1))
    return (
        f"{base_prompt}\n\n"
        f"Additional user-taught rules (always apply):\n{numbered}"
    )


#: The consent text version a meeting insight review was agreed under
#: (``meeting.state.review.REVIEW_CONSENT``; a test keeps the two equal).
INSIGHT_REVIEW_CONSENT: Final[str] = "typesafe-text-v1"


def resolve_meeting_insight_review(
    settings: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if settings is None:
        settings = settings_manager.load_all_settings()
    enabled = SettingsKey.MEETING_INSIGHT_REVIEW.read(settings)
    consent = SettingsKey.MEETING_INSIGHT_REVIEW_CONSENT.read(settings) == INSIGHT_REVIEW_CONSENT
    return {
        "enabled": enabled and consent and resolve_typesafe_enabled(settings),
        "consent": INSIGHT_REVIEW_CONSENT if consent else "",
        "sensitivity": SettingsKey.MEETING_INSIGHT_REVIEW_SENSITIVITY.read(settings),
    }
