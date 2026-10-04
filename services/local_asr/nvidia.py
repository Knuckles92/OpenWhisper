"""Bindings to the pinned NeMo-Speech.cpp v1 C ABI, used only in a worker."""
from __future__ import annotations

import array
import ctypes as c
import os
import sys
from pathlib import Path


class BackendConfig(c.Structure):
    _fields_ = [("size", c.c_size_t), ("gpu", c.c_int32)]


class ModelConfig(c.Structure):
    _fields_ = [("size", c.c_size_t), ("path", c.c_char_p), ("name", c.c_char_p)]


class RecognizerConfig(c.Structure):
    _fields_ = [("size", c.c_size_t)] + [(name, c.c_void_p) for name in
        ("backend", "model", "streaming", "decoder", "vad", "endpointing", "postproc", "diar", "batching")]


class Options(c.Structure):
    _fields_ = [
        ("size", c.c_size_t), ("request_id", c.c_char_p), ("language_code", c.c_char_p),
        ("interim_results", c.c_bool), ("enable_word_time_offsets", c.c_bool),
        ("enable_automatic_punctuation", c.c_bool), ("verbatim_transcripts", c.c_bool),
        ("profanity_filter", c.c_bool), ("stop_history_eou_ms", c.c_int32),
        ("speech_contexts", c.c_void_p), ("speech_context_count", c.c_size_t),
        ("max_alternatives", c.c_int32), ("enable_speaker_diarization", c.c_bool),
        ("max_speaker_count", c.c_int32),
    ]


def _load_linux(runtime: str, device: str):
    # RUNPATH=$ORIGIN finds the rest of the release (ggml, and for CUDA its
    # own cudart and cuBLAS) beside this library. The release also bundles
    # an older libstdc++; loading the system's first keeps one copy per
    # process, the newer, which the release's libraries accept.
    try:
        c.CDLL("libstdc++.so.6", mode=c.RTLD_GLOBAL)
    except OSError:
        pass
    library = Path(runtime) / "nemo-speech" / "lib" / "libnemo_speech_asr_c.so"
    try:
        return c.CDLL(str(library))
    except OSError as exc:
        if device == "cuda" and "libcuda.so" in str(exc):
            raise RuntimeError(
                "The GPU speech runtime needs the NVIDIA driver (libcuda.so.1 was "
                "not found). Install the NVIDIA driver, or choose CPU."
            ) from exc
        if device == "cuda" and "libvulkan.so" in str(exc):
            raise RuntimeError(
                "The Vulkan speech runtime needs the Vulkan loader (libvulkan.so.1 "
                "was not found). Install it and the NVIDIA driver, or choose CPU."
            ) from exc
        raise


# ggml_backend_dev_type values in the ggml NeMo-Speech.cpp 0.1.0 pins.
_GGML_DEVICE_GPU = 1
_GGML_DEVICE_IGPU = 2
# Vulkan device names; drivers before about 2021 left out the "NVIDIA" prefix.
_NVIDIA_NAMES = ("nvidia", "geforce", "quadro", "tesla", "titan")


def nvidia_device_index(descriptions) -> int:
    """NeMo's gpu index of the first NVIDIA device among its GPU descriptions."""
    for index, description in enumerate(descriptions):
        if any(name in description.lower() for name in _NVIDIA_NAMES):
            return index
    raise RuntimeError(
        "The Vulkan speech runtime found no NVIDIA GPU. Check the NVIDIA driver, "
        "or choose CPU."
    )


