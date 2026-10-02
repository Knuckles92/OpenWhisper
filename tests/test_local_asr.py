import concurrent.futures
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from services.local_asr import cache
from services.local_asr.catalog import MODELS, BACKENDS, RUNTIME_IDS, artifacts, runtime_catalog, selected_model, selected_device
from transcriber.optional_backend import LocalSpeechBackend, SpeechDecoder


@pytest.fixture
def isolated_models(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "local_app_dir", lambda: str(tmp_path))
    data = b"verified model"
    spec = dict(repo="test/model", revision="pinned", files=[dict(
        name="weights.gguf", url="https://example.invalid/weights",
        size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
    )])
    monkeypatch.setattr(cache, "artifacts", lambda key: spec)
    def download(_url, sha, size, target, progress, cancel, **kwargs):
        assert sha == hashlib.sha256(data).hexdigest()
        assert size == len(data)
        Path(target).write_bytes(data)
    monkeypatch.setattr("services.components._download_verified", download)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    return spec


def test_every_optional_artifact_is_pinned_and_has_integrity_metadata():
    assert len(BACKENDS) == 5 and len(MODELS) == 7
    for key in MODELS:
        spec = artifacts(key)
        assert spec["revision"]
        for f in spec["files"]:
            assert len(f["sha256"]) == 64 and f["size_bytes"] > 0
            assert f["url"].startswith("https://")
            assert Path(f["name"]).name == f["name"]
    assert set(runtime_catalog()) == set(RUNTIME_IDS)
    for runtime in runtime_catalog().values():
        for platform, entry in runtime["platforms"].items():
            assert entry["platform"] == platform
            for f in entry["archives"]:
                assert len(f["sha256"]) == 64 and f["size_bytes"] > 0
                assert f["url"].startswith("https://github.com/") or platform == "win_amd64" or (
                    platform == "darwin_arm64" and f["extract"] == "python-wheel"
                    and f["url"].startswith("https://files.pythonhosted.org/")
                )


def test_linux_x86_64_offers_only_the_native_nvidia_runtimes():
    catalog = runtime_catalog()
    linux = {key for key, runtime in catalog.items() if "linux_x86_64" in runtime["platforms"]}
    assert linux == {"asr-nvidia-cpu", "asr-nvidia-cuda", "asr-nvidia-vulkan"}
    assert set(catalog["asr-nvidia-vulkan"]["platforms"]) == {"linux_x86_64"}
    for key, flavor in (("asr-nvidia-cpu", "cpu"), ("asr-nvidia-cuda", "cuda"), ("asr-nvidia-vulkan", "vulkan")):
        (archive,) = catalog[key]["platforms"]["linux_x86_64"]["archives"]
        assert archive["extract"] == "nemo-tar"
        assert archive["root"] == f"nemo-speech-0.1.0-linux-x86_64-{flavor}"
        assert archive["name"] == archive["root"] + ".tar.gz"
        assert archive["url"].endswith("/v0.1.0/" + archive["name"])


def test_linux_parakeet_offers_cpu_and_gpu_runtimes(monkeypatch):
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    monkeypatch.setattr("services.components.current_platform_tag", lambda: "linux_x86_64")
    monkeypatch.setattr("services.gpu_info.nvidia_gpu", lambda: None)
    monkeypatch.setattr("services.components.is_installed", lambda _: False)
    backend = LocalSpeechBackend("parakeet", device="cuda")
    backend.reload_model()
    assert backend.runtime_component == "asr-nvidia-cuda"
    assert "GPU runtime in Downloads" in backend.last_error
    qwen = LocalSpeechBackend("qwen_asr", device="cpu")
    qwen.reload_model()
    assert "not available on this platform" in qwen.last_error


