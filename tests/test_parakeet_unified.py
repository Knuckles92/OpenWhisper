"""Parakeet Unified on transcribe.cpp: catalog, runtime install, worker engine, stream final."""
import array
import io
import os
import sys
import tarfile
import threading
import types
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from config import config
from services import components
from services.components import ComponentError, ComponentId
from services.incremental_dictation import IncrementalDictation, stream_final_text
from services.local_asr import catalog, transcribe_engine, worker
from services.local_asr.languages import language_choices, selected_language
from services.streaming_transcriber import FinishedStream, NativeStreamingTranscriber


def _device(description, kind="vulkan", device_type="gpu", memory=4 << 30):
    return SimpleNamespace(description=description, kind=kind, device_type=device_type,
                           memory_total=memory)


IGPU = _device("AMD Radeon(TM) Graphics", device_type="igpu", memory=32 << 30)
SMALL = _device("NVIDIA GeForce GTX 1050 Ti", memory=4 << 30)
BIG = _device("NVIDIA GeForce RTX 2060", memory=6 << 30)
CPU = _device("AMD Ryzen 7", kind="cpu", device_type="cpu", memory=48 << 30)


# --- catalog ---------------------------------------------------------------

def test_unified_is_an_english_streaming_engine_on_the_transcribe_runtime():
    model = catalog.MODELS["parakeet-unified-en"]
    assert (model.backend, model.streaming, model.stream_final, model.meeting) == (
        "parakeet_unified", True, True, False
    )
    assert catalog.DEFAULT_MODELS["parakeet_unified"] == "parakeet-unified-en"
    assert config.MODEL_VALUE_MAP["Parakeet Unified"] == "parakeet_unified"
    for device in ("auto", "cpu", "cuda"):
        assert catalog.runtime_id("parakeet_unified", device) == catalog.TRANSCRIBE_RUNTIME
        # One runtime serves both; the worker resolves Auto against its GPUs.
        assert catalog.resolve_runtime("parakeet_unified", device) == (catalog.TRANSCRIBE_RUNTIME, device)
    assert language_choices("parakeet_unified") == ("en",)
    assert selected_language("parakeet_unified", "ru") == "en"
    assert "parakeet_unified" in config.STREAMING_NATIVE_PREVIEW_BACKENDS
    assert "parakeet_unified" in config.STREAMING_PREVIEW_BACKENDS


def test_unified_artifacts_pin_the_q8_gguf_and_upstream_release_archives():
    gguf, *notices = catalog.artifacts("parakeet-unified-en")["files"]
    assert gguf["name"] == "parakeet-unified-en-0.6b-Q8_0.gguf"
    # The NVIDIA Open Model License and its attribution notice stay beside the weights.
    assert [f["name"] for f in notices] == ["LICENSE", "NOTICE"]
    assert gguf["size_bytes"] == 731357568
    assert "/resolve/843b1b175ace04f5a89d379c79e2518c0d065b25/" in gguf["url"]
    platforms = catalog.runtime_catalog()[catalog.TRANSCRIBE_RUNTIME]["platforms"]
    assert set(platforms) == {"win_amd64", "linux_x86_64"}
    for tag, entry in platforms.items():
        native = entry["archives"][-1]
        assert native["extract"] == "transcribe-tar"
        assert native["url"].startswith("https://github.com/handy-computer/transcribe.cpp/releases/download/v0.4.1/")
        assert native["name"] == native["root"].replace("native-", "native-0.4.1-") + ".tar.gz"
        binding = entry["archives"][-2]
        assert binding["name"] == "transcribe_cpp-0.4.1-py3-none-any.whl"
        # Embedded Python reads the root; the app's interpreter a wheel tree.
        assert binding["extract"] == ("zip" if tag == "win_amd64" else "python-wheel")


# --- runtime install ------------------------------------------------------

def _release(path, root, names):
    with tarfile.open(path, "w:gz") as archive:
        for name in names:
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = 4
            archive.addfile(info, io.BytesIO(b"test"))


def test_release_archive_extracts_under_transcribe_native(tmp_path):
    root = "transcribe-native-linux-x86_64-cpu-vulkan"
    archive = tmp_path / "native.tar.gz"
    _release(archive, root, ["libtranscribe.so", "libggml-vulkan.so", "licenses/LICENSE"])
    staging = tmp_path / "out"
    staging.mkdir()
    components._extract_archive(dict(extract="transcribe-tar", root=root), str(archive), str(staging),
                                lambda *args: None, threading.Event())
    assert (staging / "transcribe-native" / "libtranscribe.so").read_bytes() == b"test"
    assert not (staging / root).exists()

    with patch.object(components, "current_platform_tag", return_value="linux_x86_64"):
        # The binding's wheel tree is part of the runtime too.
        with pytest.raises(ComponentError):
            components._validate_component_payload(ComponentId.ASR_TRANSCRIBE, str(staging))
        (staging / "site-packages" / "transcribe_cpp").mkdir(parents=True)
        (staging / "site-packages" / "transcribe_cpp" / "__init__.py").write_text("")
        components._validate_component_payload(ComponentId.ASR_TRANSCRIBE, str(staging))


