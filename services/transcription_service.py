"""
Transcription controller for managing transcription backends and model selection.
"""

import logging
from typing import Dict, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from config import config
from services.settings_service import settings_manager
from transcriber import TranscriptionBackend, LocalWhisperBackend, OpenAIBackend


class TranscriptionService(QObject):
    """Manages transcription backends and model selection."""

    # Signals
    device_info_changed = pyqtSignal(str)
    model_changed = pyqtSignal(str)  # Emits model_value when model changes

    def __init__(self):
        super().__init__()
        self.backends: Dict[str, TranscriptionBackend] = {}
        self.current_backend: Optional[TranscriptionBackend] = None
        self._current_model_name: str = "local_whisper"

        self._setup_backends()

    def _setup_backends(self):
        """Initialize transcription backends."""
        logging.info("Setting up transcription backends...")

        self.backends['local_whisper'] = LocalWhisperBackend()
        self.backends['api_whisper'] = OpenAIBackend('api_whisper')
        self.backends['api_gpt4o'] = OpenAIBackend('api_gpt4o')
        self.backends['api_gpt4o_mini'] = OpenAIBackend('api_gpt4o_mini')

        saved_model = settings_manager.load_model_selection()
        self.current_backend = self.backends.get(
            saved_model, self.backends['local_whisper']
        )
        self._current_model_name = saved_model or 'local_whisper'
        logging.info(f"Using transcription backend: {saved_model}")

    def on_model_changed(self, model_name: str):
        """Handle model selection change.

        Args:
            model_name: Display name of the model from UI dropdown.
        """
        model_value = config.MODEL_VALUE_MAP.get(model_name)
        if model_value and model_value in self.backends:
            self.current_backend = self.backends[model_value]
            self._current_model_name = model_value
            settings_manager.save_model_selection(model_value)
            logging.info(f"Switched to model: {model_value}")

            if model_value == 'local_whisper':
                local_backend = self.backends.get('local_whisper')
                if local_backend and hasattr(local_backend, 'device_info'):
                    self.device_info_changed.emit(local_backend.device_info)
            else:
                self.device_info_changed.emit("")

            self.model_changed.emit(model_value)

    def reload_whisper_model(self) -> Optional[str]:
        """Reload the local whisper model with current settings.

        Returns:
            Device info string if successful, None otherwise.
        """
        logging.info("Reloading whisper model...")

        local_backend = self.backends.get('local_whisper')
        if local_backend:
            local_backend.reload_model()

            if hasattr(local_backend, 'device_info'):
                device_info = local_backend.device_info
                self.device_info_changed.emit(device_info)
                logging.info(f"Whisper reloaded: {device_info}")
                return device_info
        else:
            logging.warning("Local whisper backend not found")

        return None

    def get_current_backend(self) -> Optional[TranscriptionBackend]:
        """Get the currently selected transcription backend."""
        return self.current_backend

    def get_current_model_name(self) -> str:
        """Get the current model name."""
        return self._current_model_name

    def get_model_info(self) -> str:
        """Get detailed model info string for history/logging.

        Returns:
            Model info string, including device info for local whisper.
        """
        if self._current_model_name == 'local_whisper':
            local_backend = self.backends.get('local_whisper')
            if local_backend and hasattr(local_backend, 'device_info'):
                return f"local_whisper ({local_backend.device_info})"
        return self._current_model_name

    def get_local_device_info(self) -> Optional[str]:
        """Get device info from local whisper backend.

        Returns:
            Device info string or None if not available.
        """
        local_backend = self.backends.get('local_whisper')
        if local_backend and hasattr(local_backend, 'device_info'):
            return local_backend.device_info
        return None

    def cleanup(self):
        """Cleanup all transcription backends."""
        for backend_name, backend in self.backends.items():
            try:
                logging.info(f"Cleaning up transcription backend: {backend_name}")
                backend.cleanup()
            except Exception as e:
                logging.debug(f"Error cleaning up {backend_name} backend: {e}")
        self.backends.clear()
        self.current_backend = None


__all__ = ["TranscriptionService"]
