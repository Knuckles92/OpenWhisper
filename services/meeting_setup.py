"""Model preparation and temporary audio checks for Meeting Mode."""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass

from services.settings import (
    HuggingFaceAccessPolicy,
    MeetingSpeakerIdBackend,
    SettingsKey,
    is_hf_hub_offline_env_set,
    resolve_meeting_asr_source,
    resolve_meeting_speaker_id_backend,
    resolve_meeting_whisper_model,
    settings_manager,
)

logger = logging.getLogger(__name__)
_audio_check_lock = threading.Lock()

# Setup checks whether the mic can hear the room. The dashboard uses a higher
# threshold to flag sustained speech-level signal during a meeting.
_SETUP_MIC_SIGNAL_RMS = 64.0  # About -54 dBFS for int16 PCM.


def setup_complete() -> bool:
    settings = settings_manager.load_all_settings()
    return settings.get(SettingsKey.MEETING_SETUP_COMPLETE) is True


@dataclass(frozen=True)
class ModelSetup:
    label: str
    speech_download: str = ""
    runtime: str = ""
    speaker_download: bool = False

    @property
    def missing(self) -> list[str]:
        return [
            name for name, needed in (
                (self.label, self.speech_download),
                ("speech runtime", self.runtime),
                ("speaker identification model", self.speaker_download),
            ) if needed
        ]


def inspect_models() -> ModelSetup:
    from services.components import is_installed, speaker_model_path
    from services.hf_access import is_model_cached
    from services.local_asr.catalog import MODELS, resolve_runtime, selected_device
    from transcriber.local_backend import LocalWhisperBackend

    settings = settings_manager.load_all_settings()
    speaker_download = (
        resolve_meeting_speaker_id_backend(settings) != MeetingSpeakerIdBackend.OFF
        and not speaker_model_path()
    )
    if resolve_meeting_asr_source(settings) == "remote":
        return ModelSetup("Remote speech engine", speaker_download=speaker_download)

    model = resolve_meeting_whisper_model(settings)
    if model == "auto":
        model = LocalWhisperBackend(model_name="auto", load=False).resolve_auto_model()
    entry = MODELS.get(model)
    runtime = ""
    if entry is not None:
        runtime, _device = resolve_runtime(entry.backend, selected_device(entry.backend, settings))
        if is_installed(runtime):
            runtime = ""
    return ModelSetup(
        label=entry.label if entry else model,
        speech_download="" if is_model_cached(model) else model,
        runtime=runtime,
        speaker_download=speaker_download,
    )


def prepare_models(cancel: threading.Event, status) -> ModelSetup:
    """Use the existing caches and download coordinators after a user click."""
    from services.components import (
        check_compatibility, component_coordinator, ensure_speaker_model,
        install_component, is_installed, speaker_model_download_blocked,
    )
    from services.hf_access import (
        describe_model_download_error, download_model_files,
        hf_access_coordinator, is_model_cached,
    )

    plan = inspect_models()
    if cancel.is_set():
        return plan
    if plan.runtime:
        entry = component_coordinator.catalog_entry(plan.runtime)
        if entry is None:
            raise RuntimeError("The selected speech runtime is unavailable on this computer.")
        reason = check_compatibility(entry)
        if reason:
            raise RuntimeError(reason)
    if plan.speech_download:
        model = plan.speech_download
        if (is_hf_hub_offline_env_set()
                or settings_manager.load_hf_access_policy() == HuggingFaceAccessPolicy.NEVER):
            raise RuntimeError("Speech model downloads are disabled in Downloads settings.")
        if not hf_access_coordinator.begin_request(model):
            raise RuntimeError("The speech model is already downloading. Try again when it finishes.")
        try:
            if not is_model_cached(model):
                status(f"Downloading {plan.label}…")
                try:
                    download_model_files(model, cancel=cancel)
                except Exception as exc:
                    raise RuntimeError(describe_model_download_error(model, exc)) from exc
        finally:
            hf_access_coordinator.end_request(model)
    if plan.runtime and not cancel.is_set():
        if component_coordinator.begin_install(plan.runtime, cancel=cancel) is None:
            raise RuntimeError("The speech runtime is already installing. Try again when it finishes.")
        try:
            if not is_installed(plan.runtime):
                status("Installing the speech runtime…")
                install_component(plan.runtime, entry, lambda *_: None, cancel)
        finally:
            component_coordinator.end_install(plan.runtime)
    if plan.speaker_download and not cancel.is_set():
        reason = speaker_model_download_blocked()
        if reason:
            raise RuntimeError(reason)
        status("Downloading the speaker identification model…")
        ensure_speaker_model()
    return inspect_models()