def test_only_pre_turing_gpus_run_the_vulkan_runtime(monkeypatch):
    from services import gpu_info
    from services.local_asr.catalog import runtime_id

    monkeypatch.setattr("services.components.current_platform_tag", lambda: "linux_x86_64")
    for gpu, expected in (
        (None, "asr-nvidia-cuda"),
        (gpu_info.NvidiaGpu("NVIDIA GeForce RTX 2060", 6144, (7, 5)), "asr-nvidia-cuda"),
        (gpu_info.NvidiaGpu("NVIDIA GeForce GTX 1050 Ti", 4096, None), "asr-nvidia-cuda"),
        (gpu_info.NvidiaGpu("NVIDIA GeForce GTX 1050 Ti", 4096, (6, 1)), "asr-nvidia-vulkan"),
    ):
        monkeypatch.setattr(gpu_info, "nvidia_gpu", lambda gpu=gpu: gpu)
        assert runtime_id("parakeet", "cuda") == runtime_id("nemotron", "cuda") == expected
        assert runtime_id("parakeet", "cpu") == "asr-nvidia-cpu"
        assert runtime_id("qwen_asr", "cuda") == "asr-qwen"
    # Without a Vulkan release for the platform, the card keeps CUDA.
    monkeypatch.setattr("services.components.current_platform_tag", lambda: "win_amd64")
    assert runtime_id("parakeet", "cuda") == "asr-nvidia-cuda"


def test_auto_uses_the_vulkan_runtime_only_once_installed(monkeypatch):
    from services import gpu_info
    from services.local_asr.catalog import resolve_runtime

    installed = {"asr-nvidia-cpu"}
    monkeypatch.setattr("services.components.current_platform_tag", lambda: "linux_x86_64")
    monkeypatch.setattr("services.components.is_installed", lambda key: key in installed)
    # No GPU Acceleration: CTranslate2 can't count the card, Vulkan doesn't need it to.
    monkeypatch.setattr("ctranslate2.get_cuda_device_count", lambda: 0)
    monkeypatch.setattr(gpu_info, "nvidia_gpu", lambda: gpu_info.NvidiaGpu("GTX 1050 Ti", 4096, (6, 1)))
    assert resolve_runtime("parakeet", "auto") == ("asr-nvidia-cpu", "cpu")
    assert resolve_runtime("parakeet", "cuda") == ("asr-nvidia-vulkan", "cuda")
    installed.add("asr-nvidia-vulkan")
    assert resolve_runtime("nemotron", "auto") == ("asr-nvidia-vulkan", "cuda")
    assert resolve_runtime("nemotron", "cpu") == ("asr-nvidia-cpu", "cpu")
    # A Turing card keeps choosing exactly as before.
    installed.add("asr-nvidia-cuda")
    monkeypatch.setattr(gpu_info, "nvidia_gpu", lambda: gpu_info.NvidiaGpu("RTX 2060", 6144, (7, 5)))
    assert resolve_runtime("parakeet", "auto") == ("asr-nvidia-cpu", "cpu")
    monkeypatch.setattr("ctranslate2.get_cuda_device_count", lambda: 1)
    assert resolve_runtime("parakeet", "auto") == ("asr-nvidia-cuda", "cuda")


def test_vulkan_runs_on_the_nvidia_card_not_the_integrated_gpu():
    from services.local_asr.nvidia import nvidia_device_index

    assert nvidia_device_index(["Intel(R) UHD Graphics 630 (CFL GT2)", "NVIDIA GeForce GTX 1050 Ti"]) == 1
    assert nvidia_device_index(["GeForce GTX 1060 6GB"]) == 0
    with pytest.raises(RuntimeError, match="no NVIDIA GPU"):
        nvidia_device_index(["Intel(R) UHD Graphics 630 (CFL GT2)"])


