"""Streaming transcription helpers for the application controller."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from config import config
try:
    from services.settings import SettingsKey, settings_manager
except ImportError:  # pragma: no cover - supports lightweight test stubs
    from services.settings import settings_manager

    class SettingsKey:
        STREAMING_ENABLED = "streaming_enabled"
        STREAMING_CHUNK_DURATION = "streaming_chunk_duration"

if TYPE_CHECKING:
    from services.recorder import AudioLevelCallback
else:
    AudioLevelCallback = Callable[[float], None]
from services.streaming_transcriber import StreamingTranscriber
from transcriber import LocalWhisperBackend

if TYPE_CHECKING:
    from services.application_controller import ApplicationController

logger = logging.getLogger(__name__)

PREVIEW_UNAVAILABLE_STATUS = (
    "Live preview needs Local Whisper, Parakeet, or Nemotron Streaming"
)


class StreamingRuntime:
    """Owns streaming transcription setup and lifecycle."""

    def __init__(self, controller: "ApplicationController"):
        self.controller = controller

    def setup_audio_level_callback(self) -> None:
        def audio_level_callback(level: float) -> None:
            levels = [level] * 20
            self.controller.ui_controller.update_audio_levels(levels)

        callback: AudioLevelCallback = audio_level_callback
        self.controller.recorder.set_audio_level_callback(callback)

    def setup_streaming(self) -> None:
        self._configure_streaming(initial_setup=True)

    def reconfigure_streaming(self) -> None:
        """Reconfigure streaming transcriber based on current settings."""
        logger.info("Reconfiguring streaming transcription...")

        if self.controller.recorder.is_recording:
            logger.warning("Cannot reconfigure streaming while recording")
            self.controller.ui_controller.set_status(
                "Stop recording before changing streaming mode"
            )
            return

        self._cleanup_streaming_resources()
        self._configure_streaming(initial_setup=False)

    def on_partial_transcription(self, text: str, is_final: bool) -> None:
        self.controller.partial_transcription.emit(text, is_final)
        if self.controller._streaming_enabled and text:
            self.controller.streaming_text_update.emit(text, is_final)

    def start_streaming_session(self) -> None:
        """Start real-time streaming transcription for an active recording."""
        if not self.controller.streaming_transcriber:
            return

        self.controller.recorder.set_streaming_callback(
            self.controller.streaming_transcriber.feed_audio
        )
        self.controller.streaming_transcriber.start_streaming(
            sample_rate=config.SAMPLE_RATE,
            callback=self.on_partial_transcription,
        )
        logger.info("Streaming transcription started")

        if self.controller._streaming_enabled:
            # Set synchronously so a queued RECORDING overlay update does not
            # flash the waveform before the streaming overlay show is delivered.
            self.controller.ui_controller.streaming_flow_active = True
            self.controller.streaming_overlay_show.emit()

    def stop_streaming_session(self) -> str:
        """Stop streaming transcription and return the accumulated text."""
        if not self.controller.streaming_transcriber:
            return ""

        streaming_text = self.controller.streaming_transcriber.stop_streaming()
        self.controller.recorder.set_streaming_callback(None)
        logger.info(
            f"Streaming transcription stopped, got {len(streaming_text)} chars"
        )
        return streaming_text

    def cancel_streaming_session(self) -> None:
        """Cancel any active streaming session."""
        if self.controller.streaming_transcriber:
            self.controller.streaming_transcriber.stop_streaming()
            self.controller.recorder.set_streaming_callback(None)
            logger.info("Streaming transcription canceled")

        if self.controller._streaming_enabled:
            self.controller.streaming_overlay_hide.emit()

    def cleanup(self) -> None:
        self._cleanup_streaming_resources()

    def _configure_streaming(self, *, initial_setup: bool) -> None:
        try:
            settings = settings_manager.load_all_settings()
            self.controller._streaming_enabled = settings.get(
                SettingsKey.STREAMING_ENABLED, config.STREAMING_ENABLED
            )
            if not self.controller._streaming_enabled:
                logger.info("Streaming transcription disabled")
                return

            backend = self.controller.current_backend
            if isinstance(backend, LocalWhisperBackend):
                streaming_backend = self._load_dedicated_preview_backend()
                if streaming_backend is None:
                    self.controller._streaming_enabled = False
                    return
            elif self._shares_preview_decoder(backend):
                if not backend.is_available():
                    # The engine is still loading or waiting on a download; the
                    # reload worker runs setup again once it has finished.
                    logger.info("Streaming preview waits for %s to load", backend.name)
                    self.controller._streaming_enabled = False
                    self.controller._pending_streaming_setup = True
                    return
                streaming_backend = backend
                logger.info("Streaming preview shares the loaded %s engine", backend.name)
            else:
                logger.info("Streaming requested but not available for this backend")
                if not initial_setup:
                    self.controller.ui_controller.set_status(PREVIEW_UNAVAILABLE_STATUS)
                self.controller._streaming_enabled = False
                return

            chunk_duration = settings.get(
                SettingsKey.STREAMING_CHUNK_DURATION, config.STREAMING_CHUNK_DURATION_SEC
            )
            self.controller.streaming_transcriber = StreamingTranscriber(
                backend=streaming_backend,
                chunk_duration_sec=chunk_duration,
                overlap_sec=config.STREAMING_OVERLAP_SEC,
            )
            logger.info(
                f"Streaming transcription enabled (chunk_duration={chunk_duration}s)"
            )
        except Exception as exc:
            logger.error(f"Failed to setup streaming: {exc}")
            self.controller._streaming_enabled = False
            if not initial_setup:
                self.controller.ui_controller.set_status("Failed to reconfigure streaming")

    @staticmethod
    def _shares_preview_decoder(backend) -> bool:
        from transcriber.optional_backend import LocalSpeechBackend

        return (
            isinstance(backend, LocalSpeechBackend)
            and backend.backend_id in config.STREAMING_PREVIEW_BACKENDS
        )

    def _load_dedicated_preview_backend(self):
        """Load tiny.en for a Whisper dictation preview, or None if it cannot run yet."""
        from services.hf_access import is_model_cached

        if not is_model_cached("tiny.en"):
            logger.info(
                "tiny.en is not in the local cache; waiting for download consent"
            )
            self.controller.request_model_download("tiny.en")
            return None

        logger.info("Creating dedicated tiny.en backend for streaming preview...")
        self.controller._streaming_backend = LocalWhisperBackend(model_name="tiny.en")
        streaming_backend = self.controller._streaming_backend
        if getattr(streaming_backend, "model", None) is None:
            logger.warning("Streaming preview inactive: tiny.en did not load")
            return None
        self._warmup_streaming_backend(streaming_backend)
        return streaming_backend

    def _warmup_streaming_backend(self, backend) -> None:
        try:
            import numpy as np

            if getattr(backend, "model", None) is None:
                logger.warning("Streaming warmup skipped: model is not loaded")
                return

            silence = np.zeros(
                max(1, config.WHISPER_TARGET_SAMPLE_RATE // 2),
                dtype=np.float32,
            )
            segments, _info = backend.model.transcribe(
                silence,
                beam_size=1,
                vad_filter=False,
            )
            # Consume the generator so CTranslate2 finishes the first pass now.
            list(segments)
            logger.info("Streaming preview model warmed up")
        except Exception as exc:
            logger.warning(f"Streaming warmup failed (non-fatal): {exc}")

    def _cleanup_streaming_resources(self) -> None:
        if self.controller.streaming_transcriber:
            try:
                self.controller.streaming_transcriber.cleanup()
                logger.info("Cleaned up existing streaming transcriber")
            except Exception as exc:
                logger.warning(f"Error cleaning up streaming transcriber: {exc}")
            self.controller.streaming_transcriber = None

        if self.controller._streaming_backend:
            try:
                logger.info("Cleaning up dedicated streaming backend...")
                self.controller._streaming_backend.cleanup()
                logger.info("Cleaned up dedicated streaming backend")
            except Exception as exc:
                logger.warning(f"Error cleaning up streaming backend: {exc}")
            self.controller._streaming_backend = None
