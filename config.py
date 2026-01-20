"""
Configuration constants for the OpenWhisper application.
"""
from dataclasses import dataclass
from typing import Dict, List, Tuple
import numpy as np


@dataclass
class AppConfig:
    """Centralized configuration for the OpenWhisper application."""
    
    # File paths
    SETTINGS_FILE: str = "openwhisper_settings.json"
    RECORDED_AUDIO_FILE: str = "recorded_audio.wav"
    LOG_FILE: str = "openwhisper.log"
    ENV_FILE: str = ".env"
    
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

    # UI settings
    MAIN_WINDOW_SIZE: str = "300x200"
    LOADING_WINDOW_SIZE: str = "300x300"
    HOTKEY_DIALOG_SIZE: str = "400x300"
    OVERLAY_SIZE: str = "200x30"
    
    # Waveform overlay settings
    WAVEFORM_OVERLAY_WIDTH: int = 300
    WAVEFORM_OVERLAY_HEIGHT: int = 80
    WAVEFORM_BAR_COUNT: int = 20
    WAVEFORM_BAR_WIDTH: int = 8
    WAVEFORM_BAR_SPACING: int = 2
    WAVEFORM_FRAME_RATE: int = 30
    WAVEFORM_LEVEL_SMOOTHING: float = 0.7
    
    # Waveform colors (hex format)
    WAVEFORM_BG_COLOR: str = "#1a1a1a"
    WAVEFORM_ACCENT_COLOR: str = "#00d4ff"
    WAVEFORM_SECONDARY_COLOR: str = "#0099cc"
    WAVEFORM_TEXT_COLOR: str = "#ffffff"

    # Streaming text overlay settings
    STREAMING_OVERLAY_WIDTH: int = 450
    STREAMING_OVERLAY_MIN_HEIGHT: int = 100
    STREAMING_OVERLAY_MAX_HEIGHT: int = 300
    STREAMING_OVERLAY_FONT_SIZE: int = 12

    # Timing settings
    HOTKEY_DEBOUNCE_MS: int = 300
    OVERLAY_HIDE_DELAY_MS: int = 1500
    CANCELLATION_ANIMATION_DURATION_MS: int = 800
    PROGRESS_BAR_INTERVAL_MS: int = 10
    # Continue capturing this many ms after stop to avoid end cut-offs
    POST_ROLL_MS: int = 1200
    # How long to wait for the recorder thread to flush post-roll frames before saving
    POST_ROLL_FINALIZE_GRACE_MS: int = 800
    # Extra silence appended to the end of saved audio so ASR models don't drop the last word
    END_PADDING_MS: int = 500
    
    # Audio splitting settings
    MAX_FILE_SIZE_MB: int = 23  # Maximum file size before splitting
    SILENCE_THRESHOLD: float = 0.01  # Volume threshold to detect silence
    MIN_CHUNK_DURATION_SEC: int = 30  # Minimum duration for each chunk in seconds
    SILENCE_DURATION_SEC: float = 0.5  # Duration of silence needed for split point
    OVERLAP_DURATION_SEC: float = 2.0  # Overlap between chunks to avoid word cutoffs
    
    # Whisper model - "auto" selects based on hardware (turbo for GPU, base for CPU)
    DEFAULT_WHISPER_MODEL: str = "auto"

    # Faster-whisper settings
    FASTER_WHISPER_DEVICE: str = "auto"  # "auto", "cuda", "cpu"
    FASTER_WHISPER_COMPUTE_TYPE: str = "auto"  # "auto", "float16", "int8", "float32"
    FASTER_WHISPER_VAD_ENABLED: bool = True
    FASTER_WHISPER_VAD_MIN_SILENCE_MS: int = 500
    FASTER_WHISPER_BEAM_SIZE: int = 5

    # Streaming transcription settings
    STREAMING_ENABLED: bool = False  # Opt-in feature for real-time transcription
    STREAMING_CHUNK_DURATION_SEC: float = 3.0  # Process every N seconds
    STREAMING_QUEUE_SIZE: int = 10  # Maximum queued chunks (prevents memory issues)
    STREAMING_BEAM_SIZE: int = 3  # Smaller beam size for faster processing

    # Meeting mode settings
    MEETINGS_FILE: str = "meetings.json"
    MEETING_AUDIO_FOLDER: str = "meeting_audio"
    MEETING_CHUNK_DURATION_SEC: float = 30.0  # Larger chunks for better quality
    MEETING_QUEUE_SIZE: int = 20  # Larger queue for long recordings

    # Meeting recording settings (complete WAV recordings)
    MEETING_RECORDINGS_FOLDER: str = "meeting_recordings"
    MAX_MEETING_RECORDINGS: int = 10  # ~3GB at 1hr/meeting (44.1kHz 16-bit mono ~5.3MB/min)

    # Meeting Insights settings
    INSIGHTS_PROVIDERS: List[str] = None  # ["openai", "openrouter"]
    DEFAULT_INSIGHTS_PROVIDER: str = "openai"
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    DEFAULT_OPENAI_INSIGHTS_MODEL: str = "gpt-4o"
    DEFAULT_OPENROUTER_INSIGHTS_MODEL: str = "anthropic/claude-3.5-sonnet"
    INSIGHTS_MAX_TOKENS: int = 3000
    INSIGHTS_TEMPERATURE: float = 0.5

    # Waveform style settings
    CURRENT_WAVEFORM_STYLE: str = "particle"
    WAVEFORM_STYLE_CONFIGS: Dict[str, Dict] = None
    
    def __post_init__(self):
        """Initialize computed fields after dataclass creation."""
        if self.DEFAULT_HOTKEYS is None:
            self.DEFAULT_HOTKEYS = {
                'record_toggle': 'kp *',
                'cancel': 'kp -',
                'enable_disable': 'ctrl+alt+kp *'
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

        if self.INSIGHTS_PROVIDERS is None:
            self.INSIGHTS_PROVIDERS = ["openai", "openrouter"]


# Global config instance
config = AppConfig() 
