"""Parakeet Unified through transcribe.cpp's pinned ctypes binding, used only in a worker."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_SAMPLE_RATE = 16000
#: Parakeet Unified's buffered stream: 5.6 s of left context, 1.04 s chunks
#: and 1.04 s of lookahead, transcribe.cpp's default and its most accurate
#: preset. Live text starts after about 2.1 s of speech. On a GTX 1050 Ti
#: (2026-10-31) it got 15.8% of words wrong while speaking, against 54.5%
#: for Parakeet v3's 3 s window preview, and finished 0.3 s after release
#: whatever the length.
STREAM_GEOMETRY = dict(left_ms=5600, chunk_ms=1040, right_ms=1040)
#: transcribe.cpp log levels worth a line in openwhisper.log: warn and error.
_LOGGED_LEVELS = (2, 3)


class GpuUnavailable(RuntimeError):
    """The GPU could not run the model; the worker lets Auto retry on the CPU."""


def library_path(runtime: str) -> Path:
    name = ("transcribe.dll" if sys.platform == "win32"
            else "libtranscribe.dylib" if sys.platform == "darwin" else "libtranscribe.so")
    return Path(runtime) / "transcribe-native" / name


def choose_gpu(devices, *, required: bool):
    """The Vulkan device to run on, or None for the CPU.

    The dedicated card with the most memory. A laptop's integrated GPU is
    often listed first, and Auto never picks it; an explicit GPU choice
    takes it when there is nothing else.
    """
    gpus = [device for device in devices if device.kind == "vulkan"
            and device.device_type in ("gpu", "igpu")]
    pool = [device for device in gpus if device.device_type == "gpu"] or (gpus if required else [])
    return max(pool, key=lambda device: device.memory_total) if pool else None


def english(language):
    """The model's language for a request; it understands English only."""
    if not language or language.casefold() in ("auto", "english") or language.casefold().startswith("en-") \
            or language.casefold() == "en":
        return "en"
    raise ValueError("Parakeet Unified supports English only.")


class TranscribeRecognizer:
    """One loaded model and its session.

    transcribe.cpp runs one compute stream per model: while a stream is
    open, an offline decode on the same model is refused. A durable decode
    must never lose to a preview, so ``transcribe`` ends any open stream,
    and that stream's later pushes fail (the preview stops) instead of
    silently starting over without the speech before them.
    """

    def __init__(self, runtime: str, model_path: str, device: str):
        # The binding loads this library instead of searching for a provider
        # package, and its ggml backend modules from the same folder.
        os.environ["TRANSCRIBE_LIBRARY"] = str(library_path(runtime))
        import transcribe_cpp

        self._tc = transcribe_cpp
        transcribe_cpp.set_log_callback(_log)
        #: The GPU the model runs on, for the log; "" on the CPU.
        self.gpu_name = ""
        gpu = None if device == "cpu" else choose_gpu(transcribe_cpp.backends(), required=device != "auto")
        if gpu is None and device not in ("cpu", "auto"):
            raise RuntimeError(
                "Parakeet Unified found no Vulkan GPU. Update the graphics driver, or choose CPU."
            )
        try:
            self.model = transcribe_cpp.Model(
                model_path, backend="vulkan" if gpu else "cpu", device=gpu
            )
        except transcribe_cpp.TranscribeError as exc:
            if gpu is None:
                raise
            raise GpuUnavailable(f"Parakeet Unified could not load on the {gpu.description}: {exc}") from exc
        self.device = "vulkan" if gpu else "cpu"
        if gpu is not None:
            self.gpu_name = gpu.description
        self.session = self.model.session()
        self._stream = None
        self._stream_key = None
        #: Stream keys ended by a decode or a newer stream; they stay refused
        #: until the caller cancels them.
        self._interrupted = set()

    def transcribe(self, samples, language=None):
        language = english(language)
        self._end_stream(interrupted=True)
        if not samples:
            return dict(text="", segments=[])
        try:
            result = self.session.run(samples, language=language)
        except self._tc.InputTooShort:
            return dict(text="", segments=[])
        text = result.text.strip()
        segments = [dict(text=segment.text.strip(), start=segment.t0_ms / 1000, end=segment.t1_ms / 1000)
                    for segment in result.segments if segment.text.strip()]
        if text and not segments:
            segments = [dict(text=text, start=0., end=len(samples) / _SAMPLE_RATE)]
        return dict(text=text, segments=segments)

    def stream(self, key, samples, language=None, finish=False):
        """Push audio to the stream ``key`` and return its whole text so far.

        One event, a line revised by id like Moonshine's: the text is the
        stream's committed words plus its tentative tail. The first push for
        a key opens its stream; ``finish`` flushes the lookahead and closes it.
        """
        language = english(language)
        if key in self._interrupted:
            raise RuntimeError("The live preview stopped for a transcription.")
        if self._stream_key != key:
            self._end_stream(interrupted=True)
            options = self._tc.ParakeetBufferedStreamOptions(**STREAM_GEOMETRY)
            self._stream = self.session.stream(language=language, family=options)
            self._stream_key = key
        stream = self._stream
        try:
            update = stream.feed(samples) if samples else None
            if finish:
                update = stream.finalize()
            text = stream.text().display.strip()
        except Exception:
            self._end_stream()
            raise
        received = update.input_received_ms / 1000 if update is not None else 0.
        if finish:
            self._end_stream()
        return [dict(id="stream", text=text, start=0., end=received, final=finish)]

    def cancel_stream(self, key):
        if key == self._stream_key:
            self._end_stream()
        self._interrupted.discard(key)

    def _end_stream(self, *, interrupted=False):
        stream, key = self._stream, self._stream_key
        self._stream = self._stream_key = None
        if stream is None:
            return
        if interrupted:
            self._interrupted.add(key)
        stream.reset()

    def close(self):
        self._end_stream()
        self.session.close()
        self.model.close()


def _log(level, message):
    if level in _LOGGED_LEVELS and message.strip():
        print(f"transcribe.cpp: {message.rstrip()}", file=sys.stderr)
