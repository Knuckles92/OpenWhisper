"""Streaming transcription helpers for the application controller."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Callable, Optional

from config import config
from services.settings import SettingsKey, setting_value, settings_manager

if TYPE_CHECKING:
    from services.recorder import AudioLevelCallback
else:
    AudioLevelCallback = Callable[[float], None]
from services.streaming_transcriber import (
    NativeStreamingTranscriber,
    StreamingTranscriber,
)
from transcriber import LocalWhisperBackend

if TYPE_CHECKING:
    from services.application_controller import ApplicationController

logger = logging.getLogger(__name__)

PREVIEW_UNAVAILABLE_STATUS = (
    "Live preview needs Local Whisper, Parakeet, or Nemotron Streaming"
)
REMOTE_PREVIEW_UNAVAILABLE_STATUS = "Live preview needs Parakeet or Nemotron Streaming on {host}"


def preview_unavailable_reason(backend_key: str, host: str = "", host_family: Optional[str] = None) -> str:
    """Why the dictation preview can't run on an engine, or "" when it can.

    ``backend_key`` is a ``config.MODEL_VALUE_MAP`` value. The Remote engine
    previews with whatever the host runs, so ``host_family`` decides; until
    it connects that isn't known, and setup checks again once it is.
    """
    if backend_key == "remote":
        if host_family is None or host_family in config.STREAMING_PREVIEW_BACKENDS:
            return ""
        return REMOTE_PREVIEW_UNAVAILABLE_STATUS.format(host=host or "the paired computer")
    if backend_key == "local_whisper" or backend_key in config.STREAMING_PREVIEW_BACKENDS:
        return ""
    return PREVIEW_UNAVAILABLE_STATUS


class StreamingRuntime:
    """Owns streaming transcription setup and lifecycle."""

    def __init__(self, controller: "ApplicationController"):
        self.controller = controller
        self._stopping = False
        #: ``_engine_identity()`` when the current preview was set up.
        self._configured_for: Optional[tuple] = None

    def setup_audio_level_callback(self) -> None:
        def audio_level_callback(level: float) -> None:
            levels = [level] * 20
            self.controller.ui_controller.update_audio_levels(levels)

        callback: AudioLevelCallback = audio_level_callback
        self.controller.recorder.set_audio_level_callback(callback)

    def setup_streaming(self) -> None:
        if self.controller.streaming_transcriber is not None:
            # A remote engine asks again after every reload, since the host
            # may run a different engine than when this preview was built.
            # A preview that still fits, or that a recording is using, stays.
            if (self.controller.recorder.is_recording
                    or self._configured_for == self._engine_identity()):
                return
            self._cleanup_streaming_resources()
        self._configure_streaming(initial_setup=True)

    def _engine_identity(self) -> tuple:
        backend = self.controller.current_backend
        return (backend, getattr(backend, "backend_id", None), getattr(backend, "model_name", None))

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
        if self._stopping:
            return
        self.controller.partial_transcription.emit(text, is_final)
        if self.controller._streaming_enabled and text:
            self.controller.streaming_text_update.emit(text, is_final)

    def start_streaming_session(self) -> None:
        """Start real-time streaming transcription for an active recording."""
        if not self.controller.streaming_transcriber:
            return

        self._stopping = False
        self.controller.recorder.set_streaming_callback(
            self.controller.streaming_transcriber.feed_audio
        )
        self.controller.streaming_transcriber.start_streaming(
            sample_rate=config.SAMPLE_RATE,
            callback=self.on_partial_transcription,
        )
        if getattr(self.controller.streaming_transcriber, "is_streaming", True) is False:
            # The last recording's worker is still finishing a window (stop no
            # longer waits for it), so this one records without a preview;
            # showing the preview overlay would leave it empty throughout.
            self.controller.recorder.set_streaming_callback(None)
            logger.warning("Live preview skipped: the previous preview is still finishing")
            return
        logger.info("Streaming transcription started")

        if self.controller._streaming_enabled:
            # Set synchronously so a queued RECORDING overlay update does not
            # flash the waveform before the streaming overlay show is delivered.
            self.controller.ui_controller.streaming_flow_active = True
            self.controller.streaming_overlay_show.emit()

    def begin_stop_streaming_session(self) -> None:
        """Hide further preview updates while post-roll still feeds the decoder."""
        self._stopping = True

    def stop_streaming_session(self) -> str:
        """Stop streaming transcription and return the text published so far.

        The window preview returns at once and keeps its unfinished window for
        ``finalize_streaming_text``; the native stream still finishes here.
        """
        if not self.controller.streaming_transcriber:
            return ""

        self._stopping = True
        self.controller.recorder.set_streaming_callback(None)
        started = time.perf_counter()
        streaming_text = self.controller.streaming_transcriber.stop_streaming()
        logger.info(
            "Streaming transcription stopped in "
            f"{(time.perf_counter() - started) * 1000:.0f} ms, "
            f"got {len(streaming_text)} chars"
        )
        return streaming_text

    def finalize_streaming_text(self) -> Optional[str]:
        """Complete the stopped preview's text, decoding the tail it kept.

        Blocking: it can wait out a window decode still in flight and then
        decodes the few seconds of audio that were left, so only the
        transcription worker calls it, and only when the final transcript is
        empty. None when the preview
        has nothing to finish lazily (none configured, or a native stream,
        which flushed at stop).
        """
        finalize = getattr(self.controller.streaming_transcriber, "finalize_preview", None)
        if not callable(finalize):
            return None
        try:
            return finalize()
        except Exception as exc:
            logger.warning(f"Could not finish the live preview text: {exc}")
            return None

    def cancel_streaming_session(self) -> None:
        """Cancel any active streaming session."""
        self._stopping = True
        self.controller.recorder.set_streaming_callback(None)
        if self.controller.streaming_transcriber:
            cancel = getattr(self.controller.streaming_transcriber, "cancel_streaming", None)
            if callable(cancel):
                cancel()
            else:
                self.controller.streaming_transcriber.stop_streaming()
            logger.info("Streaming transcription canceled")

        if self.controller._streaming_enabled:
            self.controller.streaming_overlay_hide.emit()

    def cleanup(self) -> None:
        self._cleanup_streaming_resources()

    def _configure_streaming(self, *, initial_setup: bool) -> None:
        try:
            settings = settings_manager.load_all_settings()
            self.controller._streaming_enabled = setting_value(SettingsKey.STREAMING_ENABLED, settings)
            if not self.controller._streaming_enabled:
                logger.info("Streaming transcription disabled")
                return

            backend = self.controller.current_backend
            native = False
            if isinstance(backend, LocalWhisperBackend):
                streaming_backend = self._load_dedicated_preview_backend()
                if streaming_backend is None:
                    self.controller._streaming_enabled = False
                    return
            elif getattr(backend, "is_remote", False) and not backend.is_available():
                # Until it connects, a remote engine has no family to check:
                # the host says which engine it runs. The reload worker runs
                # setup again once the connection settles.
                logger.info("Streaming preview waits for the remote engine to connect")
                self.controller._streaming_enabled = False
                self.controller._pending_streaming_setup = True
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
                native = self._streams_natively(backend)
                logger.info("Streaming preview shares the loaded %s engine", backend.name)
            else:
                logger.info("Streaming requested but not available for this backend")
                if not initial_setup:
                    self.controller.ui_controller.set_status(self._unavailable_status(backend))
                self.controller._streaming_enabled = False
                return

            if native:
                # The engine keeps its own decoder state, so the chunk-duration
                # setting (a window size) does not apply here.
                if not getattr(streaming_backend, "is_remote", False):
                    # A remote host's first push is its own process's cost,
                    # and waiting on the network here would block the Qt thread.
                    self._warmup_native_stream(streaming_backend)
                self.controller.streaming_transcriber = NativeStreamingTranscriber(
                    backend=streaming_backend,
                    update_interval_sec=config.STREAMING_NATIVE_UPDATE_SEC,
                )
                self._configured_for = self._engine_identity()
                logger.info(
                    "Streaming preview follows %s's native stream "
                    f"(update_interval={config.STREAMING_NATIVE_UPDATE_SEC}s)",
                    streaming_backend.name,
                )
                return

            chunk_duration = setting_value(SettingsKey.STREAMING_CHUNK_DURATION, settings)
            self.controller.streaming_transcriber = StreamingTranscriber(
                backend=streaming_backend,
                chunk_duration_sec=chunk_duration,
                overlap_sec=config.STREAMING_OVERLAP_SEC,
            )
            self._configured_for = self._engine_identity()
            logger.info(
                f"Streaming transcription enabled (chunk_duration={chunk_duration}s)"
            )
        except Exception as exc:
            logger.error(f"Failed to setup streaming: {exc}")
            self.controller._streaming_enabled = False
            if not initial_setup:
                self.controller.ui_controller.set_status("Failed to reconfigure streaming")

    @staticmethod
    def _unavailable_status(backend) -> str:
        host = getattr(backend, "host_name", "") if getattr(backend, "is_remote", False) else ""
        if host:
            return REMOTE_PREVIEW_UNAVAILABLE_STATUS.format(host=host)
        return PREVIEW_UNAVAILABLE_STATUS

    @staticmethod
    def _shares_preview_decoder(backend) -> bool:
        from transcriber.optional_backend import LocalSpeechBackend

        return (
            isinstance(backend, LocalSpeechBackend)
            and backend.backend_id in config.STREAMING_PREVIEW_BACKENDS
        )

    @staticmethod
    def _streams_natively(backend) -> bool:
        """True when the engine's selected model advertises a native stream."""
        from services.local_asr.catalog import MODELS

        model = MODELS.get(getattr(backend, "model_name", None))
        return (
            backend.backend_id in config.STREAMING_NATIVE_PREVIEW_BACKENDS
            and bool(model and model.streaming)
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

    @staticmethod
    def _warmup_native_stream(backend) -> None:
        """Open and finish one throwaway stream so the first recording's push is warm.

        The worker's first stream push measured 0.28 s on an RTX 2060 against
        60 ms for every later one, and that cost is per process, not per
        session. Non-fatal: a failure here only means the first push is slow.
        """
        try:
            import numpy as np

            silence = np.zeros(config.WHISPER_TARGET_SAMPLE_RATE // 2, dtype=np.float32)
            backend.stream_audio("dictation-preview-warmup", silence, "auto", finish=True)
            logger.info("Native streaming preview warmed up")
        except Exception as exc:
            logger.warning(f"Native streaming warmup failed (non-fatal): {exc}")

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
        self._configured_for = None
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
