"""
Settings management for the OpenWhisper application.
"""
import json
import os
import logging
import threading
from typing import Dict, Any, Tuple, Optional
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

    def load_streaming_settings(self) -> Dict[str, Any]:
        """Load streaming transcription settings.

        Returns:
            Dictionary containing streaming settings.
        """
        try:
            settings = self.load_all_settings()
            return {
                'streaming_enabled': settings.get('streaming_enabled', config.STREAMING_ENABLED),
                'streaming_chunk_duration': settings.get('streaming_chunk_duration', config.STREAMING_CHUNK_DURATION_SEC)
            }
        except Exception as e:
            logging.warning(f"Failed to load streaming settings: {e}")
            return {
                'streaming_enabled': config.STREAMING_ENABLED,
                'streaming_chunk_duration': config.STREAMING_CHUNK_DURATION_SEC
            }

    def save_streaming_settings(self, enabled: bool, chunk_duration: float = None) -> None:
        """Save streaming transcription settings.

        Args:
            enabled: Whether streaming transcription is enabled.
            chunk_duration: Duration of audio chunks in seconds (optional).

        Raises:
            Exception: If saving fails.
        """
        try:
            settings = self.load_all_settings()
            settings['streaming_enabled'] = enabled
            if chunk_duration is not None:
                settings['streaming_chunk_duration'] = chunk_duration
            self.save_all_settings(settings)
            logging.info(f"Streaming settings saved: enabled={enabled}, chunk_duration={chunk_duration}")
        except Exception as e:
            logging.error(f"Failed to save streaming settings: {e}")
            raise


# Global settings manager instance
settings_manager = SettingsManager() 