def test_linux_vulkan_runtime_passes_the_nvidia_cards_index(monkeypatch, tmp_path):
    from services.local_asr import nvidia

    lib_dir = tmp_path / "nemo-speech" / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "libggml-vulkan.so").touch()
    # ggml lists the laptop's integrated GPU first, then the CPU, then the card.
    devices = [(nvidia._GGML_DEVICE_IGPU, b"Intel(R) UHD Graphics 630 (CFL GT2)"),
               (0, b"Intel(R) Core(TM) i5-9300H CPU"),
               (nvidia._GGML_DEVICE_GPU, b"NVIDIA GeForce GTX 1050 Ti")]
    requested = []

    def create(config, _handle):
        requested.append(nvidia.BackendConfig.from_address(config.backend).gpu)
        raise OSError("stop after create")

    ggml = SimpleNamespace(
        ggml_backend_dev_count=lambda: len(devices),
        ggml_backend_dev_get=lambda i: i,
        ggml_backend_dev_type=lambda i: devices[i][0],
        ggml_backend_dev_description=lambda i: devices[i][1],
    )

    class Speech:
        def __getattr__(self, name):
            return create if name == "nemo_speech_asr_create" else (lambda *args: None)

    def fake_cdll(name, mode=0):
        if name == str(lib_dir / "libggml.so"):
            return ggml
        if name.endswith("libnemo_speech_asr_c.so"):
            return Speech()
        raise OSError("no system libstdc++")

    monkeypatch.setattr(nvidia.sys, "platform", "linux")
    monkeypatch.setattr(nvidia.c, "CDLL", fake_cdll)
    monkeypatch.setattr(nvidia.c, "byref", lambda value: value)
    with pytest.raises(OSError, match="stop after create"):
        nvidia.NvidiaRecognizer(str(tmp_path), "model.gguf", "cuda")
    # The CUDA release, without ggml-vulkan, keeps asking for the first GPU.
    (lib_dir / "libggml-vulkan.so").unlink()
    with pytest.raises(OSError, match="stop after create"):
        nvidia.NvidiaRecognizer(str(tmp_path), "model.gguf", "cuda")
    assert requested == [1, 0]


def test_linux_worker_runs_on_the_apps_own_python(monkeypatch, tmp_path):
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("services.components.is_installed", lambda _: True)
    monkeypatch.setattr("services.components.component_dir", lambda key: str(tmp_path / key))
    monkeypatch.setattr(cache, "is_cached", lambda key: True)
    monkeypatch.setattr(cache, "load_path", lambda key: str(tmp_path / "model.gguf"))
    started = []

    class FakeProcess:
        def __init__(self, python):
            started.append(python)
            self.process = Mock(poll=Mock(return_value=None))

        def request(self, op, **kwargs):
            return {"device": kwargs["device"]}

        def close(self):
            pass

    monkeypatch.setattr("services.local_asr.process.SpeechProcess", FakeProcess)
    backend = LocalSpeechBackend("parakeet", device="cpu")
    backend.reload_model()
    assert started == [sys.executable]
    assert backend.is_available()


def test_linux_recognizer_loads_the_release_library_after_system_libstdcxx(monkeypatch, tmp_path):
    from services.local_asr import nvidia

    loaded = []

    def fake_cdll(name, mode=0):
        loaded.append((name, mode))
        if name.endswith("libnemo_speech_asr_c.so"):
            raise OSError("stop after loading")
        return object()

    monkeypatch.setattr(nvidia.sys, "platform", "linux")
    monkeypatch.setattr(nvidia.c, "CDLL", fake_cdll)
    with pytest.raises(OSError, match="stop after loading"):
        nvidia.NvidiaRecognizer(str(tmp_path), "model.gguf", "cpu")
    library = str(tmp_path / "nemo-speech" / "lib" / "libnemo_speech_asr_c.so")
    assert loaded == [("libstdc++.so.6", nvidia.c.RTLD_GLOBAL), (library, 0)]


def test_linux_gpu_runtime_without_the_driver_says_so(monkeypatch, tmp_path):
    from services.local_asr import nvidia

    def fake_cdll(name, mode=0):
        if name.endswith("libnemo_speech_asr_c.so"):
            raise OSError("libcuda.so.1: cannot open shared object file: No such file or directory")
        raise OSError("no system libstdc++")

    monkeypatch.setattr(nvidia.sys, "platform", "linux")
    monkeypatch.setattr(nvidia.c, "CDLL", fake_cdll)
    with pytest.raises(RuntimeError, match="needs the NVIDIA driver"):
        nvidia.NvidiaRecognizer(str(tmp_path), "model.gguf", "cuda")


def test_settings_never_cross_model_families_or_change_whisper():
    settings = dict(local_asr_models={"parakeet": "qwen-1.7b"}, whisper_model="turbo")
    assert selected_model("parakeet", settings) == "parakeet-v3"
    assert settings["whisper_model"] == "turbo"
    assert selected_device("moonshine", {"local_asr_devices": {"moonshine": "cuda"}}) == "cpu"
    assert selected_device("qwen_asr", {"local_asr_devices": []}) == "auto"


