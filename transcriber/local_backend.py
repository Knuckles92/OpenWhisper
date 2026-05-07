"""
Local Whisper transcription backend using faster-whisper for optimized performance.

This backend uses faster-whisper (CTranslate2) which provides:
- Up to 4x faster transcription than openai-whisper
- Lower memory usage through quantization
- Built-in VAD (Voice Activity Detection) for silence skipping
- No external FFmpeg dependency (uses PyAV)
"""
import logging
from typing import Optional, List, Tuple
from faster_whisper import WhisperModel
from .base import TranscriptionBackend
from config import config

logger = logging.getLogger(__name__)


class LocalWhisperBackend(TranscriptionBackend):
    """Local Whisper model transcription backend using faster-whisper."""

    def __init__(self, model_name: str = None, device: str = None, compute_type: str = None):
        """Initialize the local faster-whisper backend.

        Args:
            model_name: Whisper model name to use. Reads from settings if None.
                       Use "auto" to auto-select based on hardware (turbo for GPU, base for CPU).
                       Available: auto, tiny, base, small, medium, large-v2, large-v3, turbo, distil-large-v3
            device: Device to use ("cuda" or "cpu"). Overrides settings if provided.
            compute_type: Compute type to use ("float16", "float32", "int8", etc.). Overrides settings if provided.
        """
        super().__init__()
        # Read model from settings if not explicitly provided
        if model_name is None:
            from services.settings import SettingsKey, settings_manager
            settings = settings_manager.load_all_settings()
            model_name = settings.get(SettingsKey.WHISPER_MODEL, config.DEFAULT_WHISPER_MODEL)
        self.model_name = model_name  # May be "auto", resolved in _load_model
        self.model: Optional[WhisperModel] = None
        self._device: Optional[str] = None
        self._compute_type: Optional[str] = None
        self._override_device = device  # Store override values
        self._override_compute_type = compute_type
        self._load_model()

    def _get_supported_compute_types(self, device: str) -> set:
        """Get compute types supported by the current hardware.

        Args:
            device: "cpu" or "cuda"

        Returns:
            Set of supported compute type strings.
        """
        try:
            import ctranslate2
            supported = ctranslate2.get_supported_compute_types(device)
            logger.debug(f"Supported compute types for {device}: {supported}")
            return set(supported)
        except Exception as e:
            logger.warning(f"Could not query supported compute types: {e}")
            # Return safe fallback - float32 is always supported
            return {"float32"}

    def _select_best_compute_type(self, device: str, preferred: str) -> str:
        """Select the best available compute type, with fallback.

        Args:
            device: "cpu" or "cuda"
            preferred: The preferred compute type to use if supported

        Returns:
            The best available compute type string.
        """
        supported = self._get_supported_compute_types(device)

        if preferred in supported:
            return preferred

        # Preferred type not supported, try fallbacks
        if device == "cpu":
            # CPU fallback order: int8 (fastest) -> int8_float32 -> float32 (most compatible)
            fallback_order = ["int8", "int8_float32", "float32"]
        else:
            # GPU fallback order: float16 (fastest) -> int8_float16 -> float32
            fallback_order = ["float16", "int8_float16", "float32"]

        for fallback in fallback_order:
            if fallback in supported:
                logger.warning(
                    f"Compute type '{preferred}' not supported on this {device}. "
                    f"Falling back to '{fallback}'. "
                    f"(Supported types: {', '.join(sorted(supported))})"
                )
                return fallback

        # Ultimate fallback - float32 should always work
        logger.warning(f"No preferred compute types available, using float32")
        return "float32"

    def _detect_hardware(self) -> Tuple[str, str, str]:
        """Auto-detect the best device, compute type, and model for transcription.

        Returns:
            Tuple of (device, compute_type, model) where:
            - device: "cuda" for GPU or "cpu" for CPU
            - compute_type: "float16" for GPU, "int8" for CPU (if supported)
            - model: "turbo" for GPU, "base" for CPU
        """
        # Use override values if provided, otherwise check user settings
        from services.settings import SettingsKey, settings_manager
        settings = settings_manager.load_all_settings()

        if self._override_device is not None:
            device = self._override_device
        else:
            device = settings.get(SettingsKey.WHISPER_DEVICE, config.FASTER_WHISPER_DEVICE)

        if self._override_compute_type is not None:
            compute_type = self._override_compute_type
        else:
            compute_type = settings.get(SettingsKey.WHISPER_COMPUTE_TYPE, config.FASTER_WHISPER_COMPUTE_TYPE)

        # Get model from settings (no override for model, use model_name parameter instead)
        model = settings.get(SettingsKey.WHISPER_MODEL, config.DEFAULT_WHISPER_MODEL)

        # Auto-detect based on CUDA availability
        has_cuda = False
        if device == "auto" or compute_type == "auto" or model == "auto":
            try:
                import torch
                has_cuda = torch.cuda.is_available()
            except ImportError:
                has_cuda = False

            if has_cuda:
                detected_device = "cuda"
                detected_compute = "float16"
                detected_model = "turbo"
                logger.info("CUDA detected - using GPU acceleration with float16 and turbo model")
            else:
                detected_device = "cpu"
                detected_compute = "int8"
                detected_model = "base"
                logger.info("No CUDA available - using CPU with int8 quantization and base model")

            # Apply auto-detected values only where needed
            if device == "auto":
                device = detected_device
            if compute_type == "auto":
                compute_type = detected_compute
            if model == "auto":
                model = detected_model

        # Validate that the compute type is actually supported on this hardware
        # This prevents crashes on CPUs without AVX2 when int8 is selected
        compute_type = self._select_best_compute_type(device, compute_type)

        return device, compute_type, model

    def _load_model(self):
        """Load the faster-whisper model with auto hardware detection."""
        try:
            self._device, self._compute_type, detected_model = self._detect_hardware()

            # Use detected model if current model is "auto"
            if self.model_name == "auto":
                self.model_name = detected_model

            logger.info(f"Loading faster-whisper model: {self.model_name} "
                        f"(device={self._device}, compute_type={self._compute_type})")

            self.model = WhisperModel(
                self.model_name,
                device=self._device,
                compute_type=self._compute_type
            )

            logger.info("Faster-whisper model loaded successfully")

        except Exception as e:
            logger.error(f"Failed to load faster-whisper model: {e}")
            self.model = None

    def transcribe(self, audio_path: str) -> str:
        """Transcribe audio file using faster-whisper model.

        Args:
            audio_path: Path to the audio file to transcribe.

        Returns:
            Transcribed text.

        Raises:
            Exception: If transcription fails or model is not available.
        """
        if not self.is_available():
            raise Exception("Faster-whisper model is not available.")

        try:
            self.is_transcribing = True
            self.reset_cancel_flag()

            logger.info(f"Processing audio with faster-whisper (VAD={config.FASTER_WHISPER_VAD_ENABLED})...")

            # Configure VAD parameters if enabled
            vad_params = None
            if config.FASTER_WHISPER_VAD_ENABLED:
                vad_params = dict(
                    min_silence_duration_ms=config.FASTER_WHISPER_VAD_MIN_SILENCE_MS
                )

            # Transcribe - returns a generator of segments and transcription info
            segments, info = self.model.transcribe(
                audio_path,
                beam_size=config.FASTER_WHISPER_BEAM_SIZE,
                vad_filter=config.FASTER_WHISPER_VAD_ENABLED,
                vad_parameters=vad_params
            )

            logger.info(f"Detected language: {info.language} "
                        f"(probability: {info.language_probability:.2f})")

            # Iterate through segments to get transcribed text
            # Note: segments is a generator - transcription happens as we iterate
            text_parts = []
            for segment in segments:
                if self.should_cancel:
                    logger.info("Transcription canceled by user")
                    raise Exception("Transcription canceled")
                text_parts.append(segment.text)

            transcript = " ".join(text_parts).strip()

            # Clean up extra whitespace
            import re
            transcript = re.sub(r'\s+', ' ', transcript)

            logger.info(f"Transcription complete. Length: {len(transcript)} characters")

            return transcript

        except Exception as e:
            if "canceled" not in str(e).lower():
                logger.error(f"Transcription failed: {e}")
            raise
        finally:
            self.is_transcribing = False

    def transcribe_chunks(self, chunk_files: List[str]) -> str:
        """Transcribe multiple audio chunk files efficiently with faster-whisper.

        Args:
            chunk_files: List of paths to audio chunk files.

        Returns:
            Combined transcribed text from all chunks.

        Raises:
            Exception: If transcription fails or model is not available.
        """
        if not self.is_available():
            raise Exception("Faster-whisper model is not available.")

        try:
            self.is_transcribing = True
            self.reset_cancel_flag()

            transcriptions = []

            # Configure VAD parameters if enabled
            vad_params = None
            if config.FASTER_WHISPER_VAD_ENABLED:
                vad_params = dict(
                    min_silence_duration_ms=config.FASTER_WHISPER_VAD_MIN_SILENCE_MS
                )

            for i, chunk_file in enumerate(chunk_files):
                if self.should_cancel:
                    logger.info("Chunked transcription canceled by user")
                    raise Exception("Transcription canceled")

                logger.info(f"Processing chunk {i+1}/{len(chunk_files)}: {chunk_file}")

                # Transcribe individual chunk
                segments, info = self.model.transcribe(
                    chunk_file,
                    beam_size=config.FASTER_WHISPER_BEAM_SIZE,
                    vad_filter=config.FASTER_WHISPER_VAD_ENABLED,
                    vad_parameters=vad_params
                )

                # Collect text from segments
                text_parts = []
                for segment in segments:
                    if self.should_cancel:
                        logger.info("Transcription canceled during chunk processing")
                        raise Exception("Transcription canceled")
                    text_parts.append(segment.text)

                chunk_text = " ".join(text_parts).strip()
                transcriptions.append(chunk_text)

                logger.info(f"Chunk {i+1}/{len(chunk_files)} completed. "
                           f"Length: {len(chunk_text)} characters")

            # Combine transcriptions using audio_processor
            from services.audio_processor import audio_processor
            combined_text = audio_processor.combine_transcriptions(transcriptions)

            logger.info(f"Chunked transcription complete. "
                        f"Total length: {len(combined_text)} characters")

            return combined_text

        except Exception as e:
            if "canceled" not in str(e).lower():
                logger.error(f"Chunked transcription failed: {e}")
            raise
        finally:
            self.is_transcribing = False

    def is_available(self) -> bool:
        """Check if the faster-whisper model is available.

        Returns:
            True if model is loaded and available, False otherwise.
        """
        return self.model is not None

    def reload_model(self, model_name: str = None):
        """Reload the Whisper model with a different model name.

        Args:
            model_name: New model name to load. Reads from settings if None.
        """
        if model_name:
            self.model_name = model_name
        else:
            # Read from settings when no explicit model provided
            from services.settings import SettingsKey, settings_manager
            settings = settings_manager.load_all_settings()
            self.model_name = settings.get(SettingsKey.WHISPER_MODEL, config.DEFAULT_WHISPER_MODEL)
        # Clean up existing model first
        self.cleanup()
        self._load_model()

    def cleanup(self):
        """Clean up faster-whisper model and release resources.

        This unloads the model from memory (including GPU memory if applicable).
        """
        import time

        try:
            if self.model is not None:
                logger.debug("[cleanup] Starting cleanup...")

                # Cancel any ongoing transcription
                self.should_cancel = True

                # Force CUDA to finish ALL pending operations before destroying model
                # This is critical for large models like turbo
                logger.debug("[cleanup] Synchronizing CUDA...")
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.synchronize()
                        logger.debug("[cleanup] CUDA synchronized")
                except ImportError:
                    pass
                except Exception as e:
                    logger.debug(f"[cleanup] CUDA sync error: {e}")

                # Small delay after sync
                time.sleep(0.3)

                logger.debug("[cleanup] Setting model = None...")
                self.model = None
                logger.debug("[cleanup] Model set to None")

                # Give CUDA/ctranslate2 time to finish destructor work
                logger.debug("[cleanup] Sleeping 0.5s...")
                time.sleep(0.5)
                logger.debug("[cleanup] Sleep done")

                # Force garbage collection to release memory
                logger.debug("[cleanup] Calling gc.collect()...")
                import gc
                gc.collect()
                logger.debug("[cleanup] gc.collect() done")

                # Another delay before touching CUDA cache
                time.sleep(0.2)

                # Clear GPU cache
                logger.debug("[cleanup] Clearing CUDA cache...")
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                        logger.debug("[cleanup] CUDA cache cleared")
                except ImportError:
                    pass
                except Exception as e:
                    logger.debug(f"[cleanup] CUDA error: {e}")

                logger.debug("[cleanup] Cleanup complete!")
        except Exception as e:
            logger.debug(f"[cleanup] Exception: {e}")

    @property
    def name(self) -> str:
        """Get the backend name with model info."""
        device_info = f"{self._device}/{self._compute_type}" if self._device else "not loaded"
        status = "Ready" if self.is_available() else "Not Available"
        return f"FasterWhisper ({self.model_name}, {device_info}) - {status}"

    @property
    def device_info(self) -> str:
        """Get current device, compute type, and model info."""
        if self._device and self._compute_type:
            return f"{self.model_name} | {self._device} ({self._compute_type})"
        return "Not initialized"

    @property
    def requires_file_splitting(self) -> bool:
        """faster-whisper can handle files of any size without splitting.

        The library processes audio in a streaming fashion and can handle
        arbitrarily long audio files without memory issues.
        """
        return False