def test_release_archive_from_another_root_is_refused(tmp_path):
    archive = tmp_path / "native.tar.gz"
    _release(archive, "transcribe-native-windows-x86_64-cuda", ["transcribe.dll"])
    with pytest.raises(ComponentError):
        components._extract_archive(
            dict(extract="transcribe-tar", root="transcribe-native-windows-x86_64-cpu-vulkan"),
            str(archive), str(tmp_path), lambda *args: None, threading.Event(),
        )


def test_windows_runtime_needs_python_binding_and_vulkan_library(tmp_path):
    names = ["python.exe", "python312.dll", "python312.zip", "transcribe_cpp/__init__.py",
             "transcribe-native/transcribe.dll"]
    for name in names:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"x")
    with patch.object(components, "current_platform_tag", return_value="win_amd64"):
        with pytest.raises(ComponentError):
            components._validate_component_payload(ComponentId.ASR_TRANSCRIBE, str(tmp_path))
        (tmp_path / "transcribe-native" / "ggml-vulkan.dll").write_bytes(b"x")
        components._validate_component_payload(ComponentId.ASR_TRANSCRIBE, str(tmp_path))
    assert (tmp_path / "python312._pth").read_text() == "python312.zip\n.\nimport site\n"


# --- worker engine ----------------------------------------------------------

class _Busy(Exception):
    pass