def test_partial_cache_is_not_loadable_and_download_finishes_atomically(isolated_models):
    key = "parakeet-v3"
    partial = cache.model_dir(key).with_name(key + ".partial")
    partial.mkdir(parents=True)
    (partial / "weights.gguf").write_bytes(b"broken")
    assert not cache.is_cached(key)
    cache.download(key)
    assert cache.is_cached(key)
    assert Path(cache.load_path(key)).name == "weights.gguf"
    assert not partial.exists()
    (cache.model_dir(key)/"weights.gguf").write_bytes(b"truncated")
    assert not cache.is_cached(key)


def test_hard_offline_blocks_all_model_hosts(isolated_models, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    with pytest.raises(RuntimeError, match="HF_HUB_OFFLINE"):
        cache.download("moonshine-small")
    assert not cache.is_cached("moonshine-small")


def test_failed_download_cannot_publish_cache(isolated_models, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("network interrupted")
    monkeypatch.setattr("services.components._download_verified", fail)
    with pytest.raises(OSError):
        cache.download("parakeet-v3")
    assert not cache.is_cached("parakeet-v3")


def test_cache_swap_rolls_back_previous_tree(isolated_models, monkeypatch):
    target = cache.model_dir("parakeet-v3")
    target.mkdir(parents=True)
    (target/"old.txt").write_text("keep")
    replace = cache.os.replace
    def fail_publish(source, destination):
        if str(source).endswith(".partial"):
            raise OSError("locked")
        return replace(source, destination)
    monkeypatch.setattr(cache.os, "replace", fail_publish)
    with pytest.raises(OSError, match="locked"):
        cache.download("parakeet-v3")
    assert (target/"old.txt").read_text() == "keep"


def test_missing_runtime_does_not_import_optional_packages(monkeypatch):
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    monkeypatch.setattr("services.components.is_installed", lambda _: False)
    backend = LocalSpeechBackend("qwen_asr", device="cpu")
    backend.reload_model()
    assert not backend.is_available()
    assert "runtime" in backend.device_info
    assert "qwen_asr" not in sys.modules
    assert backend.large_file_size_mb("long-meeting.wav") is None


def test_auto_can_use_cpu_runtime_but_explicit_cuda_cannot(monkeypatch):
    monkeypatch.setattr("services.components.current_platform_tag", lambda: "win_amd64")
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    monkeypatch.setattr("ctranslate2.get_cuda_device_count", lambda: 1)
    monkeypatch.setattr("services.components.is_installed", lambda key: key.endswith("cpu"))
    monkeypatch.setattr(cache, "is_cached", lambda key: False)
    backend = LocalSpeechBackend("parakeet")
    backend.reload_model()
    assert backend.runtime_component == "asr-nvidia-cpu"
    explicit = LocalSpeechBackend("parakeet", device="cuda")
    explicit.reload_model()
    assert explicit.runtime_component == "asr-nvidia-cuda"
    assert "GPU" in explicit.last_error


def test_unsupported_runtime_does_not_point_to_downloads(monkeypatch):
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    monkeypatch.setattr("services.components.current_platform_tag", lambda: "darwin_arm64")
    monkeypatch.setattr("services.components.is_installed", lambda _: False)
    backend = LocalSpeechBackend("qwen_asr", device="cpu")
    backend.reload_model()
    assert backend.runtime_component is None
    assert "not available on this platform" in backend.last_error
    assert "Downloads" not in backend.last_error


def test_mac_parakeet_offers_native_runtime(monkeypatch):
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    monkeypatch.setattr("services.components.current_platform_tag", lambda: "darwin_arm64")
    monkeypatch.setattr("services.components.is_installed", lambda _: False)
    backend = LocalSpeechBackend("parakeet", device="cpu")
    backend.reload_model()
    assert backend.runtime_component == "asr-nvidia-cpu"
    assert "CPU runtime in Downloads" in backend.last_error


def test_cancel_discards_worker_and_speech_decoder(monkeypatch):
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    backend = LocalSpeechBackend("parakeet")
    process = Mock()
    backend._process, backend.model = process, SpeechDecoder(backend)
    backend.cancel_transcription()
    assert backend.should_cancel
    assert backend.model is None and backend._process is None
    process.close.assert_called_once()


def test_cancel_during_load_cannot_publish_stale_model(monkeypatch):
    entered, resume = threading.Event(), threading.Event()
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    monkeypatch.setattr("services.components.is_installed", lambda _: True)
    monkeypatch.setattr("services.components.component_dir", lambda _: ".")
    monkeypatch.setattr(cache, "is_cached", lambda _: True)
    monkeypatch.setattr(cache, "load_path", lambda _: "model.gguf")
    class Process:
        def __init__(self, _python):
            self.process = SimpleNamespace(poll=lambda: None)
        def request(self, *args, **kwargs):
            entered.set()
            resume.wait(2)
            return {"device": "cpu"}
        def close(self):
            pass
    monkeypatch.setattr("services.local_asr.process.SpeechProcess", Process)
    backend = LocalSpeechBackend("parakeet", device="cpu")
    with concurrent.futures.ThreadPoolExecutor() as executor:
        loading = executor.submit(backend.reload_model)
        assert entered.wait(2)
        backend.cancel_transcription()
        resume.set()
        loading.result(2)
    assert backend.model is None and not backend.is_available()


def test_audio_windows_preserve_resampled_stereo_samples(tmp_path):
    import wave
    from faster_whisper.audio import decode_audio
    from services.local_asr.audio import windows
    rate = 44100
    t = np.arange(rate*65)/rate
    mono = (np.sin(2*np.pi*321*t)*16000).astype(np.int16)
    path = tmp_path/"audio.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.column_stack((mono, mono)).tobytes())
    chunks = list(windows(str(path)))
    joined = np.concatenate([audio for _, audio in chunks])
    expected = decode_audio(str(path), sampling_rate=16000)
    assert len(joined) == len(expected)
    assert np.max(np.abs(joined - expected)) < 1e-4
    assert all(len(audio) <= 30*16000 for _, audio in chunks)
    assert chunks[1][0] == len(chunks[0][1])/16000


def test_chunk_results_keep_offsets_and_silence_is_empty(monkeypatch):
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    backend = LocalSpeechBackend("parakeet")
    backend._recognize = Mock(return_value=dict(text="one", segments=[dict(text="one",start=0.,end=1.)]))
    assert backend._transcribe_audio(np.zeros(48000, np.float32)) == dict(text="",segments=[])
    backend._recognize.assert_not_called()
    audio = np.full(61*16000, .1, np.float32)
    result = backend._transcribe_audio(audio)
    assert result["text"] == "one one one"
    assert [round(s["start"],1) for s in result["segments"]] == [0.,24.1,48.2]


def _worker(monkeypatch, script):
    from services.local_asr.process import SpeechProcess
    popen = subprocess.Popen
    monkeypatch.setattr("services.local_asr.process.subprocess.Popen",
                        lambda args, **kwargs: popen([sys.executable, "-u", "-c", script], **kwargs))
    return SpeechProcess(sys.executable)


def test_worker_ignores_stale_reply_ids(monkeypatch):
    worker = _worker(monkeypatch, 'import json,sys\nfor line in sys.stdin:\n r=json.loads(line)\n print(json.dumps({"id":0,"result":{"text":"stale"}}),flush=True)\n print(json.dumps({"id":r["id"],"result":{"text":"correct"}}),flush=True)')
    try:
        assert worker.request("transcribe", timeout=3) == {"text": "correct"}
    finally:
        worker.close()


def test_cancel_interrupts_unresponsive_native_worker(monkeypatch):
    worker = _worker(monkeypatch, 'import sys,time\nfor line in sys.stdin: time.sleep(100)')
    with concurrent.futures.ThreadPoolExecutor() as executor:
        pending = executor.submit(worker.request, "transcribe", timeout=10)
        time.sleep(.1)
        started = time.monotonic()
        worker.close()
        with pytest.raises(RuntimeError, match="canceled"):
            pending.result(3)
    assert time.monotonic()-started < 3
    assert worker.process.poll() is not None


def test_timeout_kills_worker_and_reports_reload(monkeypatch):
    worker = _worker(monkeypatch, 'import sys,time\nfor line in sys.stdin: time.sleep(100)')
    with pytest.raises(RuntimeError, match="timed out"):
        worker.request("transcribe", timeout=.15)
    assert worker.process.poll() is not None


def test_crash_reports_worker_error(monkeypatch):
    worker = _worker(monkeypatch, 'import sys\nsys.stderr.write("native load failed\\n"); sys.stderr.flush()\nsys.exit(3)')
    try:
        with pytest.raises(RuntimeError, match="Speech worker stopped"):
            worker.request("load", timeout=3)
    finally:
        worker.close()


def test_meeting_model_selection_preserves_whisper_fallback():
    from services.settings import resolve_meeting_whisper_model
    assert resolve_meeting_whisper_model({"meeting_asr_model":"nemotron-3.5"}) == "nemotron-3.5"
    assert resolve_meeting_whisper_model({"meeting_asr_model":"qwen-1.7b","meeting_whisper_model":"small"}) == "small"


def test_moonshine_sessions_finalize_and_close(monkeypatch):
    from services.local_asr.moonshine import MoonshineRecognizer
    stream = Mock()
    line = SimpleNamespace(text="last word",start_time=1.,duration=.5,line_id=1,is_complete=False)
    stream.stop.return_value = SimpleNamespace(lines=[line])
    engine = MoonshineRecognizer.__new__(MoonshineRecognizer)
    engine.engine = Mock()
    engine.engine.create_stream.return_value = stream
    engine.streams = {}
    result = engine.transcribe([.2]*16000, "en")
    assert result["text"] == "last word"
    assert result["segments"] == [dict(text="last word",start=1.,end=1.5)]
    stream.stop.assert_called_once()
    stream.close.assert_called_once()
    assert engine.streams == {}


def test_moonshine_rejects_unsupported_language_before_opening_stream():
    from services.local_asr.moonshine import MoonshineRecognizer
    engine = MoonshineRecognizer.__new__(MoonshineRecognizer)
    engine.engine, engine.streams = Mock(), {}
    with pytest.raises(ValueError, match="English"):
        engine.stream("mic", [], "es")
    engine.engine.create_stream.assert_not_called()



def test_controls_preserve_whisper_and_other_speech_families(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QApplication
    from services.settings import SettingsManager
    from ui_qt.widgets import local_engine_controls as controls
    _app = QApplication.instance() or QApplication([])
    manager = SettingsManager(str(tmp_path/"settings.json"))
    manager.update_settings({"whisper_model":"turbo","whisper_device":"cuda","whisper_compute_type":"float16"})
    monkeypatch.setattr(controls, "settings_manager", manager)
    widget = controls.LocalEngineControls()
    widget.set_backend("qwen_asr")
    widget.model_combo.setCurrentIndex(widget.model_combo.findData("qwen-1.7b"))
    widget.device_combo.setCurrentText("cpu")
    widget.language_combo.setCurrentText("Auto")
    widget.set_backend("moonshine")
    assert widget.device_combo.currentText() == "cpu" and not widget.device_combo.isEnabled()
    widget.set_backend("qwen_asr")
    widget.set_values("base", "cuda", "int8")
    assert widget.model_combo.currentData() == "qwen-1.7b"
    assert widget.device_combo.currentText() == "cpu"
    assert widget.language_combo.currentText() == "Auto"
    widget.set_backend("local_whisper")
    assert widget.model_combo.currentText() == "turbo"
    assert widget.compute_combo.currentText() == "float16"
    widget.close()


def test_meeting_preview_copies_capture_and_flushes_last_audio():
    from meeting.asr.preview import MeetingSpeechPreview
    from meeting.interfaces import CaptureBlock
    got_result = threading.Event()
    received = []
    class Backend:
        def stream_audio(self, channel, audio, language=None, *, finish=False):
            received.append((channel, audio.copy(), finish))
            return [dict(text="preview", start=0, end=.75, final=finish)]
        def cancel_stream(self, _channel):
            pass
    preview = MeetingSpeechPreview(Backend(), lambda _: got_result.set(), lambda: False)
    frames = np.full(12000, 3000, np.int16)
    preview.feed(CaptureBlock("mic",frames,16000,100.),10.)
    assert got_result.wait(3)
    preview.feed(CaptureBlock("mic",frames[:1600],16000,100.75),10.75)
    preview.stop()
    assert any(not finish for _, _, finish in received)
    assert any(finish for _, _, finish in received)
    assert received[0][1].mean() == pytest.approx(3000/32768, abs=1e-4)


def test_native_runtime_swap_retries_short_windows_lock(tmp_path, monkeypatch):
    from services import components
    source, target = tmp_path/"staging", tmp_path/"installed"
    source.mkdir()
    (source/"model").write_text("ready")
    replace = components.os.replace
    attempts = []
    class Cancel:
        def is_set(self):
            return False
        def wait(self, _delay):
            pass
    def locked_twice(src, dst):
        attempts.append(True)
        if len(attempts) < 3:
            raise PermissionError("scanner still reading files")
        replace(src, dst)
    monkeypatch.setattr(components.os, "replace", locked_twice)
    components._replace_speech_runtime(str(source), str(target), Cancel())
    assert (target/"model").read_text() == "ready"
    assert len(attempts) == 3


def test_native_runtime_swap_cancellation_keeps_staging(tmp_path):
    from services import components
    source, target = tmp_path/"staging", tmp_path/"installed"
    source.mkdir()
    canceled = threading.Event()
    canceled.set()
    with pytest.raises(components.ComponentCanceled):
        components._replace_speech_runtime(str(source), str(target), canceled)
    assert source.exists() and not target.exists()



def test_canceled_job_does_not_restart_speech_worker(monkeypatch):
    from transcriber.optional_backend import LocalSpeechBackend
    backend = LocalSpeechBackend("parakeet")
    reload = Mock()
    monkeypatch.setattr(backend, "reload_model", reload)
    backend.cancel_transcription()
    with pytest.raises(RuntimeError, match="canceled"):
        backend.transcribe("unused.wav")
    reload.assert_not_called()
    assert not backend.is_transcribing


@pytest.mark.parametrize("backend", ["Local Whisper", "Parakeet", "Qwen3-ASR", "Nemotron Streaming", "Moonshine"])
def test_main_window_keeps_local_controls_visible(backend):
    from config import config
    from ui_qt.main_window import MainWindow
    window = SimpleNamespace(transcription_tabs=[Mock(), Mock()])
    assert backend in config.MODEL_VALUE_MAP
    MainWindow._apply_local_engine_visibility(window, backend)
    for tab in window.transcription_tabs:
        tab.set_local_engine_visible.assert_called_once_with(True)


def test_cancel_during_runtime_check_does_not_start_worker(monkeypatch):
    from transcriber.optional_backend import LocalSpeechBackend
    from services import components
    from services.local_asr import cache, process
    backend = LocalSpeechBackend("parakeet", "parakeet-v3", "cpu")
    def installed(_component):
        backend.cancel_transcription()
        return True
    worker = Mock()
    monkeypatch.setattr(components, "is_installed", installed)
    monkeypatch.setattr(cache, "is_cached", lambda _: True)
    monkeypatch.setattr(process, "SpeechProcess", worker)
    backend.reload_model()
    worker.assert_not_called()
    assert not backend.is_available()


def test_packaged_worker_entry_does_not_start_ui():
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / 'main.py'), '--local-asr-worker'],
        input='{"id": 1, "op": "cancel_stream", "session": "test"}\n{"id": 2, "op": "shutdown"}\n',
        text=True, capture_output=True, timeout=10,
    )
    assert completed.returncode == 0
    response = json.loads(completed.stdout)
    assert response['id'] == 1 and 'error' in response
    assert 'Starting OpenWhisper' not in completed.stderr


def test_frozen_linux_worker_relaunches_app(monkeypatch):
    import io
    from services.local_asr.process import SpeechProcess
    child = Mock(stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO())
    child.poll.return_value = 0
    popen = Mock(return_value=child)
    monkeypatch.setattr('services.local_asr.process.subprocess.Popen', popen)
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    worker = SpeechProcess('/opt/openwhisper/OpenWhisper')
    try:
        assert popen.call_args.args[0] == ['/opt/openwhisper/OpenWhisper', '--local-asr-worker']
    finally:
        worker.close()


def test_frozen_mac_worker_relaunches_app_without_python_flags(monkeypatch):
    import io
    from services.local_asr.process import SpeechProcess
    child = Mock(stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO())
    child.poll.return_value = 0
    popen = Mock(return_value=child)
    monkeypatch.setattr('services.local_asr.process.subprocess.Popen', popen)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    worker = SpeechProcess('/Applications/OpenWhisper.app/Contents/MacOS/OpenWhisper')
    try:
        assert popen.call_args.args[0] == [
            '/Applications/OpenWhisper.app/Contents/MacOS/OpenWhisper', '--local-asr-worker',
        ]
    finally:
        worker.close()
