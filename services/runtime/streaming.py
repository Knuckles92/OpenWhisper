"""Streaming transcription helpers for the application controller."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from config import config
try:
    from services.settings import (
        SettingsKey,
        is_streaming_overlay_enabled,
        settings_manager,
    )
except ImportError:  # pragma: no cover - supports lightweight test stubs
    from services.settings import settings_manager

    def is_streaming_overlay_enabled(settings):
        if "streaming_overlay_enabled" in settings:
            return bool(settings["streaming_overlay_enabled"])
        return bool(settings.get("streaming_paste_enabled", False))

    class SettingsKey:
        STREAMING_ENABLED = "streaming_enabled"
        STREAMING_CHUNK_DURATION = "streaming_chunk_duration"
        STREAMING_OVERLAY_ENABLED = "streaming_overlay_enabled"

if TYPE_CHECKING:
    from services.recorder import AudioLevelCallback
else:
    AudioLevelCallback = Callable[[float], None]
from services.streaming_transcriber import StreamingTranscriber
from transcriber import LocalWhisperBackend

if TYPE_CHECKING:
    from services.application_controller import ApplicationController

logger = logging.getLogger(__name__)


class StreamingRuntime:
    """Owns streaming transcription setup and lifecycle."""

    def __init__(self, controller: "ApplicationController"):
        self.controller = controller

    def setup_audio_level_callback(self) -> None:
        """Setup audio level callback for waveform display."""

        def audio_level_callback(level: float) -> None:
            levels = [level] * 20
            self.controller.ui_controller.update_audio_levels(levels)

        callback: AudioLevelCallback = audio_level_callback
        self.controller.recorder.set_audio_level_callback(callback)

    def setup_streaming(self) -> None:
        """Initialize streaming transcriber if enabled."""
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
        """Handle partial transcription from the streaming worker."""
        self.controller.partial_transcription.emit(text, is_final)
        if self.controller._streaming_overlay_enabled and text:
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

        if self.controller._streaming_overlay_enabled:
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

        if self.controller._streaming_overlay_enabled:
            self.controller.streaming_overlay_hide.emit()

    def cleanup(self) -> None:
        """Release streaming resources."""
        self._cleanup_streaming_resources()

    def _configure_streaming(self, *, initial_setup: bool) -> None:
        try:
            settings = settings_manager.load_all_settings()
            self.controller._streaming_enabled = settings.get(
                SettingsKey.STREAMING_ENABLED, config.STREAMING_ENABLED
            )
            self.controller._streaming_overlay_enabled = is_streaming_overlay_enabled(
                settings
            )

            if (
                self.controller._streaming_enabled
                and isinstance(self.controller.current_backend, LocalWhisperBackend)
            ):
                chunk_duration = settings.get(
                    SettingsKey.STREAMING_CHUNK_DURATION, config.STREAMING_CHUNK_DURATION_SEC
                )

                logger.info("Creating dedicated tiny.en backend for streaming preview...")
                self.controller._streaming_backend = LocalWhisperBackend(
                    model_name="tiny.en"
                )
                streaming_backend = self.controller._streaming_backend

                self.controller.streaming_transcriber = StreamingTranscriber(
                    backend=streaming_backend,
                    chunk_duration_sec=chunk_duration,
                    overlap_sec=config.STREAMING_OVERLAP_SEC,
                )
                self._warmup_streaming_backend(streaming_backend)
                logger.info(
                    "Streaming transcription enabled "
                    f"(chunk_duration={chunk_duration}s, "
                    f"overlay={self.controller._streaming_overlay_enabled})"
                )
                if not initial_setup:
                    self.controller.ui_controller.set_status("Streaming mode enabled")
            else:
                if self.controller._streaming_enabled:
                    logger.info(
                        "Streaming requested but not available "
                        "(requires Local Whisper backend)"
                    )
                    if not initial_setup:
                        self.controller.ui_controller.set_status(
                            "Streaming requires Local Whisper backend"
                        )
                else:
                    logger.info("Streaming transcription disabled")
                    if not initial_setup:
                        self.controller.ui_controller.set_status("Streaming mode disabled")

                self.controller._streaming_enabled = False
                self.controller._streaming_overlay_enabled = False
        except Exception as exc:
            logger.error(f"Failed to setup streaming: {exc}")
            self.controller._streaming_enabled = False
            self.controller._streaming_overlay_enabled = False
            if not initial_setup:
                self.controller.ui_controller.set_status("Failed to reconfigure streaming")

    def _warmup_streaming_backend(self, backend) -> None:
        """Run a short silent inference so the first live preview is not a cold start.

        Args:
            backend: Dedicated streaming LocalWhisperBackend instance.
        """
        try:
            import numpy as np

            # 0.5s of silence at Whisper's expected sample rate
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
