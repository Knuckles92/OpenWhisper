"""Bindings to the pinned NeMo-Speech.cpp v1 C ABI, used only in a worker."""
from __future__ import annotations

import array
import ctypes as c
import os
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


class NvidiaRecognizer:
    def __init__(self, runtime: str, model_path: str, device: str):
        bin_dir = str(Path(runtime) / "bin")
        self._dll_dir = os.add_dll_directory(bin_dir)
        self.lib = c.CDLL(str(Path(bin_dir) / "nemo_speech_asr_c.dll"))
        self._bind()
        backend = BackendConfig(c.sizeof(BackendConfig), 0 if device == "cuda" else -1)
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