def _vulkan_gpu_index(lib_dir: Path) -> tuple[int, str]:
    """The NVIDIA card's index among the Vulkan runtime's GPUs, and its name.

    NeMo numbers dedicated and integrated GPUs together, in ggml's order, and
    a laptop's integrated GPU often comes first, so index 0 would run the
    model there. Listing the devices doesn't initialize them, so NeMo's own
    Vulkan settings still apply when it does.
    """
    ggml = c.CDLL(str(lib_dir / "libggml.so"))
    ggml.ggml_backend_dev_count.argtypes, ggml.ggml_backend_dev_count.restype = [], c.c_size_t
    ggml.ggml_backend_dev_get.argtypes, ggml.ggml_backend_dev_get.restype = [c.c_size_t], c.c_void_p
    ggml.ggml_backend_dev_type.argtypes, ggml.ggml_backend_dev_type.restype = [c.c_void_p], c.c_int
    ggml.ggml_backend_dev_description.argtypes, ggml.ggml_backend_dev_description.restype = [c.c_void_p], c.c_char_p
    descriptions = []
    for i in range(ggml.ggml_backend_dev_count()):
        device = ggml.ggml_backend_dev_get(i)
        if ggml.ggml_backend_dev_type(device) in (_GGML_DEVICE_GPU, _GGML_DEVICE_IGPU):
            descriptions.append((ggml.ggml_backend_dev_description(device) or b"").decode("utf-8", "replace"))
    index = nvidia_device_index(descriptions)
    return index, descriptions[index]


