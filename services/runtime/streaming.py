"""Streaming transcription helpers for the application controller."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from config import config
from services.settings import settings_manager
from services.streaming_transcriber import StreamingTranscriber
from transcriber import LocalWhisperBackend

if TYPE_CHECKING:
    from services.application_controller import ApplicationController


class StreamingRuntime:
    """Owns streaming transcription setup and lifecycle."""

    def __init__(self, controller: "ApplicationController"):
        self.controller = controller

    def setup_audio_level_callback(self) -> None:
        """Setup audio level callback for waveform display."""

        def audio_level_callback(level: float) -> None:
            levels = [level] * 20
            self.controller.ui_controller.update_audio_levels(levels)

        self.controller.recorder.set_audio_level_callback(audio_level_callback)

    def setup_streaming(self) -> None:
        """Initialize streaming transcriber if enabled."""
        self._configure_streaming(initial_setup=True)

    def reconfigure_streaming(self) -> None:
        """Reconfigure streaming transcriber based on current settings."""
        logging.info("Reconfiguring streaming transcription...")

        if self.controller.recorder.is_recording:
            logging.warning("Cannot reconfigure streaming while recording")
            self.controller.ui_controller.set_status(
                "Stop recording before changing streaming mode"
            )
            return

        self._cleanup_streaming_resources()
        self._configure_streaming(initial_setup=False)

    def on_partial_transcription(self, text: str, is_final: bool) -> None:
        """Handle partial transcription from the streaming worker."""
        self.controller.partial_transcription.emit(text, is_final)
        if self.controller._streaming_paste_enabled and text:
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
        logging.info("Streaming transcription started")

        if self.controller._streaming_paste_enabled:
            self.controller.streaming_overlay_show.emit()

    def stop_streaming_session(self) -> str:
        """Stop streaming transcription and return the accumulated text."""
        if not self.controller.streaming_transcriber:
            return ""

        streaming_text = self.controller.streaming_transcriber.stop_streaming()
        self.controller.recorder.set_streaming_callback(None)
        logging.info(
            f"Streaming transcription stopped, got {len(streaming_text)} chars"
        )
        return streaming_text

    def cancel_streaming_session(self) -> None:
        """Cancel any active streaming session."""
        if self.controller.streaming_transcriber:
            self.controller.streaming_transcriber.stop_streaming()
            self.controller.recorder.set_streaming_callback(None)
            logging.info("Streaming transcription cancelled")

        if self.controller._streaming_paste_enabled:
            self.controller.streaming_overlay_hide.emit()
            self.controller.caret_indicator_hide.emit()

    def cleanup(self) -> None:
        """Release streaming resources."""
        self._cleanup_streaming_resources()

    def _configure_streaming(self, *, initial_setup: bool) -> None:
        try:
            settings = settings_manager.load_all_settings()
            self.controller._streaming_enabled = settings.get(
                "streaming_enabled", config.STREAMING_ENABLED
            )
            self.controller._streaming_paste_enabled = settings.get(
                "streaming_paste_enabled", False
            )
            streaming_tiny_enabled = settings.get("streaming_tiny_model_enabled", False)

            if (
                self.controller._streaming_enabled
                and isinstance(self.controller.current_backend, LocalWhisperBackend)
            ):
                chunk_duration = settings.get(
                    "streaming_chunk_duration", config.STREAMING_CHUNK_DURATION_SEC
                )

                if streaming_tiny_enabled:
                    logging.info("Creating dedicated tiny.en backend for streaming...")
                    self.controller._streaming_backend = LocalWhisperBackend(
                        model_name="tiny.en"
                    )
                    streaming_backend = self.controller._streaming_backend
                    logging.info(
                        "Streaming %s dedicated tiny.en model",
                        "will use" if initial_setup else "reconfigured with",
                    )
                else:
                    streaming_backend = self.controller.current_backend
                    logging.info(
                        "Streaming %s main transcription model",
                        "will share" if initial_setup else "reconfigured to share",
                    )

                self.controller.streaming_transcriber = StreamingTranscriber(
                    backend=streaming_backend,
                    chunk_duration_sec=chunk_duration,
                )
                logging.info(
                    "Streaming transcription enabled "
                    f"(chunk_duration={chunk_duration}s, "
                    f"paste_overlay={self.controller._streaming_paste_enabled})"
                )
                if not initial_setup:
                    self.controller.ui_controller.set_status("Streaming mode enabled")
            else:
                if self.controller._streaming_enabled:
                    logging.info(
                        "Streaming requested but not available "
                        "(requires Local Whisper backend)"
                    )
                    if not initial_setup:
                        self.controller.ui_controller.set_status(
                            "Streaming requires Local Whisper backend"
                        )
                else:
                    logging.info("Streaming transcription disabled")
                    if not initial_setup:
                        self.controller.ui_controller.set_status("Streaming mode disabled")

                self.controller._streaming_enabled = False
                self.controller._streaming_paste_enabled = False
        except Exception as exc:
            logging.error(f"Failed to setup streaming: {exc}")
            self.controller._streaming_enabled = False
            self.controller._streaming_paste_enabled = False
            if not initial_setup:
                self.controller.ui_controller.set_status("Failed to reconfigure streaming")

    def _cleanup_streaming_resources(self) -> None:
        if self.controller.streaming_transcriber:
            try:
                self.controller.streaming_transcriber.cleanup()
                logging.info("Cleaned up existing streaming transcriber")
            except Exception as exc:
                logging.warning(f"Error cleaning up streaming transcriber: {exc}")
            self.controller.streaming_transcriber = None

        if self.controller._streaming_backend:
            try:
                logging.info("Cleaning up dedicated streaming backend...")
                self.controller._streaming_backend.cleanup()
                logging.info("Cleaned up dedicated streaming backend")
            except Exception as exc:
                logging.warning(f"Error cleaning up streaming backend: {exc}")
            self.controller._streaming_backend = None
