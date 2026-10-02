"""Local transcription with faster-whisper."""
import logging
import threading
from typing import Optional, Tuple
from .base import TranscriptionBackend
from config import config

logger = logging.getLogger(__name__)

# faster-whisper, and ctranslate2 under it, load when a model first does
# rather than at startup, which measured 0.44 s: a session on another engine
# (a remote host, Parakeet, the API) never needs them. Tests replace this.
WhisperModel = None


def _whisper_model_class():
    global WhisperModel
    if WhisperModel is None:
        from services.isolated import IsolatedWhisperModel as model_class

        WhisperModel = model_class
    return WhisperModel

# Substrings that identify a GPU-specific load failure worth retrying on the CPU:
# a missing CUDA library ("Library cublas64_12.dll is not found or cannot be
# loaded"), a driver problem, or GPU memory exhaustion.
_GPU_ERROR_MARKERS = ("cublas", "cudnn", "cudart", "libcu", "cuda", "gpu")

# Causes that need different actions from the user. Exhausted VRAM and absent
# libraries both surface as a failed GPU load, but telling someone with a full
# 4 GB card to install packages they already have sends them after the wrong
# problem — observed on a GTX 1050 Ti, where the auto-selected turbo model at
# float32 peaked at 3957/4096 MiB before failing.
_GPU_OOM_MARKERS = ("out of memory", "outofmemory")
_GPU_LIBRARY_MARKERS = (
    "cublas", "cudnn", "cudart", "libcu", "is not found", "cannot be loaded",
)

# Windows and Linux x86_64 both offer the component; a source install can
# use the pip wheels instead.
_INSTALL_CUDA_ADVICE = (
    "Install GPU Acceleration from Downloads (a source install can use "
    "requirements-gpu.txt instead) to restore GPU acceleration."
)


class GpuFallbackCause:
    """Why a GPU load fell back to the CPU.

    Defined here rather than inferred from the note text so callers (the
    application controller) can pick cause-specific remediation — installing
    the GPU component fixes missing libraries but not exhausted VRAM.
    """

    MISSING_LIBRARIES = "missing_libraries"
    OUT_OF_MEMORY = "out_of_memory"
    UNKNOWN = "unknown"


