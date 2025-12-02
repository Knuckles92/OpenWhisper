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


class LocalWhisperBackend(TranscriptionBackend):
    """Local Whisper model transcription backend using faster-whisper."""

    def __init__(self, model_name: str = None):
        """Initialize the local faster-whisper backend.

        Args:
            model_name: Whisper model name to use. Reads from settings if None.
                       Use "auto" to auto-select based on hardware (turbo for GPU, base for CPU).
                       Available: auto, tiny, base, small, medium, large-v2, large-v3, turbo, distil-large-v3
        """
        super().__init__()
        # Read model from settings if not explicitly provided
        if model_name is None:
            from settings import settings_manager
            settings = settings_manager.load_all_settings()
            model_name = settings.get('whisper_model', config.DEFAULT_WHISPER_MODEL)
        self.model_name = model_name  # May be "auto", resolved in _load_model
        self.model: Optional[WhisperModel] = None
        self._device: Optional[str] = None
        self._compute_type: Optional[str] = None
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
            logging.debug(f"Supported compute types for {device}: {supported}")
            return set(supported)
        except Exception as e:
            logging.warning(f"Could not query supported compute types: {e}")
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
                logging.warning(
                    f"Compute type '{preferred}' not supported on this {device}. "
                    f"Falling back to '{fallback}'. "
                    f"(Supported types: {', '.join(sorted(supported))})"
                )
                return fallback

        # Ultimate fallback - float32 should always work
        logging.warning(f"No preferred compute types available, using float32")
        return "float32"

    def _detect_hardware(self) -> Tuple[str, str, str]:
        """Auto-detect the best device, compute type, and model for transcription.

        Returns:
            Tuple of (device, compute_type, model) where:
            - device: "cuda" for GPU or "cpu" for CPU
            - compute_type: "float16" for GPU, "int8" for CPU (if supported)
            - model: "turbo" for GPU, "base" for CPU
        """
        # Check user settings first, then config overrides
        from settings import settings_manager
        settings = settings_manager.load_all_settings()

        device = settings.get('whisper_device', config.FASTER_WHISPER_DEVICE)
        compute_type = settings.get('whisper_compute_type', config.FASTER_WHISPER_COMPUTE_TYPE)
        model = settings.get('whisper_model', config.DEFAULT_WHISPER_MODEL)

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
                logging.info("CUDA detected - using GPU acceleration with float16 and turbo model")
            else:
                detected_device = "cpu"
                detected_compute = "int8"
                detected_model = "base"
                logging.info("No CUDA available - using CPU with int8 quantization and base model")

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

            logging.info(f"Loading faster-whisper model: {self.model_name} "
                        f"(device={self._device}, compute_type={self._compute_type})")

            self.model = WhisperModel(
                self.model_name,
                device=self._device,
                compute_type=self._compute_type
            )

            logging.info("Faster-whisper model loaded successfully")

        except Exception as e:
            logging.error(f"Failed to load faster-whisper model: {e}")
            self.model = None

    def transcribe(self, audio_file_path: str) -> str:
        """Transcribe audio file using faster-whisper model.

        Args:
            audio_file_path: Path to the audio file to transcribe.

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

            logging.info(f"Processing audio with faster-whisper (VAD={config.FASTER_WHISPER_VAD_ENABLED})...")

            # Configure VAD parameters if enabled
            vad_params = None
            if config.FASTER_WHISPER_VAD_ENABLED:
                vad_params = dict(
                    min_silence_duration_ms=config.FASTER_WHISPER_VAD_MIN_SILENCE_MS
                )

            # Transcribe - returns a generator of segments and transcription info
            segments, info = self.model.transcribe(
                audio_file_path,
                beam_size=config.FASTER_WHISPER_BEAM_SIZE,
                vad_filter=config.FASTER_WHISPER_VAD_ENABLED,
                vad_parameters=vad_params
            )

            logging.info(f"Detected language: {info.language} "
                        f"(probability: {info.language_probability:.2f})")

            # Iterate through segments to get transcribed text
            # Note: segments is a generator - transcription happens as we iterate
            text_parts = []
            for segment in segments:
                if self.should_cancel:
                    logging.info("Transcription cancelled by user")
                    raise Exception("Transcription cancelled")
                text_parts.append(segment.text)

            transcribed_text = " ".join(text_parts).strip()

            # Clean up extra whitespace
            import re
            transcribed_text = re.sub(r'\s+', ' ', transcribed_text)

            logging.info(f"Transcription complete. Length: {len(transcribed_text)} characters")

            return transcribed_text

        except Exception as e:
            if "cancelled" not in str(e).lower():
                logging.error(f"Transcription failed: {e}")
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
                    logging.info("Chunked transcription cancelled by user")
                    raise Exception("Transcription cancelled")

                logging.info(f"Processing chunk {i+1}/{len(chunk_files)}: {chunk_file}")

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
                        logging.info("Transcription cancelled during chunk processing")
                        raise Exception("Transcription cancelled")
                    text_parts.append(segment.text)

                chunk_text = " ".join(text_parts).strip()
                transcriptions.append(chunk_text)

                logging.info(f"Chunk {i+1}/{len(chunk_files)} completed. "
                           f"Length: {len(chunk_text)} characters")

            # Combine transcriptions using audio_processor
            from audio_processor import audio_processor
            combined_text = audio_processor.combine_transcriptions(transcriptions)

            logging.info(f"Chunked transcription complete. "
                        f"Total length: {len(combined_text)} characters")

            return combined_text

        except Exception as e:
            if "cancelled" not in str(e).lower():
                logging.error(f"Chunked transcription failed: {e}")
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
            from settings import settings_manager
            settings = settings_manager.load_all_settings()
            self.model_name = settings.get('whisper_model', config.DEFAULT_WHISPER_MODEL)
        # Clean up existing model first
        self.cleanup()
        self._load_model()

    def cleanup(self):
        """Clean up faster-whisper model and release resources.

        This unloads the model from memory (including GPU memory if applicable).
        """
        try:
            if self.model is not None:
                logging.info("Cleaning up LocalWhisperBackend - unloading model...")

                # Cancel any ongoing transcription
                self.should_cancel = True

                # Delete the model to free memory
                del self.model
                self.model = None

                # Force garbage collection to release memory
                import gc
                gc.collect()

                # If using CUDA, clear GPU cache
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                        logging.info("Cleared CUDA cache")
                except ImportError:
                    pass  # torch not available, skip GPU cleanup
                except Exception as e:
                    logging.debug(f"Error clearing CUDA cache: {e}")

                logging.info("LocalWhisperBackend cleaned up successfully")
        except Exception as e:
            logging.debug(f"Error during LocalWhisperBackend cleanup: {e}")

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