@dataclass(frozen=True)
class AudioCheck:
    microphone: str
    system_audio: str

    @property
    def ready(self) -> bool:
        return self.microphone == "Signal detected" and self.system_audio_verified

    @property
    def system_audio_verified(self) -> bool:
        return self.system_audio == "Signal detected"


def check_audio(
    cancel: threading.Event, *, system_audio_allowed: bool = True, seconds: float = 6.0,
) -> AudioCheck:
    """Check for real signal without saving any samples or starting a meeting."""
    from contextlib import ExitStack

    from meeting.capture.devices import find_loopback_device, find_mic_device
    from meeting.capture.health import SIGNAL_RMS_INT16, CaptureSignalMonitor
    from meeting.capture.sck_stream import ScreenCaptureKitLoopbackSource
    from meeting.capture.sd_stream import SdCaptureSource
    from meeting.capture.soundcard_stream import SoundcardLoopbackSource
    from meeting.interfaces import CHANNEL_LOOPBACK, CHANNEL_MIC
    from services.audio_devices import load_priority

    if not _audio_check_lock.acquire(blocking=False):
        raise RuntimeError("An audio check is still closing. Try again in a moment.")
    try:
        with ExitStack() as streams:
            def open_source(channel, make_source, *, signal_rms=SIGNAL_RMS_INT16):
                if cancel.is_set():
                    return None
                monitor = CaptureSignalMonitor(signal_rms=signal_rms)
                try:
                    source = make_source()
                    if source is None:
                        return None
                    streams.callback(source.stop)
                    source.start(monitor.observe)
                    if source.is_active():
                        return monitor
                except Exception:
                    logger.info("Could not open %s for the meeting audio check", channel, exc_info=True)
                return None

            def portaudio_source(channel, device):
                if device is None:
                    return None
                return SdCaptureSource(
                    channel, device["index"], device["samplerate"], device["channels"],
                )

            def mac_loopback():
                if ScreenCaptureKitLoopbackSource.available():
                    return ScreenCaptureKitLoopbackSource()
                return None

            mic = open_source(
                CHANNEL_MIC, lambda: portaudio_source(CHANNEL_MIC, find_mic_device(load_priority())),
                signal_rms=_SETUP_MIC_SIGNAL_RMS,
            )
            loopback = None
            if not cancel.is_set() and system_audio_allowed:
                if sys.platform == "darwin":
                    loopback = open_source(CHANNEL_LOOPBACK, mac_loopback)
                elif sys.platform.startswith("linux"):
                    # Its bounded start opens the validated default sink monitor.
                    loopback = open_source(CHANNEL_LOOPBACK, SoundcardLoopbackSource)
                elif sys.platform == "win32":
                    loopback = open_source(
                        CHANNEL_LOOPBACK,
                        lambda: portaudio_source(CHANNEL_LOOPBACK, find_loopback_device()),
                    )
                    if loopback is None and not cancel.is_set():
                        loopback = open_source(CHANNEL_LOOPBACK, SoundcardLoopbackSource)
            monitors = (mic, loopback)
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and any(
                monitor is not None and not monitor.snapshot()["signal_windows"]
                for monitor in monitors
            ):
                if cancel.wait(0.1):
                    break

            def result(monitor):
                if monitor is None:
                    return "Could not open"
                snapshot = monitor.snapshot()
                if snapshot["signal_windows"]:
                    return "Signal detected"
                return "No signal detected" if snapshot["receiving"] else "No audio received"

            system_result = (
                result(loopback) if system_audio_allowed else "Screen & System Audio Recording permission needed"
            )
            return AudioCheck(result(mic), system_result)
    finally:
        _audio_check_lock.release()