class NvidiaRecognizer:
    def __init__(self, runtime: str, model_path: str, device: str):
        self._dll_dir = None
        #: The GPU the Vulkan runtime chose, for the log; "" otherwise.
        self.gpu_name = ""
        gpu = 0
        if sys.platform == "darwin":
            if device not in ("cpu", "metal"):
                raise RuntimeError("The Mac speech runtime supports the Apple GPU or CPU.")
            library = Path(runtime) / "nemo-speech" / "lib" / "libnemo_speech_asr_c.dylib"
            if device == "metal" and not (library.parent / "libggml-metal.dylib").exists():
                raise RuntimeError("Install NVIDIA Speech GPU (Metal) in Downloads, or choose CPU.")
            self.lib = c.CDLL(str(library))
        elif sys.platform.startswith("linux"):
            self.lib = _load_linux(runtime, device)
            lib_dir = Path(runtime) / "nemo-speech" / "lib"
            if device == "cuda" and (lib_dir / "libggml-vulkan.so").exists():
                gpu, self.gpu_name = _vulkan_gpu_index(lib_dir)
        else:
            bin_dir = Path(runtime) / "bin"
            self._dll_dir = os.add_dll_directory(str(bin_dir))
            library = bin_dir / "nemo_speech_asr_c.dll"
            self.lib = c.CDLL(str(library))
        self._bind()
        # Metal is the only GPU the Mac release can see.
        backend = BackendConfig(c.sizeof(BackendConfig), gpu if device in ("cuda", "metal") else -1)
        model = ModelConfig(c.sizeof(ModelConfig), model_path.encode("utf-8"), None)
        config = RecognizerConfig()
        config.size = c.sizeof(config)
        config.backend = c.addressof(backend)
        config.model = c.addressof(model)
        self.handle = c.c_void_p()
        self._check(self.lib.nemo_speech_asr_create(c.byref(config), c.byref(self.handle)))
        self.streams = {}

    def _bind(self):
        specs = {
            "create": ([c.POINTER(RecognizerConfig), c.POINTER(c.c_void_p)], c.c_int),
            "destroy": ([c.c_void_p], None),
            "recognition_options_default": ([], Options),
            "recognize_f32": ([c.c_void_p, c.POINTER(Options), c.POINTER(c.c_float), c.c_size_t, c.c_int32, c.POINTER(c.c_void_p)], c.c_int),
            "streaming_recognize": ([c.c_void_p, c.POINTER(Options), c.POINTER(c.c_void_p)], c.c_int),
            "stream_push_f32": ([c.c_void_p, c.POINTER(c.c_float), c.c_size_t, c.c_int32], c.c_int),
            "stream_finish": ([c.c_void_p], c.c_int),
            "stream_next": ([c.c_void_p, c.POINTER(c.c_void_p)], c.c_int),
            "stream_close": ([c.c_void_p], None),
            "result_is_final": ([c.c_void_p], c.c_bool),
            "result_audio_processed": ([c.c_void_p], c.c_float),
            "result_transcript": ([c.c_void_p, c.c_size_t], c.c_char_p),
            "result_word_count": ([c.c_void_p, c.c_size_t], c.c_size_t),
            "result_word_text": ([c.c_void_p, c.c_size_t, c.c_size_t], c.c_char_p),
            "result_word_start_time": ([c.c_void_p, c.c_size_t, c.c_size_t], c.c_int32),
            "result_word_end_time": ([c.c_void_p, c.c_size_t, c.c_size_t], c.c_int32),
            "result_destroy": ([c.c_void_p], None),
            "last_error": ([], c.c_char_p),
        }
        for name, (args, result) in specs.items():
            fn = getattr(self.lib, "nemo_speech_asr_" + name)
            fn.argtypes, fn.restype = args, result

    def _check(self, status):
        if status:
            message = self.lib.nemo_speech_asr_last_error()
            raise RuntimeError(message.decode("utf-8", "replace") if message else f"Speech runtime error {status}")

    def _options(self, language):
        options = self.lib.nemo_speech_asr_recognition_options_default()
        options.language_code = language.encode() if language and language != "auto" else None
        options.enable_word_time_offsets = True
        options.enable_automatic_punctuation = True
        options.interim_results = True
        return options

    def _result(self, result):
        try:
            text = (self.lib.nemo_speech_asr_result_transcript(result, 0) or b"").decode("utf-8")
            words = []
            for i in range(self.lib.nemo_speech_asr_result_word_count(result, 0)):
                words.append(dict(
                    text=(self.lib.nemo_speech_asr_result_word_text(result, 0, i) or b"").decode("utf-8"),
                    start=self.lib.nemo_speech_asr_result_word_start_time(result, 0, i)/1000,
                    end=self.lib.nemo_speech_asr_result_word_end_time(result, 0, i)/1000,
                ))
            return dict(text=text, words=words, final=bool(self.lib.nemo_speech_asr_result_is_final(result)),
                        end=float(self.lib.nemo_speech_asr_result_audio_processed(result)))
        finally:
            self.lib.nemo_speech_asr_result_destroy(result)

    def transcribe(self, samples: array.array, language=None):
        options = self._options(language)
        result = c.c_void_p()
        buf = (c.c_float * len(samples)).from_buffer(samples)
        self._check(self.lib.nemo_speech_asr_recognize_f32(self.handle, c.byref(options), buf, len(samples), 16000, c.byref(result)))
        data = self._result(result)
        return dict(text=data["text"], segments=segments_from_words(data, len(samples)/16000))

    def stream(self, key, samples, language=None, finish=False):
        if key not in self.streams:
            options = self._options(language)
            handle = c.c_void_p()
            self._check(self.lib.nemo_speech_asr_streaming_recognize(self.handle, c.byref(options), c.byref(handle)))
            self.streams[key] = handle
        handle = self.streams[key]
        if samples:
            buf = (c.c_float * len(samples)).from_buffer(samples)
            self._check(self.lib.nemo_speech_asr_stream_push_f32(handle, buf, len(samples), 16000))
        if finish:
            self._check(self.lib.nemo_speech_asr_stream_finish(handle))
        results = []
        while True:
            result = c.c_void_p()
            self._check(self.lib.nemo_speech_asr_stream_next(handle, c.byref(result)))
            if not result:
                break
            results.append(self._result(result))
        if finish:
            self.lib.nemo_speech_asr_stream_close(handle)
            del self.streams[key]
        return results

    def cancel_stream(self, key):
        handle = self.streams.pop(key, None)
        if handle is not None:
            self.lib.nemo_speech_asr_stream_close(handle)

    def close(self):
        for handle in self.streams.values():
            self.lib.nemo_speech_asr_stream_close(handle)
        self.streams.clear()
        if self.handle:
            self.lib.nemo_speech_asr_destroy(self.handle)
            self.handle = None


def segments_from_words(data, duration):
    words = data.get("words") or []
    if not words:
        return [dict(text=data["text"], start=0., end=duration)] if data["text"].strip() else []
    segments, current = [], []
    for word in words:
        current.append(word)
        if word["text"].rstrip().endswith((".", "?", "!")) or len(current) >= 24:
            segments.append(dict(text=" ".join(w["text"] for w in current), start=current[0]["start"], end=current[-1]["end"]))
            current = []
    if current:
        segments.append(dict(text=" ".join(w["text"] for w in current), start=current[0]["start"], end=current[-1]["end"]))
    return segments