class LocalWhisperBackend(TranscriptionBackend):
    """Local Whisper model transcription backend using faster-whisper."""

    def __init__(
        self,
        model_name: str = None,
        device: str = None,
        compute_type: str = None,
        *,
        load: bool = True,
    ):
        super().__init__()
        if model_name is None:
            from services.settings import SettingsKey, setting_value, settings_manager
            settings = settings_manager.load_all_settings()
            model_name = setting_value(SettingsKey.WHISPER_MODEL, settings)
        self.model_name = model_name
        # A faster_whisper.WhisperModel once loaded.
        self.model = None
        self._model_lock = threading.RLock()
        self._model_generation = 0
        self._device: Optional[str] = None
        self._compute_type: Optional[str] = None
        self._override_device = device
        self._override_compute_type = compute_type
        self._model_missing = False
        self._last_loaded_model: Optional[str] = None
        # True until the first ``_load_model`` attempt. Bootstrap constructs
        # the backend with ``load=False`` so the main window can appear before
        # WhisperModel is built; ``reload_model`` clears this.
        self._load_deferred = not load
        # Set when a GPU load failed and the model was loaded on the CPU instead,
        # so the UI can explain why acceleration is inactive. ``reason`` is the
        # raw error; ``note`` is the short, cause-specific status suffix;
        # ``cause`` is one of the ``GpuFallbackCause`` constants.
        self.gpu_fallback_reason: Optional[str] = None
        self.gpu_fallback_note: Optional[str] = None
        self.gpu_fallback_cause: Optional[str] = None
        if load:
            self._load_model()

    def _cuda_is_available(self) -> bool:
        """Probe with CTranslate2 because torch is not a required dependency."""
        try:
            import ctranslate2
            return ctranslate2.get_cuda_device_count() > 0
        except Exception as e:
            logger.debug(f"CUDA availability probe failed, assuming CPU: {e}")
            return False

    def _get_supported_compute_types(self, device: str) -> set:
        try:
            import ctranslate2
            supported = ctranslate2.get_supported_compute_types(device)
            logger.debug(f"Supported compute types for {device}: {supported}")
            return set(supported)
        except Exception as e:
            logger.warning(f"Could not query supported compute types: {e}")
            return {"float32"}

    def _select_best_compute_type(self, device: str, preferred: str) -> str:
        supported = self._get_supported_compute_types(device)

        if preferred in supported:
            return preferred

        if device == "cpu":
            fallback_order = ["int8", "int8_float32", "float32"]
        else:
            # GPU fallback order: float16 (fastest) -> int8_float16 ->
            # int8_float32 -> float32 (most compatible).
            #
            # int8_float32 precedes float32 because of memory, not speed. Pascal
            # and older cards support neither float16 variant, so they land on
            # float32, which roughly doubles the weights in VRAM — enough that the
            # auto-selected turbo model exhausts a 4 GB card (measured at
            # 3957/4096 MiB on a GTX 1050 Ti before failing) and drops to the CPU.
            # int8_float32 halves that at a small accuracy cost, which beats
            # losing the GPU entirely. Cards with float16 never reach this entry.
            fallback_order = ["float16", "int8_float16", "int8_float32", "float32"]

        for fallback in fallback_order:
            if fallback in supported:
                logger.warning(
                    f"Compute type '{preferred}' not supported on this {device}. "
                    f"Falling back to '{fallback}'. "
                    f"(Supported types: {', '.join(sorted(supported))})"
                )
                return fallback

        logger.warning("No preferred compute types available, using float32")
        return "float32"

    def _detect_hardware(self) -> Tuple[str, str, str]:
        from services.settings import SettingsKey, setting_value, settings_manager
        settings = settings_manager.load_all_settings()

        if self._override_device is not None:
            device = self._override_device
        else:
            device = setting_value(SettingsKey.WHISPER_DEVICE, settings)

        if self._override_compute_type is not None:
            compute_type = self._override_compute_type
        else:
            compute_type = setting_value(SettingsKey.WHISPER_COMPUTE_TYPE, settings)

        model = setting_value(SettingsKey.WHISPER_MODEL, settings)

        if device == "auto" or compute_type == "auto" or model == "auto":
            has_cuda = self._cuda_is_available()
            if device == "auto":
                device = "cuda" if has_cuda else "cpu"

            if device == "cuda":
                # Decided from what this card supports and holds, and said
                # before loading. It used to announce float16 on every card
                # and then fall back, so a Pascal card's log claimed float16
                # while it ran int8_float32.
                from services import gpu_info

                gpu = gpu_info.nvidia_gpu()
                supported = self._get_supported_compute_types("cuda")
                model, compute_type = gpu_info.plan_cuda(
                    model, compute_type, supported, gpu.total_mib if gpu else None
                )
                logger.info(gpu_info.describe_plan(model, compute_type, gpu, supported))
            else:
                if compute_type == "auto":
                    compute_type = "int8"
                if model == "auto":
                    # A CUDA device kept on the CPU (chosen, or after a GPU
                    # fallback) keeps turbo, so a later fix needs no download.
                    model = "turbo" if has_cuda else "base"
                logger.info(f"Using CPU for Local Whisper: {model} at {compute_type}")

        # Validate int8 in particular: CPUs without AVX2 may reject it.
        compute_type = self._select_best_compute_type(device, compute_type)

        return device, compute_type, model

    @property
    def load_deferred(self) -> bool:
        """True when construction skipped ``_load_model`` and nothing has tried yet."""
        return self._load_deferred

    def _load_model(self):
        """Load the faster-whisper model from the local cache only.

        Cache-first policy: cached models always load with
        ``local_files_only=True`` and never trigger Hugging Face revision or
        metadata checks. A model missing from the cache is NOT downloaded
        here — the backend stays unavailable with ``is_model_missing`` set,
        and the download must be approved through the consent flow (see
        ``download_and_load``).
        """
        self._load_deferred = False
        with self._model_lock:
            generation = self._model_generation
            self.reset_cancel_flag()
        try:
            # A reload is a fresh attempt: without this reset, a successful GPU
            # load after the user fixes the cause (e.g. installs the GPU
            # component) would keep showing the stale fallback note.
            self.gpu_fallback_reason = None
            self.gpu_fallback_note = None
            self.gpu_fallback_cause = None

            self._device, self._compute_type, detected_model = self._detect_hardware()

            self._downgrade_to_cpu_if_gpu_libraries_missing()

            if self.model_name == "auto":
                self.model_name = detected_model

            from services.hf_access import is_model_cached
            from services.whisper_sources import is_custom_model, parse_source, validate_model_folder
            if is_custom_model(self.model_name) and parse_source(self.model_name).local_path:
                self._model_missing = False
                validate_model_folder(parse_source(self.model_name).local_path)

            if not is_model_cached(self.model_name):
                logger.info(
                    f"Model '{self.model_name}' is not in the local cache; "
                    "waiting for download consent"
                )
                self._model_missing = True
                self.model = None
                return

            self._model_missing = False
            logger.info(
                f"Loading faster-whisper model from local cache: {self.model_name} "
                f"(device={self._device}, compute_type={self._compute_type})"
            )

            try:
                self._construct_model(generation)
            except Exception as gpu_error:
                if self.should_cancel:
                    raise RuntimeError("Transcription canceled") from gpu_error
                if not self._retry_on_cpu(gpu_error):
                    raise

            self._last_loaded_model = self.model_name
            logger.info("Faster-whisper model loaded successfully")

        except Exception as e:
            logger.error(f"Failed to load faster-whisper model: {e}")
            if self.model is not None and hasattr(self.model, "close"):
                self.model.close()
            self.model = None

    def _construct_model(self, generation):
        from services.whisper_sources import cached_model_path, is_custom_model
        name = cached_model_path(self.model_name) if is_custom_model(self.model_name) else self.model_name
        with self._model_lock:
            if generation != self._model_generation or self.should_cancel:
                raise RuntimeError("Transcription canceled")
            model = _whisper_model_class()(
                name, device=self._device,
                compute_type=self._compute_type, local_files_only=True,
            )
            self.model = model
        # Publish the adapter before native load begins, so Quit can kill a
        # model that hangs while loading as well as during inference.
        from services.isolated import IsolatedWhisperModel
        if isinstance(model, IsolatedWhisperModel):
            model.load()
        with self._model_lock:
            if generation != self._model_generation or self.should_cancel:
                if hasattr(model, "close"):
                    model.close()
                raise RuntimeError("Transcription canceled")

    def _downgrade_to_cpu_if_gpu_libraries_missing(self) -> None:
        """Select the CPU when a CUDA device exists but its libraries do not.

        CTranslate2 reports a device from the driver alone and resolves cuBLAS
        lazily, on the first encoder pass rather than at construction. A GPU
        model therefore loads successfully on a machine that cannot run GPU
        inference, and the failure lands on the user's first transcription — one
        exception per attempt, with no working transcript. That is the state of
        any machine with a driver but no GPU component (Windows) or no
        ``requirements-gpu.txt`` (Linux).

        Probing the libraries directly costs about 50 ms once and nothing
        afterwards. Forcing the load with a warm-up encode instead was measured
        at 3.4 s on every model load and did not speed up the first
        transcription, so it is not worth charging every healthy GPU user for.
        This reuses the probe behind the Components UI, so the two can never
        disagree about whether this machine is GPU-capable.
        """
        if self._device != "cuda":
            return

        try:
            from services.components import gpu_runtime_available
        except Exception as exc:  # pragma: no cover - defensive import guard
            logger.debug(f"Could not probe CUDA libraries: {exc}")
            return

        if gpu_runtime_available():
            return

        logger.warning(
            "A CUDA device is present but its libraries could not be loaded; "
            f"using CPU. {_INSTALL_CUDA_ADVICE}"
        )
        self.gpu_fallback_reason = "CUDA libraries (cuBLAS) could not be loaded"
        self.gpu_fallback_note = "GPU unavailable, using CPU"
        self.gpu_fallback_cause = GpuFallbackCause.MISSING_LIBRARIES
        self._device = "cpu"
        self._compute_type = self._select_best_compute_type("cpu", "int8")

    def _describe_gpu_failure(self, error: Exception) -> Tuple[str, str, str]:
        """Return cause-specific advice, status note, and fallback cause."""
        text = str(error).lower()

        if any(marker in text for marker in _GPU_OOM_MARKERS):
            return (
                "The GPU ran out of memory. Choose a smaller model, or set the "
                "compute type to int8 in Settings, to keep using the GPU.",
                "GPU out of memory, using CPU",
                GpuFallbackCause.OUT_OF_MEMORY,
            )

        if any(marker in text for marker in _GPU_LIBRARY_MARKERS):
            return (
                _INSTALL_CUDA_ADVICE,
                "GPU unavailable, using CPU",
                GpuFallbackCause.MISSING_LIBRARIES,
            )

        return ("", "GPU load failed, using CPU", GpuFallbackCause.UNKNOWN)

    def _retry_on_cpu(self, error: Exception) -> bool:
        """Reload the model on the CPU after a GPU load failure.

        A CUDA device can be present while the libraries CTranslate2 needs are
        not — no GPU component installed on Windows, no ``requirements-gpu.txt``
        on Linux, or a component removed while the app is running. Failing hard
        there leaves the backend permanently unavailable even though CPU
        transcription would work, so retry once on the CPU.

        The model is deliberately unchanged. Switching turbo to base would
        silently alter transcription quality and could require a download the
        user has not consented to.

        Args:
            error: The exception raised by the GPU load attempt.

        Returns:
            True if the model is now loaded on the CPU, False if the caller
            should propagate ``error``.
        """
        if self._device == "cpu":
            return False

        # Only GPU-shaped failures justify a device switch. A corrupt model or a
        # bad compute type would fail identically on the CPU, and reporting it as
        # "GPU unavailable" would send the user chasing the wrong problem.
        if not any(marker in str(error).lower() for marker in _GPU_ERROR_MARKERS):
            return False

        advice, note, cause = self._describe_gpu_failure(error)
        logger.warning(
            f"GPU model load failed ({error}); falling back to CPU."
            + (f" {advice}" if advice else "")
        )
        self._device = "cpu"
        self._compute_type = self._select_best_compute_type("cpu", "int8")
        self.gpu_fallback_reason = str(error)
        self.gpu_fallback_note = note
        self.gpu_fallback_cause = cause

        previous = self.model
        if previous is not None and hasattr(previous, "close"):
            previous.close()
        self._construct_model(self._model_generation)
        logger.info(
            f"Loaded '{self.model_name}' on CPU "
            f"(compute_type={self._compute_type}) after GPU failure"
        )
        return True

    def download_and_load(self, progress_callback=None) -> None:
        """Download the model from Hugging Face, then load it from the cache.

        Only call this after the access policy or an explicit user consent
        permitted the download. Runs synchronously — keep it off the Qt
        thread. A failed download leaves the backend unavailable; it is never
        treated as cached and never falls back to another model.

        """
        from services.hf_access import download_model_files

        download_model_files(self.model_name, progress_callback=progress_callback)
        self._load_model()
        if not self.is_available():
            raise Exception(
                f"Model '{self.model_name}' failed to load after download"
            )

    def transcribe(self, audio_path: str) -> str:
        """Transcribe an audio file with the loaded faster-whisper model."""
        if not self.is_available():
            raise Exception("Faster-whisper model is not available.")

        try:
            self.is_transcribing = True
            self.reset_cancel_flag()

            logger.info(f"Processing audio with faster-whisper (VAD={config.FASTER_WHISPER_VAD_ENABLED})...")

            vad_params = None
            if config.FASTER_WHISPER_VAD_ENABLED:
                vad_params = dict(
                    min_silence_duration_ms=config.FASTER_WHISPER_VAD_MIN_SILENCE_MS
                )

            from services.isolated import IsolatedWhisperModel
            model = self.model
            if isinstance(model, IsolatedWhisperModel):
                transcribe = model.transcribe_cancelable
                cancel_options = {"should_cancel": lambda: self.should_cancel}
            else:
                transcribe = model.transcribe
                cancel_options = {}
            segments, info = transcribe(
                audio_path,
                beam_size=config.FASTER_WHISPER_BEAM_SIZE,
                vad_filter=config.FASTER_WHISPER_VAD_ENABLED,
                vad_parameters=vad_params,
                **cancel_options,
            )

            logger.info(f"Detected language: {info.language} "
                        f"(probability: {info.language_probability:.2f})")

            # faster-whisper performs transcription while this generator is consumed.
            text_parts = []
            for segment in segments:
                if self.should_cancel:
                    logger.info("Transcription canceled by user")
                    raise Exception("Transcription canceled")
                text_parts.append(segment.text)

            transcript = " ".join(text_parts).strip()

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

    def is_available(self) -> bool:
        """Return whether a model is loaded."""
        return self.model is not None

    def reload_model(self, model_name: str = None):
        """Reload an explicit model or the model currently stored in settings."""
        if model_name:
            self.model_name = model_name
        else:
            from services.settings import SettingsKey, setting_value, settings_manager
            settings = settings_manager.load_all_settings()
            self.model_name = setting_value(SettingsKey.WHISPER_MODEL, settings)
        self.cleanup()
        self._load_model()

    def cleanup(self):
        """Terminate the owning process; no CUDA synchronization in the app."""
        with self._model_lock:
            self._model_generation += 1
            self.should_cancel = True
            model, self.model = self.model, None
        if model is not None and hasattr(model, "close"):
            model.close()

    def cancel_transcription(self):
        super().cancel_transcription()
        with self._model_lock:
            model = self.model
        # Keep the adapter ready for the next recording. It reloads in a new
        # process on demand, after the canceled job has unwound.
        if model is not None and hasattr(model, "cancel"):
            model.cancel()

    @property
    def device(self) -> Optional[str]:
        """Device the engine actually loaded on ("cuda"/"cpu"), or None.

        This is the resolved device, which can differ from the settings value:
        an "auto" or "cuda" selection lands here as "cpu" after a GPU fallback.
        """
        return self._device

    @property
    def compute_type(self) -> Optional[str]:
        """Compute type the engine actually loaded with, e.g. "int8_float32"."""
        return self._compute_type

    @property
    def last_loaded_model(self) -> Optional[str]:
        """Resolved name of the most recently loaded model, or None.

        Survives a failed reload to a missing model, so callers can revert the
        selection to a model that is actually present in the cache (e.g. after
        the user cancels a download).
        """
        return self._last_loaded_model

    @property
    def is_model_missing(self) -> bool:
        """True when the requested model is absent from the local cache.

        Distinguishes "needs a consented download" from other load failures
        (e.g. unsupported hardware), so callers know whether the consent flow
        can make the backend available.
        """
        return self._model_missing

    @property
    def name(self) -> str:
        device_info = f"{self._device}/{self._compute_type}" if self._device else "not loaded"
        status = "Ready" if self.is_available() else "Not Available"
        return f"FasterWhisper ({self.model_name}, {device_info}) - {status}"

    @property
    def device_info(self) -> str:
        if self._model_missing:
            return f"{self.model_name} | not downloaded"
        if self._device and self._compute_type:
            info = f"{self.model_name} | {self._device} ({self._compute_type})"
            # Without this the display is indistinguishable from a machine that
            # simply has no GPU, which hides a fixable problem. The note is
            # cause-specific: "GPU unavailable" would overstate an out-of-memory
            # failure, where the GPU was found and working.
            if self.gpu_fallback_note:
                info += f" — {self.gpu_fallback_note}"
            return info
        return "Not initialized"
