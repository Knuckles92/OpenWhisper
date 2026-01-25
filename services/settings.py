"""
Settings management for the OpenWhisper application.
"""
import json
import os
import logging
import threading
from typing import Dict, Any, Tuple, Optional, List
from config import config


class SettingsManager:
    """Handles loading and saving application settings."""
    
    def __init__(self, settings_file: str = None):
        """Initialize the settings manager.
        
        Args:
            settings_file: Path to settings file. Uses config default if None.
        """
        self.settings_file = settings_file or config.SETTINGS_FILE
        self._lock = threading.Lock()
    
    def load_hotkey_settings(self) -> Dict[str, str]:
        """Load hotkey settings from file, return defaults if file doesn't exist.
        
        Returns:
            Dictionary of hotkey mappings.
        """
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r') as f:
                    settings = json.load(f)
                    return settings.get('hotkeys', config.DEFAULT_HOTKEYS)
        except Exception as e:
            logging.warning(f"Failed to load settings: {e}")
        
        return config.DEFAULT_HOTKEYS.copy()
    
    def save_hotkey_settings(self, hotkeys: Dict[str, str]) -> None:
        """Save hotkey settings to file.
        
        Args:
            hotkeys: Dictionary of hotkey mappings to save.
            
        Raises:
            Exception: If saving fails.
        """
        try:
            # Load existing settings first to preserve other settings
            settings = self.load_all_settings()
            settings['hotkeys'] = hotkeys  # Update only hotkeys
            with open(self.settings_file, 'w') as f:
                json.dump(settings, f, indent=2)
            logging.info("Hotkey settings saved successfully")
        except Exception as e:
            logging.error(f"Failed to save settings: {e}")
            raise
    
    def load_all_settings(self) -> Dict[str, Any]:
        """Load all settings from file.
        
        Returns:
            Dictionary containing all settings.
        """
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logging.warning(f"Failed to load all settings: {e}")
        
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
            logging.info("All settings saved successfully")
        except Exception as e:
            logging.error(f"Failed to save all settings: {e}")
            raise
    
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
            logging.debug(f"Setting saved: {key}={value}")
        except Exception as e:
            logging.error(f"Failed to save setting '{key}': {e}")
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
                        
                    # Get current style
                    current_style = settings.get('current_waveform_style', config.CURRENT_WAVEFORM_STYLE)
                    
                    # Get style configurations
                    saved_configs = settings.get('waveform_style_configs', {})
                    
                    # Start with default configurations
                    all_configs = config.WAVEFORM_STYLE_CONFIGS.copy()
                    
                    # Merge saved configurations, validating each style
                    for style_name, saved_config in saved_configs.items():
                        if style_name in all_configs and isinstance(saved_config, dict):
                            # Update default config with saved values
                            all_configs[style_name].update(saved_config)
                    
                    # Validate current style exists
                    if current_style not in all_configs:
                        logging.warning(f"Invalid current style '{current_style}', falling back to default")
                        current_style = config.CURRENT_WAVEFORM_STYLE
                    
                    return current_style, all_configs
                        
            except Exception as e:
                logging.warning(f"Failed to load waveform style settings: {e}")
            
            # Return defaults on any error
            return config.CURRENT_WAVEFORM_STYLE, config.WAVEFORM_STYLE_CONFIGS.copy()
    
    def save_waveform_style_settings(self, current_style: str, style_configs: Dict[str, Dict]) -> None:
        """Save waveform style settings to file.
        
        Args:
            current_style: Currently selected style name
            style_configs: Dictionary mapping style names to their configurations
            
        Raises:
            Exception: If saving fails or validation errors occur
        """
        with self._lock:
            # Validate current_style
            if not isinstance(current_style, str) or not current_style:
                raise ValueError("current_style must be a non-empty string")
            
            # Validate style_configs
            if not isinstance(style_configs, dict):
                raise ValueError("style_configs must be a dictionary")
            
            # Validate that current_style exists in configs
            if current_style not in style_configs:
                raise ValueError(f"current_style '{current_style}' not found in style_configs")
            
            # Validate each style config
            valid_styles = set(config.WAVEFORM_STYLE_CONFIGS.keys())
            for style_name, config_dict in style_configs.items():
                if style_name not in valid_styles:
                    raise ValueError(f"Unknown style '{style_name}'. Valid styles: {valid_styles}")
                if not isinstance(config_dict, dict):
                    raise ValueError(f"Configuration for style '{style_name}' must be a dictionary")
            
            try:
                # Load existing settings
                settings = self.load_all_settings()
                
                # Update waveform style settings
                settings['current_waveform_style'] = current_style
                settings['waveform_style_configs'] = style_configs
                
                # Save all settings
                with open(self.settings_file, 'w') as f:
                    json.dump(settings, f, indent=2)
                    
                logging.info("Waveform style settings saved successfully")
                
            except Exception as e:
                logging.error(f"Failed to save waveform style settings: {e}")
                raise
    
    def get_style_config(self, style_name: str) -> Dict[str, Any]:
        """Get configuration for a specific waveform style.
        
        Args:
            style_name: Name of the style to get configuration for
            
        Returns:
            Dictionary containing the style's configuration.
            Returns default config if style not found or error occurs.
            
        Raises:
            ValueError: If style_name is invalid
        """
        if not isinstance(style_name, str) or not style_name:
            raise ValueError("style_name must be a non-empty string")
        
        try:
            _, all_configs = self.load_waveform_style_settings()
            
            if style_name in all_configs:
                return all_configs[style_name].copy()
            else:
                # Check if it's a valid style with default config
                if style_name in config.WAVEFORM_STYLE_CONFIGS:
                    logging.info(f"Style '{style_name}' not found in saved settings, returning default")
                    return config.WAVEFORM_STYLE_CONFIGS[style_name].copy()
                else:
                    raise ValueError(f"Unknown style '{style_name}'. Valid styles: {list(config.WAVEFORM_STYLE_CONFIGS.keys())}")
                    
        except Exception as e:
            if isinstance(e, ValueError):
                raise  # Re-raise validation errors
            logging.error(f"Failed to get style config for '{style_name}': {e}")
            # Return default for the style if it exists
            if style_name in config.WAVEFORM_STYLE_CONFIGS:
                return config.WAVEFORM_STYLE_CONFIGS[style_name].copy()
            else:
                # Return particle style as ultimate fallback
                return config.WAVEFORM_STYLE_CONFIGS['particle'].copy()
    
    def save_style_config(self, style_name: str, config_dict: Dict[str, Any]) -> None:
        """Save configuration for a specific waveform style.
        
        Args:
            style_name: Name of the style to save configuration for
            config_dict: Configuration dictionary to save
            
        Raises:
            ValueError: If parameters are invalid
            Exception: If saving fails
        """
        if not isinstance(style_name, str) or not style_name:
            raise ValueError("style_name must be a non-empty string")
        
        if not isinstance(config_dict, dict):
            raise ValueError("config_dict must be a dictionary")
        
        if style_name not in config.WAVEFORM_STYLE_CONFIGS:
            valid_styles = list(config.WAVEFORM_STYLE_CONFIGS.keys())
            raise ValueError(f"Unknown style '{style_name}'. Valid styles: {valid_styles}")
        
        try:
            # Load current settings
            current_style, all_configs = self.load_waveform_style_settings()
            
            # Update the specific style configuration
            all_configs[style_name] = config_dict.copy()
            
            # Save back to file
            self.save_waveform_style_settings(current_style, all_configs)
            
            logging.info(f"Configuration saved successfully for style '{style_name}'")
            
        except Exception as e:
            if isinstance(e, ValueError):
                raise  # Re-raise validation errors
            logging.error(f"Failed to save style config for '{style_name}': {e}")
            raise
    
    def load_model_selection(self) -> str:
        """Load the saved model selection.
        
        Returns:
            The saved model selection internal value, or default if not found.
        """
        try:
            settings = self.load_all_settings()
            selected_model = settings.get('selected_model')
            
            # Validate that the model exists in the available models
            if selected_model and selected_model in config.MODEL_VALUE_MAP.values():
                return selected_model
            
        except Exception as e:
            logging.warning(f"Failed to load model selection: {e}")
        
        # Return default (first model choice mapped to internal value)
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
        
        # Validate that the model exists in the available models
        if model_value not in config.MODEL_VALUE_MAP.values():
            valid_models = list(config.MODEL_VALUE_MAP.values())
            raise ValueError(f"Invalid model '{model_value}'. Valid models: {valid_models}")
        
        try:
            # Load existing settings
            settings = self.load_all_settings()
            
            # Update model selection
            settings['selected_model'] = model_value
            
            # Save all settings
            self.save_all_settings(settings)
            
            logging.info(f"Model selection saved: {model_value}")
            
        except Exception as e:
            if isinstance(e, ValueError):
                raise  # Re-raise validation errors
            logging.error(f"Failed to save model selection: {e}")
            raise

    def load_audio_input_device(self) -> Optional[int]:
        """Load the saved audio input device ID.

        Returns:
            The saved device ID, or None to use system default.
        """
        try:
            settings = self.load_all_settings()
            device_id = settings.get('audio_input_device')
            if device_id is not None and isinstance(device_id, int):
                return device_id
        except Exception as e:
            logging.warning(f"Failed to load audio input device: {e}")
        return None

    def save_audio_input_device(self, device_id: Optional[int]) -> None:
        """Save the audio input device selection.

        Args:
            device_id: The device ID to save, or None for system default.

        Raises:
            Exception: If saving fails.
        """
        try:
            settings = self.load_all_settings()
            if device_id is None:
                settings.pop('audio_input_device', None)
            else:
                settings['audio_input_device'] = device_id
            self.save_all_settings(settings)
            logging.info(f"Audio input device saved: {device_id}")
        except Exception as e:
            logging.error(f"Failed to save audio input device: {e}")
            raise

    def load_window_geometry(self) -> Optional[Dict[str, int]]:
        """Load the saved window geometry.

        Returns:
            Dictionary with x, y, width, height keys, or None if not saved.
        """
        try:
            settings = self.load_all_settings()
            geometry = settings.get('window_geometry')
            if geometry and isinstance(geometry, dict):
                # Validate all required keys exist
                required_keys = {'x', 'y', 'width', 'height'}
                if required_keys.issubset(geometry.keys()):
                    return geometry
        except Exception as e:
            logging.warning(f"Failed to load window geometry: {e}")
        return None

    def save_window_geometry(self, x: int, y: int, width: int, height: int) -> None:
        """Save the window geometry.

        Args:
            x: Window x position.
            y: Window y position.
            width: Window width.
            height: Window height.

        Raises:
            Exception: If saving fails.
        """
        try:
            settings = self.load_all_settings()
            settings['window_geometry'] = {
                'x': x,
                'y': y,
                'width': width,
                'height': height
            }
            self.save_all_settings(settings)
            logging.debug(f"Window geometry saved: {x}, {y}, {width}x{height}")
        except Exception as e:
            logging.error(f"Failed to save window geometry: {e}")
            raise

    def load_streaming_overlay_position(self) -> Optional[Dict[str, int]]:
        """Load the saved streaming overlay position.

        Returns:
            Dictionary with x, y keys, or None if not saved.
        """
        try:
            settings = self.load_all_settings()
            position = settings.get('streaming_overlay_position')
            if position and isinstance(position, dict):
                # Validate required keys exist
                required_keys = {'x', 'y'}
                if required_keys.issubset(position.keys()):
                    return position
        except Exception as e:
            logging.warning(f"Failed to load streaming overlay position: {e}")
        return None

    def save_streaming_overlay_position(self, x: int, y: int) -> None:
        """Save the streaming overlay position.

        Args:
            x: Overlay x position.
            y: Overlay y position.

        Raises:
            Exception: If saving fails.
        """
        try:
            settings = self.load_all_settings()
            settings['streaming_overlay_position'] = {
                'x': x,
                'y': y
            }
            self.save_all_settings(settings)
            logging.debug(f"Streaming overlay position saved: {x}, {y}")
        except Exception as e:
            logging.error(f"Failed to save streaming overlay position: {e}")
            raise

    def load_streaming_settings(self) -> Dict[str, Any]:
        """Load streaming transcription settings.

        Returns:
            Dictionary containing streaming settings.
        """
        try:
            settings = self.load_all_settings()
            return {
                'streaming_enabled': settings.get('streaming_enabled', config.STREAMING_ENABLED),
                'streaming_chunk_duration': settings.get('streaming_chunk_duration', config.STREAMING_CHUNK_DURATION_SEC),
                'streaming_paste_enabled': settings.get('streaming_paste_enabled', False),
                'streaming_typing_delay': settings.get('streaming_typing_delay', 0.02)
            }
        except Exception as e:
            logging.warning(f"Failed to load streaming settings: {e}")
            return {
                'streaming_enabled': config.STREAMING_ENABLED,
                'streaming_chunk_duration': config.STREAMING_CHUNK_DURATION_SEC,
                'streaming_paste_enabled': False,
                'streaming_typing_delay': 0.02
            }

    def save_streaming_settings(
        self,
        enabled: bool,
        chunk_duration: float = None,
        paste_enabled: bool = None
    ) -> None:
        """Save streaming transcription settings.

        Args:
            enabled: Whether streaming transcription is enabled.
            chunk_duration: Duration of audio chunks in seconds (optional).
            paste_enabled: Whether to type streaming text to cursor (optional).

        Raises:
            Exception: If saving fails.
        """
        try:
            settings = self.load_all_settings()
            settings['streaming_enabled'] = enabled
            if chunk_duration is not None:
                settings['streaming_chunk_duration'] = chunk_duration
            if paste_enabled is not None:
                settings['streaming_paste_enabled'] = paste_enabled
            self.save_all_settings(settings)
            logging.info(f"Streaming settings saved: enabled={enabled}, chunk_duration={chunk_duration}, paste_enabled={paste_enabled}")
        except Exception as e:
            logging.error(f"Failed to save streaming settings: {e}")
            raise

    def load_insights_settings(self) -> Dict[str, Any]:
        """Load meeting insights settings.

        Returns:
            Dictionary containing insights settings with keys:
            - provider: LLM provider name ('openai' or 'openrouter')
            - model: Model identifier string
            - openai_key: OpenAI API key (if saved)
            - openrouter_key: OpenRouter API key (if saved)
        """
        try:
            settings = self.load_all_settings()
            return {
                'provider': settings.get('insights_provider', 'openai'),
                'model': settings.get('insights_model', 'gpt-4o'),
                'openai_key': settings.get('insights_openai_key', ''),
                'openrouter_key': settings.get('insights_openrouter_key', '')
            }
        except Exception as e:
            logging.warning(f"Failed to load insights settings: {e}")
            return {
                'provider': 'openai',
                'model': 'gpt-4o',
                'openai_key': '',
                'openrouter_key': ''
            }

    def save_insights_settings(
        self,
        provider: str = None,
        model: str = None,
        openai_key: str = None,
        openrouter_key: str = None
    ) -> None:
        """Save meeting insights settings.

        Args:
            provider: LLM provider name ('openai' or 'openrouter').
            model: Model identifier string.
            openai_key: OpenAI API key (optional).
            openrouter_key: OpenRouter API key (optional).

        Raises:
            Exception: If saving fails.
        """
        try:
            settings = self.load_all_settings()
            
            if provider is not None:
                if provider not in ('openai', 'openrouter'):
                    raise ValueError(f"Invalid provider: {provider}. Must be 'openai' or 'openrouter'")
                settings['insights_provider'] = provider
            
            if model is not None:
                settings['insights_model'] = model
            
            if openai_key is not None:
                if openai_key:
                    settings['insights_openai_key'] = openai_key
                else:
                    settings.pop('insights_openai_key', None)
            
            if openrouter_key is not None:
                if openrouter_key:
                    settings['insights_openrouter_key'] = openrouter_key
                else:
                    settings.pop('insights_openrouter_key', None)
            
            self.save_all_settings(settings)
            logging.info(f"Insights settings saved: provider={provider}, model={model}")
        except Exception as e:
            logging.error(f"Failed to save insights settings: {e}")
            raise

    def get_insights_api_key(self, provider: str = None) -> Optional[str]:
        """Get the API key for the specified or current insights provider.

        This method checks both saved settings and environment variables.

        Args:
            provider: Provider name ('openai' or 'openrouter').
                     Uses saved provider setting if None.

        Returns:
            API key string, or None if not found.
        """
        import os

        try:
            settings = self.load_all_settings()
            if provider is None:
                provider = settings.get('insights_provider', 'openai')

            # First check saved settings
            if provider == 'openai':
                key = settings.get('insights_openai_key', '')
                if key:
                    return key
                # Fall back to environment variable
                return os.getenv('OPENAI_API_KEY')
            elif provider == 'openrouter':
                key = settings.get('insights_openrouter_key', '')
                if key:
                    return key
                # Fall back to environment variable
                return os.getenv('OPENROUTER_API_KEY')
            else:
                logging.warning(f"Unknown provider: {provider}")
                return None
        except Exception as e:
            logging.error(f"Failed to get insights API key: {e}")
            return None

    def load_meeting_recording_settings(self) -> Dict[str, Any]:
        """Load meeting recording settings.

        Returns:
            Dictionary containing meeting recording settings:
            - enabled: Whether to save complete meeting recordings (default: True)
            - max_recordings: Maximum number of recordings to keep (default: 10)
        """
        try:
            settings = self.load_all_settings()
            return {
                'enabled': settings.get('meeting_recording_enabled', True),
                'max_recordings': settings.get('meeting_max_recordings', config.MAX_MEETING_RECORDINGS)
            }
        except Exception as e:
            logging.warning(f"Failed to load meeting recording settings: {e}")
            return {
                'enabled': True,
                'max_recordings': config.MAX_MEETING_RECORDINGS
            }

    def save_meeting_recording_settings(
        self,
        enabled: bool = None,
        max_recordings: int = None
    ) -> None:
        """Save meeting recording settings.

        Args:
            enabled: Whether to save complete meeting recordings.
            max_recordings: Maximum number of recordings to keep.

        Raises:
            ValueError: If max_recordings is out of valid range.
            Exception: If saving fails.
        """
        if max_recordings is not None and (max_recordings < 1 or max_recordings > 100):
            raise ValueError("max_recordings must be between 1 and 100")

        try:
            settings = self.load_all_settings()

            if enabled is not None:
                settings['meeting_recording_enabled'] = enabled

            if max_recordings is not None:
                settings['meeting_max_recordings'] = max_recordings

            self.save_all_settings(settings)
            logging.info(f"Meeting recording settings saved: enabled={enabled}, max_recordings={max_recordings}")
        except Exception as e:
            if isinstance(e, ValueError):
                raise
            logging.error(f"Failed to save meeting recording settings: {e}")
            raise

    # -------------------------------------------------------------------------
    # Insight Generation Options & Presets
    # -------------------------------------------------------------------------

    def load_insights_generation_defaults(self, insight_type: str = None) -> Dict[str, Any]:
        """Load default insight generation options.

        Args:
            insight_type: Type of insight ('summary', 'action_items', 'custom').
                         If None, returns defaults for all types.

        Returns:
            Dictionary of default generation options for the specified type,
            or all types if insight_type is None.
        """
        # Default options for each insight type
        type_defaults = {
            "summary": {
                "output_length": "standard",
                "formatting_style": "bullet_points",
                "tone": "professional",
                "focus_areas": ["decisions", "discussions"],
                "participant_filter": None,
                "topic_filter": None,
                "creativity": 0.5,
                "language": "english",
                "include_timestamps": False,
                "include_speaker_attribution": True
            },
            "action_items": {
                "output_length": "standard",
                "formatting_style": "numbered_list",
                "tone": "professional",
                "focus_areas": ["decisions"],
                "participant_filter": None,
                "topic_filter": None,
                "creativity": 0.3,
                "language": "english",
                "include_timestamps": False,
                "include_speaker_attribution": True
            },
            "custom": {
                "output_length": "standard",
                "formatting_style": "markdown",
                "tone": "professional",
                "focus_areas": [],
                "participant_filter": None,
                "topic_filter": None,
                "creativity": 0.5,
                "language": "english",
                "include_timestamps": False,
                "include_speaker_attribution": True
            }
        }

        try:
            settings = self.load_all_settings()
            saved_defaults = settings.get('insights_generation_defaults', {})

            if insight_type:
                # Return defaults for specific type
                base = type_defaults.get(insight_type, type_defaults["summary"])
                saved = saved_defaults.get(insight_type, {})
                # Merge saved over base
                return {**base, **saved}
            else:
                # Return all defaults
                result = {}
                for itype in type_defaults:
                    base = type_defaults[itype]
                    saved = saved_defaults.get(itype, {})
                    result[itype] = {**base, **saved}
                return result

        except Exception as e:
            logging.warning(f"Failed to load insights generation defaults: {e}")
            if insight_type:
                return type_defaults.get(insight_type, type_defaults["summary"])
            return type_defaults

    def save_insights_generation_defaults(
        self,
        insight_type: str,
        options: Dict[str, Any]
    ) -> None:
        """Save default insight generation options for a type.

        Args:
            insight_type: Type of insight ('summary', 'action_items', 'custom').
            options: Dictionary of generation options to save.

        Raises:
            ValueError: If insight_type is invalid.
            Exception: If saving fails.
        """
        valid_types = ['summary', 'action_items', 'custom']
        if insight_type not in valid_types:
            raise ValueError(f"Invalid insight_type: {insight_type}. Must be one of {valid_types}")

        try:
            settings = self.load_all_settings()

            if 'insights_generation_defaults' not in settings:
                settings['insights_generation_defaults'] = {}

            settings['insights_generation_defaults'][insight_type] = options
            self.save_all_settings(settings)
            logging.info(f"Insights generation defaults saved for {insight_type}")

        except Exception as e:
            if isinstance(e, ValueError):
                raise
            logging.error(f"Failed to save insights generation defaults: {e}")
            raise

    def load_insights_last_used(self, insight_type: str) -> Optional[Dict[str, Any]]:
        """Load the last used generation options for an insight type.

        Args:
            insight_type: Type of insight ('summary', 'action_items', 'custom').

        Returns:
            Dictionary of last used options, or None if not found.
        """
        try:
            settings = self.load_all_settings()
            last_used = settings.get('insights_last_used', {})
            return last_used.get(insight_type)
        except Exception as e:
            logging.warning(f"Failed to load insights last used: {e}")
            return None

    def save_insights_last_used(
        self,
        insight_type: str,
        options: Dict[str, Any]
    ) -> None:
        """Save the last used generation options for an insight type.

        Args:
            insight_type: Type of insight ('summary', 'action_items', 'custom').
            options: Dictionary of generation options.

        Raises:
            Exception: If saving fails.
        """
        try:
            settings = self.load_all_settings()

            if 'insights_last_used' not in settings:
                settings['insights_last_used'] = {}

            settings['insights_last_used'][insight_type] = options
            self.save_all_settings(settings)
            logging.debug(f"Insights last used saved for {insight_type}")

        except Exception as e:
            logging.error(f"Failed to save insights last used: {e}")
            raise

    def load_insights_presets(self) -> List[Dict[str, Any]]:
        """Load all insight presets (built-in + custom).

        Returns:
            List of preset dictionaries, with built-in presets first.
        """
        try:
            # Start with built-in presets from config
            builtin_presets = config.INSIGHT_BUILTIN_PRESETS.copy()

            # Load custom presets from settings
            settings = self.load_all_settings()
            custom_presets = settings.get('insights_custom_presets', [])

            # Combine: built-in first, then custom
            return builtin_presets + custom_presets

        except Exception as e:
            logging.warning(f"Failed to load insights presets: {e}")
            return config.INSIGHT_BUILTIN_PRESETS.copy()

    def save_insights_preset(self, preset: Dict[str, Any]) -> None:
        """Save a custom insight preset.

        Args:
            preset: Preset dictionary with id, name, insight_type, options.

        Raises:
            ValueError: If preset is missing required fields.
            Exception: If saving fails.
        """
        required_fields = ['id', 'name', 'insight_type', 'options']
        for field in required_fields:
            if field not in preset:
                raise ValueError(f"Preset missing required field: {field}")

        # Mark as not built-in
        preset['is_builtin'] = False

        try:
            settings = self.load_all_settings()

            if 'insights_custom_presets' not in settings:
                settings['insights_custom_presets'] = []

            # Check if preset with same ID exists, update it
            existing_idx = None
            for i, p in enumerate(settings['insights_custom_presets']):
                if p.get('id') == preset['id']:
                    existing_idx = i
                    break

            if existing_idx is not None:
                settings['insights_custom_presets'][existing_idx] = preset
            else:
                settings['insights_custom_presets'].append(preset)

            self.save_all_settings(settings)
            logging.info(f"Insights preset saved: {preset['name']}")

        except Exception as e:
            if isinstance(e, ValueError):
                raise
            logging.error(f"Failed to save insights preset: {e}")
            raise

    def delete_insights_preset(self, preset_id: str) -> bool:
        """Delete a custom insight preset.

        Args:
            preset_id: ID of the preset to delete.

        Returns:
            True if preset was deleted, False if not found.

        Raises:
            ValueError: If trying to delete a built-in preset.
            Exception: If saving fails.
        """
        # Check if it's a built-in preset
        for preset in config.INSIGHT_BUILTIN_PRESETS:
            if preset.get('id') == preset_id:
                raise ValueError(f"Cannot delete built-in preset: {preset_id}")

        try:
            settings = self.load_all_settings()
            custom_presets = settings.get('insights_custom_presets', [])

            # Find and remove the preset
            original_len = len(custom_presets)
            custom_presets = [p for p in custom_presets if p.get('id') != preset_id]

            if len(custom_presets) == original_len:
                return False  # Not found

            settings['insights_custom_presets'] = custom_presets
            self.save_all_settings(settings)
            logging.info(f"Insights preset deleted: {preset_id}")
            return True

        except Exception as e:
            if isinstance(e, ValueError):
                raise
            logging.error(f"Failed to delete insights preset: {e}")
            raise

    def get_insights_preset(self, preset_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific preset by ID.

        Args:
            preset_id: ID of the preset to retrieve.

        Returns:
            Preset dictionary, or None if not found.
        """
        try:
            all_presets = self.load_insights_presets()
            for preset in all_presets:
                if preset.get('id') == preset_id:
                    return preset
            return None
        except Exception as e:
            logging.error(f"Failed to get insights preset: {e}")
            return None


# Global settings manager instance
settings_manager = SettingsManager() 