def _fake_binding(devices=(BIG, IGPU, CPU), *, gpu_fails=False):
    """A transcribe_cpp stand-in with the native rule that matters: one stream per model."""
    tc = types.ModuleType("transcribe_cpp")
    tc.TranscribeError = type("TranscribeError", (Exception,), {})
    tc.InputTooShort = type("InputTooShort", (tc.TranscribeError,), {})
    tc.ParakeetBufferedStreamOptions = lambda **geometry: ("buffered", geometry)
    tc.set_log_callback = lambda handler: None
    tc.backends = lambda: list(devices)
    tc.loaded = []

    class Stream:
        def __init__(self, session, options):
            self.session, self.options, self.heard, self.state = session, options, 0, "active"

        def feed(self, samples):
            self.heard += len(samples)
            return SimpleNamespace(input_received_ms=self.heard * 1000 // 16000)

        def finalize(self):
            self.state = "finished"
            return SimpleNamespace(input_received_ms=self.heard * 1000 // 16000)

        def text(self):
            words = " ".join(["word"] * (self.heard // 16000))
            return SimpleNamespace(display=words + ("." if self.state == "finished" else ""))

        def reset(self):
            if self.session.active is self:
                self.session.active = None

    class Session:
        def __init__(self):
            self.active = None

        def run(self, samples, language=None):
            if self.active is not None:
                raise _Busy("a stream is active on this model")
            if len(samples) < 160:
                raise tc.InputTooShort("too short")
            return SimpleNamespace(text=" Hello there. ", segments=[
                SimpleNamespace(text=" Hello there.", t0_ms=120, t1_ms=900)])

        def stream(self, language=None, family=None):
            if self.active is not None:
                raise _Busy("a stream is already active on this model")
            self.active = Stream(self, family)
            return self.active

        def close(self):
            pass

    class Model:
        def __init__(self, path, backend="auto", device=None):
            if gpu_fails and backend == "vulkan":
                raise tc.TranscribeError("vk::Device::createBuffer failed")
            tc.loaded.append((backend, device))
            self.session_ = Session()

        def session(self):
            return self.session_

        def close(self):
            pass

    tc.Model = Model
    return tc


@pytest.fixture
def binding(monkeypatch):
    def install(**options):
        fake = _fake_binding(**options)
        monkeypatch.setitem(sys.modules, "transcribe_cpp", fake)
        return fake
    monkeypatch.setenv("TRANSCRIBE_LIBRARY", "")
    return install


def _samples(seconds):
    return array.array("f", [0.1] * int(16000 * seconds))


def test_gpu_choice_prefers_the_biggest_dedicated_card_and_auto_skips_integrated():
    assert transcribe_engine.choose_gpu([IGPU, SMALL, BIG, CPU], required=False) is BIG
    assert transcribe_engine.choose_gpu([IGPU, CPU], required=False) is None
    assert transcribe_engine.choose_gpu([IGPU, CPU], required=True) is IGPU
    assert transcribe_engine.choose_gpu([CPU], required=True) is None


@pytest.mark.parametrize("language", [None, "auto", "en", "en-US", "EN", "English"])
def test_english_requests_reach_the_model_as_en(language):
    assert transcribe_engine.english(language) == "en"


def test_other_languages_are_refused():
    with pytest.raises(ValueError, match="English only"):
        transcribe_engine.english("ru")


def test_auto_loads_on_the_dedicated_gpu_through_vulkan(binding, tmp_path):
    fake = binding()
    engine = transcribe_engine.TranscribeRecognizer(str(tmp_path), "model.gguf", "auto")
    assert fake.loaded == [("vulkan", BIG)]
    assert (engine.device, engine.gpu_name) == ("vulkan", BIG.description)
    assert os.environ["TRANSCRIBE_LIBRARY"] == str(transcribe_engine.library_path(str(tmp_path)))


def test_auto_without_a_dedicated_gpu_runs_on_the_cpu(binding, tmp_path):
    fake = binding(devices=(IGPU, CPU))
    engine = transcribe_engine.TranscribeRecognizer(str(tmp_path), "model.gguf", "auto")
    assert fake.loaded == [("cpu", None)]
    assert (engine.device, engine.gpu_name) == ("cpu", "")


def test_explicit_gpu_without_one_is_an_error(binding, tmp_path):
    binding(devices=(CPU,))
    with pytest.raises(RuntimeError, match="no Vulkan GPU"):
        transcribe_engine.TranscribeRecognizer(str(tmp_path), "model.gguf", "cuda")


def test_a_gpu_that_cannot_load_lets_auto_retry_on_the_cpu(binding, tmp_path):
    binding(gpu_fails=True)
    state = worker.WorkerState()
    request = dict(backend="parakeet_unified", device="auto", runtime=str(tmp_path), model_path="m.gguf")
    with pytest.raises(worker.UnsupportedGpu, match="RTX 2060"):
        worker.load_engine(state, request)
    # The backend restarts the worker with "cpu" after an unsupported_gpu reply.
    result = worker.load_engine(state, dict(request, device="cpu"))
    assert result == {"device": "cpu"}


def test_worker_reports_the_vulkan_gpu(binding, tmp_path):
    binding()
    state = worker.WorkerState()
    result = worker.load_engine(state, dict(backend="parakeet_unified", device="auto",
                                             runtime=str(tmp_path), model_path="m.gguf"))
    assert result == {"device": "vulkan", "gpu": BIG.description}


def test_transcribe_returns_text_and_timed_segments(binding, tmp_path):
    binding()
    engine = transcribe_engine.TranscribeRecognizer(str(tmp_path), "m.gguf", "auto")
    assert engine.transcribe(_samples(1), "auto") == dict(
        text="Hello there.", segments=[dict(text="Hello there.", start=.12, end=.9)])
    # A window too short for the encoder has no words rather than an error.
    assert engine.transcribe(_samples(.005)) == dict(text="", segments=[])
    assert engine.transcribe(array.array("f")) == dict(text="", segments=[])


def test_stream_returns_one_revised_line_and_closes_on_finish(binding, tmp_path):
    binding()
    engine = transcribe_engine.TranscribeRecognizer(str(tmp_path), "m.gguf", "auto")
    first = engine.stream("preview", _samples(1), "auto")
    assert first == [dict(id="stream", text="word", start=0., end=1., final=False)]
    options = engine._stream.options
    assert options == ("buffered", transcribe_engine.STREAM_GEOMETRY)
    last = engine.stream("preview", _samples(1), "auto", finish=True)
    assert last == [dict(id="stream", text="word word.", start=0., end=2., final=True)]
    # Finished: the model is free for a file decode again.
    assert engine.transcribe(_samples(1))["text"] == "Hello there."


def test_a_decode_ends_the_open_preview_stream_which_then_stops(binding, tmp_path):
    binding()
    engine = transcribe_engine.TranscribeRecognizer(str(tmp_path), "m.gguf", "auto")
    engine.stream("preview", _samples(1))
    # The native library refuses run() during a stream; the durable decode wins.
    assert engine.transcribe(_samples(1))["text"] == "Hello there."
    # The preview never starts over without the speech before it.
    with pytest.raises(RuntimeError, match="live preview stopped"):
        engine.stream("preview", _samples(1))
    engine.cancel_stream("preview")
    assert engine.stream("preview", _samples(1))[0]["text"] == "word"


def test_a_newer_stream_supersedes_the_open_one(binding, tmp_path):
    binding()
    engine = transcribe_engine.TranscribeRecognizer(str(tmp_path), "m.gguf", "auto")
    engine.stream("old", _samples(1))
    assert engine.stream("new", _samples(2))[0]["text"] == "word word"
    with pytest.raises(RuntimeError):
        engine.stream("old", _samples(1))


# --- native preview: the finished stream ------------------------------------

class _UnifiedStream:
    """``backend.stream_audio`` reporting the audio it heard, like the worker."""

    model_name = "parakeet-unified-en"
    generation = 3

    def __init__(self):
        self.heard = 0
        self.ready = threading.Event()

    def stream_audio(self, session, audio, language=None, *, finish=False):
        self.heard += len(audio)
        self.ready.set()
        return [dict(id="stream", text="words so far", start=0., end=self.heard / 16000, final=finish)]

    def cancel_stream(self, session):
        pass


def _tone(seconds):
    t = np.arange(int(config.SAMPLE_RATE * seconds)) / config.SAMPLE_RATE
    return (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16)


def _native(backend):
    preview = NativeStreamingTranscriber(backend, update_interval_sec=0.75)
    preview.start_streaming(config.SAMPLE_RATE, lambda *_: None)
    return preview


def test_a_stopped_stream_reports_its_text_and_the_audio_it_heard():
    backend = _UnifiedStream()
    preview = _native(backend)
    for _ in range(3):
        preview.feed_audio(_tone(.5))
    assert preview.finished_stream() is None  # still streaming
    preview.stop_streaming()
    finished = preview.finished_stream()
    assert finished == FinishedStream(backend, 3, "words so far", pytest.approx(1.5, abs=.01))


def test_a_dropped_block_means_no_finished_stream():
    backend = _UnifiedStream()
    preview = _native(backend)
    preview.feed_audio(_tone(.5))
    preview._dropped = True  # the recorder found the queue full once
    preview.stop_streaming()
    assert preview.finished_stream() is None


def test_a_canceled_stream_has_no_finished_text():
    backend = _UnifiedStream()
    preview = _native(backend)
    preview.feed_audio(_tone(.8))
    assert backend.ready.wait(3)
    preview.cancel_streaming()
    preview.worker_thread.join(3)
    assert preview.finished_stream() is None


# --- the long dictation's transcript ----------------------------------------

def _finished(backend, audio_sec, generation=3):
    return FinishedStream(backend, generation, "Streamed words.", audio_sec)


@pytest.mark.parametrize("recorded, heard, generation, expected", [
    (45.0, 45.05, 3, "Streamed words."),
    (29.9, 29.9, 3, None),     # one window: the file decode is quick and more accurate
    (45.0, 44.0, 3, None),     # the stream missed audio (a microphone switch's gap)
    (45.0, 45.0, 4, None),     # the engine reloaded mid-dictation
])
def test_stream_final_needs_a_long_dictation_the_stream_fully_heard(recorded, heard, generation, expected):
    backend = _UnifiedStream()
    assert stream_final_text(backend, _finished(backend, heard, generation), recorded) == expected


def test_stream_final_is_only_for_stream_final_models_and_their_own_engine():
    backend = _UnifiedStream()
    nemotron = SimpleNamespace(model_name="nemotron-3.5", generation=3)
    assert stream_final_text(nemotron, _finished(nemotron, 45.), 45.) is None
    assert stream_final_text(backend, _finished(SimpleNamespace(), 45.), 45.) is None


class _FileBackend:
    model_name = "parakeet-unified-en"
    generation = 3

    def __init__(self):
        self.decoded = []

    def transcribe(self, path, recognition=None):
        self.decoded.append(path)
        return "File words."


def test_a_long_dictation_pastes_the_kept_stream_once(monkeypatch, tmp_path):
    recorded = str(tmp_path / "recorded_audio.wav")
    monkeypatch.setattr(config, "RECORDED_AUDIO_FILE", recorded)
    backend = _FileBackend()
    slot = IncrementalDictation()
    slot.keep_stream_final(_finished(backend, 40.), 40.)
    # An upload of another file never takes the dictation's stream...
    assert slot.transcribe(backend, str(tmp_path / "upload.wav")) == "File words."
    slot.keep_stream_final(_finished(backend, 40.), 40.)
    assert slot.transcribe(backend, recorded) == "Streamed words."
    # ...and it is used once: a retranscribe decodes the file.
    assert slot.transcribe(backend, recorded) == "File words."
    slot.keep_stream_final(_finished(backend, 40.), 40.)
    slot.discard()
    assert slot.transcribe(backend, recorded) == "File words."
    assert backend.decoded == [str(tmp_path / "upload.wav"), recorded, recorded]
