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
    assert len(BACKENDS) == 4 and len(MODELS) == 6
    for key in MODELS:
        spec = artifacts(key)
        assert spec["revision"]
        for f in spec["files"]:
            assert len(f["sha256"]) == 64 and f["size_bytes"] > 0
            assert f["url"].startswith("https://")
            assert Path(f["name"]).name == f["name"]
    assert set(runtime_catalog()) == set(RUNTIME_IDS)
    for runtime in runtime_catalog().values():
        for f in runtime["platforms"]["win_amd64"]["archives"]:
            assert len(f["sha256"]) == 64 and f["size_bytes"] > 0


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
    assert not backend.requires_file_splitting


def test_auto_can_use_cpu_runtime_but_explicit_cuda_cannot(monkeypatch):
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
    app = QApplication.instance() or QApplication([])
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
