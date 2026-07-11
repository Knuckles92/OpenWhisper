"""
Settings management for the OpenWhisper application.
"""
import json
import os
import logging
import threading
from typing import Dict, Any, Final, Tuple, Optional
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
    MINIMIZE_TRAY: Final[str] = "minimize_tray"
    STREAMING_ENABLED: Final[str] = "streaming_enabled"
    STREAMING_CHUNK_DURATION: Final[str] = "streaming_chunk_duration"
    # Legacy keys kept for reading/migrating older settings files
    STREAMING_OVERLAY_ENABLED: Final[str] = "streaming_overlay_enabled"
    STREAMING_PASTE_ENABLED: Final[str] = "streaming_paste_enabled"
    WHISPER_MODEL: Final[str] = "whisper_model"
    WHISPER_DEVICE: Final[str] = "whisper_device"
    WHISPER_COMPUTE_TYPE: Final[str] = "whisper_compute_type"
    HF_HUB_OFFLINE: Final[str] = "hf_hub_offline"
    LAST_TAB_INDEX: Final[str] = "last_tab_index"
    # Recording retention: "keep_all" or "custom" (+ max_saved_recordings count)
    RECORDING_RETENTION_MODE: Final[str] = "recording_retention_mode"
    MAX_SAVED_RECORDINGS: Final[str] = "max_saved_recordings"


class RecordingRetentionMode:
    """Values for ``SettingsKey.RECORDING_RETENTION_MODE``."""
    KEEP_ALL: Final[str] = "keep_all"
    CUSTOM: Final[str] = "custom"


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
    """Return whether ``HF_HUB_OFFLINE`` is already set in the process env."""
    return os.environ.get(_HF_HUB_OFFLINE_ENV, "").strip().lower() in _HF_HUB_OFFLINE_TRUTHY


def apply_hf_hub_offline(enabled: bool) -> None:
    """Apply or clear the ``HF_HUB_OFFLINE`` environment variable for this process.

    Args:
        enabled: When True, set ``HF_HUB_OFFLINE=1``. When False, remove it so
            HuggingFace Hub checks are allowed again in this process.
    """
    if enabled:
        os.environ[_HF_HUB_OFFLINE_ENV] = "1"
        logger.info("HuggingFace Hub offline mode enabled (HF_HUB_OFFLINE=1)")
    else:
        removed = os.environ.pop(_HF_HUB_OFFLINE_ENV, None)
        if removed is not None:
            logger.info("HuggingFace Hub offline mode disabled")


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


def is_hf_hub_offline_enabled(settings: Optional[Dict[str, Any]] = None) -> bool:
    """Return whether HuggingFace Hub network checks should be skipped.

    True when the Settings toggle is on, or when ``HF_HUB_OFFLINE`` is already
    set in the environment (CLI / shell override).

    Args:
        settings: Optional loaded settings dict. Loads from disk when omitted.

    Returns:
        True when model loads should use local files only.
    """
    if is_hf_hub_offline_env_set():
        return True
    if settings is None:
        settings = settings_manager.load_all_settings()
    return bool(settings.get(SettingsKey.HF_HUB_OFFLINE, False))


def apply_hf_hub_offline_from_settings() -> bool:
    """Enable offline Hub mode from saved settings if the toggle is on.

    Does not clear a pre-existing ``HF_HUB_OFFLINE`` env var when the setting is
    off, so shell/CLI overrides still work at startup.

    Returns:
        True when offline mode is active after applying settings.
    """
    settings = settings_manager.load_all_settings()
    enabled = bool(settings.get(SettingsKey.HF_HUB_OFFLINE, False))
    if enabled:
        apply_hf_hub_offline(True)
    return is_hf_hub_offline_enabled(settings)
