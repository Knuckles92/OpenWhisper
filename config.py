"""OpenWhisper configuration constants."""
import os
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Dict, List, Tuple

from _version import __version__

try:
    import numpy as np
except ImportError:  # pragma: no cover - lightweight fallback for test/import environments
    np = SimpleNamespace(int16="int16")


APP_NAME = "OpenWhisper"
ENV_FILE_NAME = ".env"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle rather than source."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def bundle_root() -> str:
    """Directory holding bundled read-only assets (stylesheets, icons).

    Under PyInstaller this is the extraction dir (``sys._MEIPASS``); from
    source it is the repository root.
    """
    if is_frozen():
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def local_app_dir() -> str:
    """Per-user application directory, e.g. ``%LOCALAPPDATA%\\OpenWhisper``.

    Always absolute, regardless of whether the app is frozen.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get(
            "XDG_DATA_HOME", os.path.expanduser("~/.local/share")
        )
    return os.path.join(base, APP_NAME)


def data_root() -> str:
    """Directory for writable user data.

    Frozen builds install to ``%LOCALAPPDATA%\\Programs\\OpenWhisper``, which
    must be treated as read-only, so user data goes to
    ``%LOCALAPPDATA%\\OpenWhisper`` instead. Running from source returns ``""``
    so every path stays CWD-relative exactly as before — developer workflows
    and the test suite are unaffected.
    """
    if not is_frozen():
        return ""

    root = local_app_dir()
    os.makedirs(root, exist_ok=True)
    return root


def components_root() -> str:
    """Directory holding downloaded components.

    Unlike settings, this is always an absolute per-user path — components are
    multi-gigabyte binaries that should not be duplicated per source checkout,
    and they deliberately live outside the install directory so upgrading the
    application does not delete them.
    """
    return os.path.join(local_app_dir(), "components")


def user_data_path(filename: str) -> str:
    """Resolve ``filename`` against the writable user-data root.

    Returns the bare filename when running from source, preserving the
    historical CWD-relative behavior.
    """
    root = data_root()
    return os.path.join(root, filename) if root else filename


def env_file_path() -> str:
    """Locate the optional ``.env`` file holding API keys.

    Frozen builds look in the user-data root first, because the bundle
    directory is read-only and a user-supplied ``.env`` cannot live there.
    Falls back to the repository root so source checkouts keep working.
    """
    candidates = []
    root = data_root()
    if root:
        candidates.append(os.path.join(root, ENV_FILE_NAME))
    candidates.append(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ENV_FILE_NAME)
    )

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0]


@dataclass
class AppConfig:
    """Centralized configuration for the OpenWhisper application."""

    # Application version (see _version.py)
    VERSION: str = __version__

    # File paths. These resolve under %LOCALAPPDATA%\OpenWhisper in frozen
    # builds and stay CWD-relative when running from source.
    SETTINGS_FILE: str = field(
        default_factory=lambda: user_data_path("openwhisper_settings.json")
    )
    RECORDED_AUDIO_FILE: str = field(
        default_factory=lambda: user_data_path("recorded_audio.wav")
    )
    LOG_FILE: str = field(default_factory=lambda: user_data_path("openwhisper.log"))

    # Logging configuration
    LOG_LEVEL: str = os.environ.get("OPENWHISPER_LOG_LEVEL", "INFO").upper()
    LOG_FORMAT: str = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    LOG_MAX_BYTES: int = 5 * 1024 * 1024
    LOG_BACKUP_COUNT: int = 3

    # History and recordings
    HISTORY_FILE: str = field(
        default_factory=lambda: user_data_path("transcription_history.json")
    )
    RECORDINGS_FOLDER: str = field(
        default_factory=lambda: user_data_path("recordings")
    )
    MAX_SAVED_RECORDINGS: int = 20
    DATABASE_FILE: str = field(
        default_factory=lambda: user_data_path("openwhisper.db")
    )

    # Audio settings
    CHUNK_SIZE: int = 1024
    AUDIO_FORMAT: type = np.int16
    CHANNELS: int = 1
    SAMPLE_RATE: int = 44100

    # Default hotkeys
    DEFAULT_HOTKEYS: Dict[str, str] = None

    # Model configurations
    MODEL_CHOICES: Tuple[str, ...] = (
        'Local Whisper',
        'API: Whisper',
        'API: GPT-4o Transcribe',
        'API: GPT-4o Mini Transcribe'
    )

    MODEL_VALUE_MAP: Dict[str, str] = None

    # Whisper model choices for faster-whisper
    WHISPER_MODEL_CHOICES: List[str] = None

    # Main window sizing
    # The styled three-tab bar asks for about 574px before its 48px container
    # margins and the collapsed history edge. Keep enough room for that full
    # layout so labels and footer controls cannot collide or clip.
    MAIN_WINDOW_MIN_WIDTH: int = 650
    # Leave enough headroom for the always-visible engine fields under Windows
    # font metrics and display scaling. Saved/manual geometry is clamped to this
    # floor so the front-page scroller never appears in the normal full UI.
    MAIN_WINDOW_MIN_HEIGHT: int = 580
    MAIN_WINDOW_DEFAULT_WIDTH: int = 680
    # Sized so neither transcription tab needs a scrollbar at rest while still
    # keeping the collapsed workspace compact.
    MAIN_WINDOW_DEFAULT_HEIGHT: int = 600
    MAIN_WINDOW_TRANSCRIPTION_EXPAND_HEIGHT: int = 840
    MAIN_WINDOW_HISTORY_SIDEBAR_WIDTH: int = 380
    MAIN_WINDOW_HISTORY_EDGE_TAB_WIDTH: int = 24
    MAIN_WINDOW_MAX_WIDTH: int = 1280
    # A collapsed transcript should reopen at the compact full-window height,
    # even when the last saved geometry came from an expanded transcript.
    MAIN_WINDOW_COLLAPSED_RESTORE_MAX_HEIGHT: int = MAIN_WINDOW_DEFAULT_HEIGHT
    MAIN_WINDOW_COMPACT_WIDTH: int = 420
    MAIN_WINDOW_COMPACT_HEIGHT: int = 250

    # Waveform overlay settings
    WAVEFORM_OVERLAY_WIDTH: int = 300
    WAVEFORM_OVERLAY_HEIGHT: int = 80
    WAVEFORM_STREAMING_MAX_HEIGHT: int = 400
    WAVEFORM_FRAME_RATE: int = 30
    WAVEFORM_LEVEL_SMOOTHING: float = 0.7

    # Streaming text overlay settings
    STREAMING_OVERLAY_FONT_SIZE: int = 16

    # Timing settings
    HOTKEY_DEBOUNCE_MS: int = 300
    # Shortest push-and-hold that counts as a recording; shorter holds cancel.
    RECORD_MIN_HOLD_MS: int = 250
    OVERLAY_HIDE_DELAY_MS: int = 1500
    CANCELLATION_ANIMATION_DURATION_MS: int = 800
    CANCELLATION_GRACE_MS: int = 200
    POST_ROLL_MS: int = 1200
    POST_ROLL_FINALIZE_GRACE_MS: int = 800
    END_PADDING_MS: int = 500
    # Debounce for whisper-engine reloads triggered by the inline main-GUI
    # controls; coalesces rapid model/device/quant changes into one reload.
    WHISPER_RELOAD_DEBOUNCE_MS: int = 400
    HOTKEY_WATCHDOG_INTERVAL_MS: int = 10_000
    HOTKEY_SLEEP_GAP_THRESHOLD_SEC: float = 30.0
    HOTKEY_HOOK_REFRESH_INTERVAL_MS: int = 5 * 60 * 1000
    WHISPER_TARGET_SAMPLE_RATE: int = 16000

    # Record hotkey activation: "toggle" (press to start, press again to stop)
    # or "push_hold" (hold to record, release to stop and transcribe).
    RECORDING_TRIGGER_MODE: str = "toggle"

    # Audio splitting settings
    MAX_FILE_SIZE_MB: int = 23
    SILENCE_THRESHOLD: float = 0.01
    MIN_CHUNK_DURATION_SEC: int = 30
    SILENCE_DURATION_SEC: float = 0.5
    OVERLAP_DURATION_SEC: float = 2.0

    # Whisper model - "auto" selects based on hardware (turbo for GPU, base for
    # CPU). On macOS there is no CUDA, so "auto" resolves to CPU (base model).
    DEFAULT_WHISPER_MODEL: str = "auto"

    # Faster-whisper settings. CUDA is unavailable on macOS (faster-whisper has
    # no MPS/Metal backend), so "auto" runs on CPU there.
    FASTER_WHISPER_DEVICE: str = "auto"
    FASTER_WHISPER_COMPUTE_TYPE: str = "auto"
    FASTER_WHISPER_VAD_ENABLED: bool = True
    FASTER_WHISPER_VAD_MIN_SILENCE_MS: int = 500
    FASTER_WHISPER_BEAM_SIZE: int = 5

    # Streaming transcription settings
    STREAMING_ENABLED: bool = False
    STREAMING_CHUNK_DURATION_SEC: float = 3.0
    STREAMING_OVERLAP_SEC: float = 0.75
    STREAMING_QUEUE_SIZE: int = 10

    # Post-ASR transcript cleanup (OpenAI, OpenRouter, or a custom endpoint)
    TRANSCRIPT_CLEANUP_ENABLED: bool = False
    TRANSCRIPT_CLEANUP_TIMEOUT_S: float = 8.0
    TRANSCRIPT_CLEANUP_PROVIDER: str = "openrouter"
    TRANSCRIPT_CLEANUP_MODEL: str = "gpt-4o-mini"
    TRANSCRIPT_CLEANUP_OPENROUTER_MODEL: str = "openrouter/free"
    # Model-list ordering in Model Manager. "alphabetical" sorts client-side;
    # other values are OpenRouter /models sort params.
    TRANSCRIPT_CLEANUP_MODEL_SORT: str = "alphabetical"
    TRANSCRIPT_CLEANUP_REASONING: str = "off"
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    TRANSCRIPT_CLEANUP_PROMPT: str = (
        "You clean up speech-to-text transcripts. "
        "Fix punctuation and capitalization, remove filler words "
        "(um, uh, like as filler, you know), and fix obvious ASR errors. "
        "Do not invent content, do not add information that was not spoken, "
        "and preserve meaning, tone, and proper nouns. "
        "Return only the cleaned transcript text with no preamble or quotes."
    )
    # Learned cleanup rules (user-taught behaviors appended to the base prompt)
    MAX_TRANSCRIPT_CLEANUP_RULES: int = 50
    TRANSCRIPT_CLEANUP_RULE_POLISH_PROMPT: str = (
        "You convert a user's instruction into one short rule for an AI that "
        "cleans up speech-to-text transcripts. Rewrite the instruction as a "
        "single, clear, imperative directive. Preserve every specific detail "
        "exactly - names, spellings, capitalization, abbreviations, "
        "expansions, and formatting requests. Do not add behaviors the user "
        "did not ask for, do not generalize, and do not explain. If the "
        "instruction contains several related behaviors, join them into one "
        "rule with semicolons. Return only the rule text with no numbering, "
        "quotes, or preamble.\n\n"
        "Example input: so um whenever I say my name it should be spelled "
        "A L E X R I V E R A\n"
        'Example output: Always spell the user\'s name "Alex Rivera".'
    )

    # Developer tools (Settings → Advanced). Off for normal use.
    DEVELOPER_MODE: bool = False

    # In-app updater. Automatic GitHub metadata checks and the
    # update-available dialog are on until the user opts out.
    UPDATE_CHECK_ENABLED: bool = True
    UPDATE_NOTIFY_ENABLED: bool = True
    UPDATE_CHECK_INTERVAL_S: int = 24 * 60 * 60
    UPDATE_CHECK_DELAY_MS: int = 8_000

    # Meeting Mode defaults
    MEETING_WHISPER_MODEL: str = "auto"
    MEETING_LANGUAGE: str = "auto"
    MEETING_LLM_PROVIDER: str = "openrouter"
    MEETING_LLM_MODEL: str = "openrouter/free"
    MEETING_AGENT_CORE: str = "pi"
    MEETING_SPEAKER_ID_BACKEND: str = "local"
    MEETING_END_REDECODE: bool = False
    MEETING_END_POLISH: bool = True
    MEETING_END_REPORT: bool = True
    MEETING_REPORT_RIBBON: bool = True
    MEETING_REPORT_BRIEF: bool = True
    MEETING_REPORT_SIGNAL: bool = True
    MEETING_SERVER_BIND: str = "localhost"
    MEETING_SERVER_PORT: int = 0
    MEETINGS_FOLDER: str = field(
        default_factory=lambda: user_data_path("meetings")
    )

    # Waveform style settings
    WAVEFORM_STYLE_CONFIGS: Dict[str, Dict] = None

    def __post_init__(self):
        if self.DEFAULT_HOTKEYS is None:
            if sys.platform == "darwin":
                # Control+Option combos avoid macOS system shortcuts (Spotlight,
                # input sources, emoji picker) and common app defaults such as
                # 1Password Quick Access (Cmd+Shift+Space). Modifiers: cmd, ctrl,
                # alt (option), shift. Numpad keys are unreliable on Mac laptops.
                self.DEFAULT_HOTKEYS = {
                    'record_toggle': 'ctrl+alt+r',
                    'cancel': 'ctrl+alt+escape',
                    'enable_disable': 'ctrl+alt+shift+r',
                    'minimize_tray': 'ctrl+alt+m',
                    'meeting_toggle': '',
                }
            else:
                self.DEFAULT_HOTKEYS = {
                    'record_toggle': 'kp *',
                    'cancel': 'kp -',
                    'enable_disable': 'ctrl+alt+kp *',
                    'minimize_tray': 'ctrl+alt+m',
                    'meeting_toggle': '',
                }

        if self.MODEL_VALUE_MAP is None:
            self.MODEL_VALUE_MAP = {
                'Local Whisper': 'local_whisper',
                'API: Whisper': 'api_whisper',
                'API: GPT-4o Transcribe': 'api_gpt4o',
                'API: GPT-4o Mini Transcribe': 'api_gpt4o_mini'
            }

        if self.WHISPER_MODEL_CHOICES is None:
            self.WHISPER_MODEL_CHOICES = [
                "auto",
                "tiny", "tiny.en",
                "base", "base.en",
                "small", "small.en",
                "medium", "medium.en",
                "large-v1", "large-v2", "large-v3",
                "turbo",
                "distil-small.en", "distil-medium.en",
                "distil-large-v2", "distil-large-v3"
            ]

        if self.WAVEFORM_STYLE_CONFIGS is None:
            self.WAVEFORM_STYLE_CONFIGS = {
                'particle': {
                    'max_particles': 150,
                    'emission_rate': 30,
                    'particle_life': 2.0,
                    'gravity': 20,
                    'damping': 0.98,
                    'wind_strength': 5,
                    'audio_response': 1.5,
                    'bg_color': '#0a0a0a',
                    'text_color': '#ffffff',
                    'glow_effect': True,
                    'turbulence_strength': 10,
                    'color_shift_speed': 50
                }
            }
config = AppConfig()
