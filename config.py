"""
Configuration constants for the OpenWhisper application.
"""
import os
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Dict, List, Tuple

try:
    import numpy as np
except ImportError:  # pragma: no cover - lightweight fallback for test/import environments
    np = SimpleNamespace(int16="int16")


@dataclass
class AppConfig:
    """Centralized configuration for the OpenWhisper application."""

    # File paths
    SETTINGS_FILE: str = "openwhisper_settings.json"
    RECORDED_AUDIO_FILE: str = "recorded_audio.wav"
    LOG_FILE: str = "openwhisper.log"
    ENV_FILE: str = ".env"

    # Logging configuration
    LOG_LEVEL: str = os.environ.get("OPENWHISPER_LOG_LEVEL", "INFO").upper()
    LOG_FORMAT: str = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    LOG_MAX_BYTES: int = 5 * 1024 * 1024
    LOG_BACKUP_COUNT: int = 3

    # History and recordings
    HISTORY_FILE: str = "transcription_history.json"
    RECORDINGS_FOLDER: str = "recordings"
    MAX_SAVED_RECORDINGS: int = 3
    DATABASE_FILE: str = "openwhisper.db"

    # Audio settings
    CHUNK_SIZE: int = 1024
    AUDIO_FORMAT: type = np.int16  # NumPy dtype for audio format
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
    MAIN_WINDOW_MIN_WIDTH: int = 500
    # Lowered so the window can shrink smoothly once the transcription box is
    # collapsed; the layout's own minimum still governs the expanded state.
    MAIN_WINDOW_MIN_HEIGHT: int = 460
    MAIN_WINDOW_DEFAULT_WIDTH: int = 605
    # Compact height for the collapsed transcription + local-engine layout.
    MAIN_WINDOW_DEFAULT_HEIGHT: int = 580
    # Target height when the user expands the transcription panel.
    MAIN_WINDOW_TRANSCRIPTION_EXPAND_HEIGHT: int = 840
    MAIN_WINDOW_HISTORY_SIDEBAR_WIDTH: int = 380
    MAIN_WINDOW_HISTORY_EDGE_TAB_WIDTH: int = 24
    MAIN_WINDOW_MAX_WIDTH: int = 1280
    MAIN_WINDOW_COLLAPSED_RESTORE_MAX_HEIGHT: int = 840

    # Waveform overlay settings
    WAVEFORM_OVERLAY_WIDTH: int = 300
    WAVEFORM_OVERLAY_HEIGHT: int = 80
    WAVEFORM_STREAMING_MAX_HEIGHT: int = 400  # Soft cap; also limited by screen space near cursor
    WAVEFORM_FRAME_RATE: int = 30
    WAVEFORM_LEVEL_SMOOTHING: float = 0.7

    # Streaming text overlay settings
    STREAMING_OVERLAY_WIDTH: int = 450
    STREAMING_OVERLAY_MIN_HEIGHT: int = 100
    STREAMING_OVERLAY_MAX_HEIGHT: int = 300
    STREAMING_OVERLAY_FONT_SIZE: int = 12

    # Timing settings
    HOTKEY_DEBOUNCE_MS: int = 300
    OVERLAY_HIDE_DELAY_MS: int = 1500
    CANCELLATION_ANIMATION_DURATION_MS: int = 800
    CANCELLATION_GRACE_MS: int = 200  # Extra delay after cancel animation before hiding overlay
    PROGRESS_BAR_INTERVAL_MS: int = 10
    # Continue capturing this many ms after stop to avoid end cut-offs
    POST_ROLL_MS: int = 1200
    # How long to wait for the recorder thread to flush post-roll frames before saving
    POST_ROLL_FINALIZE_GRACE_MS: int = 800
    # Extra silence appended to the end of saved audio so ASR models don't drop the last word
    END_PADDING_MS: int = 500
    # Debounce for whisper-engine reloads triggered by the inline main-GUI
    # controls; coalesces rapid model/device/quant changes into one reload.
    WHISPER_RELOAD_DEBOUNCE_MS: int = 400
    # Hotkey watchdog: detects sleep/resume gaps; periodic refresh re-registers the hook
    HOTKEY_WATCHDOG_INTERVAL_MS: int = 10_000
    HOTKEY_SLEEP_GAP_THRESHOLD_SEC: float = 30.0
    HOTKEY_HOOK_REFRESH_INTERVAL_MS: int = 5 * 60 * 1000
    # Whisper expects 16 kHz audio regardless of recorder sample rate
    WHISPER_TARGET_SAMPLE_RATE: int = 16000

    # Audio splitting settings
    MAX_FILE_SIZE_MB: int = 23  # Maximum file size before splitting
    SILENCE_THRESHOLD: float = 0.01  # Volume threshold to detect silence
    MIN_CHUNK_DURATION_SEC: int = 30  # Minimum duration for each chunk in seconds
    SILENCE_DURATION_SEC: float = 0.5  # Duration of silence needed for split point
    OVERLAP_DURATION_SEC: float = 2.0  # Overlap between chunks to avoid word cutoffs

    # Whisper model - "auto" selects based on hardware (turbo for GPU, base for
    # CPU). On macOS there is no CUDA, so "auto" resolves to CPU (base model).
    DEFAULT_WHISPER_MODEL: str = "auto"

    # Faster-whisper settings. CUDA is unavailable on macOS (faster-whisper has
    # no MPS/Metal backend), so "auto" runs on CPU there.
    FASTER_WHISPER_DEVICE: str = "auto"  # "auto", "cuda", "cpu" (cuda N/A on macOS)
    FASTER_WHISPER_COMPUTE_TYPE: str = "auto"  # "auto", "float16", "int8", "float32" (float16 needs GPU)
    FASTER_WHISPER_VAD_ENABLED: bool = True
    FASTER_WHISPER_VAD_MIN_SILENCE_MS: int = 500
    FASTER_WHISPER_BEAM_SIZE: int = 5

    # Streaming transcription settings
    STREAMING_ENABLED: bool = False  # Opt-in feature for real-time transcription
    STREAMING_CHUNK_DURATION_SEC: float = 3.0  # Process every N seconds of new audio
    STREAMING_OVERLAP_SEC: float = 0.75  # Overlap with previous chunk for word boundaries
    STREAMING_QUEUE_SIZE: int = 10  # Maximum queued chunks (prevents memory issues)
    STREAMING_BEAM_SIZE: int = 1  # Preview-only; keep beam tiny for speed

    # Waveform style settings
    CURRENT_WAVEFORM_STYLE: str = "particle"
    WAVEFORM_STYLE_CONFIGS: Dict[str, Dict] = None

    def __post_init__(self):
        """Initialize computed fields after dataclass creation."""
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
                    'minimize_tray': 'ctrl+alt+m'
                }
            else:
                self.DEFAULT_HOTKEYS = {
                    'record_toggle': 'kp *',
                    'cancel': 'kp -',
                    'enable_disable': 'ctrl+alt+kp *',
                    'minimize_tray': 'ctrl+alt+m'
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
                # Auto-select based on hardware (turbo for GPU, base for CPU)
                "auto",
                # Standard models
                "tiny", "tiny.en",
                "base", "base.en",
                "small", "small.en",
                "medium", "medium.en",
                "large-v1", "large-v2", "large-v3",
                "turbo",
                # Distil models (faster, English-focused)
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
                    'particle_trail': True,
                    'glow_effect': True,
                    'turbulence_strength': 10,
                    'color_shift_speed': 50
                }
            }

# Global config instance
config = AppConfig()
