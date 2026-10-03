"""Fault injection at the actual process boundary; no model or paid API needed."""
from concurrent.futures import ThreadPoolExecutor
import http.server
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

from services import isolated
from services.local_asr.process import SpeechProcess


WORKER = '''
import json, os, socket, time
from pathlib import Path
s = socket.create_connection(('127.0.0.1', int(os.environ['OPENWHISPER_WORKER_PORT'])))
s.sendall(os.environ['OPENWHISPER_WORKER_TOKEN'].encode('ascii'))
incoming, outgoing = s.makefile('r'), s.makefile('w')
for line in incoming:
    r = json.loads(line)
    op = r['op']
    if op == 'openai_transcribe':
        if r['audio_path'].endswith('reject.wav'):
            time.sleep(.2)
            outgoing.write(json.dumps({'id': r['id'], 'error': 'upload rejected'}) + '\\n')
            outgoing.flush()
            continue
        time.sleep(100)
    if op == 'download_model':
        outgoing.write(json.dumps({'id': r['id'], 'progress': [5, 10]}) + '\\n')
        outgoing.flush()
        time.sleep(100)
    if (op == 'whisper_load' and r['model'] == 'block-load') or (op == 'whisper_transcribe' and (r['audio_path'].endswith('block.wav') or os.environ.get('TEST_BLOCK_PREVIEW'))):
        Path(os.environ['TEST_WORKER_STARTED']).write_text(op)
        time.sleep(100)
    result = {}
    if op == 'whisper_transcribe':
        text = 'fresh result'
        if r.get('numpy_audio'):
            import numpy as np
            text = str(np.load(r['audio_path'], allow_pickle=False).tolist())
        result = {'segments': [{'text': text, 'start': 0., 'end': 1.}], 'info': {'language': 'en', 'language_probability': .9}}
    outgoing.write(json.dumps({'id': r['id'], 'result': result}) + '\\n')
    outgoing.flush()
'''


@pytest.fixture
def native_worker(monkeypatch, tmp_path):
    started = tmp_path / "worker-started"
    monkeypatch.setenv("TEST_WORKER_STARTED", str(started))
    processes = []
    popen = subprocess.Popen

    def launch(_command, **kwargs):
        process = popen([sys.executable, "-u", "-c", WORKER], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("services.local_asr.process.subprocess.Popen", launch)
    return started, processes


def wait_for_file(path):
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert path.exists(), "worker never reached the injected stall"


def test_native_cancel_reaps_stalled_worker_and_next_decode_reloads(native_worker):
    started, processes = native_worker
    model = isolated.IsolatedWhisperModel("base", device="cpu", local_files_only=True)
    try:
        model.load()
        with ThreadPoolExecutor() as executor:
            pending = executor.submit(model.transcribe, "block.wav")
            wait_for_file(started)
            before = time.monotonic()
            model.cancel()
            with pytest.raises(RuntimeError, match="canceled"):
                pending.result(timeout=3)
            assert time.monotonic() - before < 3
        assert processes[0].poll() is not None
        segments, info = model.transcribe("next.wav")
        assert [s.text for s in segments] == ["fresh result"]
        assert info.language == "en"
        assert len(processes) == 2
    finally:
        model.close()
    assert all(p.poll() is not None for p in processes)


def test_preview_numpy_audio_uses_same_cancelable_model_contract(native_worker, tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    model = isolated.IsolatedWhisperModel("base")
    try:
        segments, _ = model.transcribe(np.array([.25, -.5], np.float32), beam_size=1)
        assert list(segments)[0].text == "[0.25, -0.5]"
        assert list(tmp_path.glob("whisper-worker-*.npy")) == []
    finally:
        model.close()


def test_quit_during_native_model_construction_cannot_publish_model(native_worker, monkeypatch):
    from transcriber.local_backend import LocalWhisperBackend
    started, processes = native_worker
    monkeypatch.setattr("transcriber.local_backend.WhisperModel", isolated.IsolatedWhisperModel)
    monkeypatch.setattr(LocalWhisperBackend, "_detect_hardware", lambda self: ("cpu", "int8", "block-load"))
    monkeypatch.setattr("services.hf_access.is_model_cached", lambda _: True)
    backend = LocalWhisperBackend("block-load", load=False)
    with ThreadPoolExecutor() as executor:
        pending = executor.submit(backend._load_model)
        wait_for_file(started)
        before = time.monotonic()
        backend.cleanup()
        pending.result(timeout=3)
        assert time.monotonic() - before < 3
    assert backend.model is None
    assert not backend.is_available()
    assert all(p.poll() is not None for p in processes)


def test_external_startup_cancel_interrupts_native_model_load(native_worker, monkeypatch):
    from transcriber.local_backend import LocalWhisperBackend
    started, processes = native_worker
    monkeypatch.setattr("transcriber.local_backend.WhisperModel", isolated.IsolatedWhisperModel)
    monkeypatch.setattr(LocalWhisperBackend, "_detect_hardware", lambda self: ("cpu", "int8", "block-load"))
    monkeypatch.setattr("services.hf_access.is_model_cached", lambda _: True)
    backend = LocalWhisperBackend("block-load", load=False)
    cancel = threading.Event()
    try:
        with ThreadPoolExecutor() as executor:
            pending = executor.submit(backend._load_model, cancel_event=cancel)
            wait_for_file(started)
            cancel.set()
            pending.result(timeout=3)
        assert backend.model is None
        assert not backend.is_available()
        assert all(p.poll() is not None for p in processes)
    finally:
        backend.cleanup()


def test_deferred_meeting_stop_reaps_loading_native_model(native_worker, monkeypatch):
    from meeting.asr.engine import MeetingAsrEngine
    from transcriber.local_backend import LocalWhisperBackend
    started, processes = native_worker
    monkeypatch.setattr("transcriber.local_backend.WhisperModel", isolated.IsolatedWhisperModel)
    monkeypatch.setattr(LocalWhisperBackend, "_detect_hardware", lambda self: ("cpu", "int8", "block-load"))
    monkeypatch.setattr("services.hf_access.is_model_cached", lambda _: True)
    engine = MeetingAsrEngine("block-load", "m_cancel", object(), defer_load=True)
    assert processes == []
    try:
        with ThreadPoolExecutor() as executor:
            pending = executor.submit(engine.load_backend)
            wait_for_file(started)
            assert engine._backend is not None
            engine.stop()
            assert pending.result(timeout=3) is False
        assert engine._backend is None
        assert not engine.is_available
        assert engine._thread is None
        assert all(p.poll() is not None for p in processes)
    finally:
        engine.stop()


def test_hub_cancel_interrupts_download_without_waiting_for_next_byte(native_worker):
    from services.hf_access import download_model_files
    _, processes = native_worker
    progress = threading.Event()
    cancel = threading.Event()
    with ThreadPoolExecutor() as executor:
        pending = executor.submit(download_model_files, "base", lambda *_: progress.set(), cancel=cancel)
        assert progress.wait(5)
        before = time.monotonic()
        cancel.set()
        with pytest.raises(RuntimeError, match="canceled"):
            pending.result(timeout=3)
        assert time.monotonic() - before < 3
    assert all(p.poll() is not None for p in processes)


def test_request_deadline_reaps_worker(native_worker):
    _, processes = native_worker
    worker = SpeechProcess(sys.executable, isolated=True)
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            worker.request("download_model", model="base", timeout=.5)
    finally:
        worker.close()
    assert all(p.poll() is not None for p in processes)


def test_rejected_chunk_interrupts_other_stalled_uploads(native_worker, tmp_path):
    from transcriber.openai_backend import OpenAIBackend
    _, processes = native_worker
    paths = [tmp_path / name for name in ("stalled.wav", "reject.wav", "queued.wav")]
    for path in paths:
        path.write_bytes(b"RIFF" + b"0" * 64)
    backend = OpenAIBackend("whisper-1", api_key="test")
    backend.prepare_client()
    before = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="upload rejected"):
            backend._transcribe_chunks([str(path) for path in paths], "whisper-1")
        assert time.monotonic() - before < 3
        assert all(p.poll() is not None for p in processes)
    finally:
        backend.cleanup()


def test_cancel_recording_interrupts_preview_and_next_recording_works(native_worker, monkeypatch):
    from types import SimpleNamespace
    from services.streaming_transcriber import StreamingTranscriber

    started, processes = native_worker
    monkeypatch.setenv("TEST_BLOCK_PREVIEW", "1")
    model = isolated.IsolatedWhisperModel("base")
    preview = StreamingTranscriber(SimpleNamespace(model=model), chunk_duration_sec=.01)
    try:
        preview.start_streaming(16000, lambda *_: None)
        preview.feed_audio(np.full(320, .1, np.float32))
        wait_for_file(started)
        before = time.monotonic()
        preview.cancel_streaming()
        preview.worker_thread.join(3)
        assert not preview.worker_thread.is_alive()
        assert time.monotonic() - before < 3
        assert processes[0].poll() is not None
        monkeypatch.delenv("TEST_BLOCK_PREVIEW")
        segments, _ = model.transcribe("fresh.wav")
        assert list(segments)[0].text == "fresh result"
    finally:
        preview.cleanup()
        model.close()


def test_recovery_removes_only_preview_audio_owned_by_dead_processes(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr(isolated, "_process_exists", lambda pid: pid == 123)
    dead = tmp_path / "whisper-worker-456-stale.npy"
    alive = tmp_path / "whisper-worker-123-active.npy"
    unrelated = tmp_path / "whisper-worker-unrecognized.npy"
    for path in (dead, alive, unrelated):
        path.write_bytes(b"audio")
    isolated.cleanup_orphaned_preview_audio()
    assert not dead.exists()
    assert alive.exists() and unrelated.exists()


def test_worker_launch_uses_real_bootstrap_and_exits_without_ui():
    worker = SpeechProcess(sys.executable, isolated=True)
    try:
        assert worker.request("ping", timeout=15) == {"ready": True}
    finally:
        worker.close()
    assert worker.process.poll() is not None
    assert all(not reader.is_alive() for reader in worker._readers)


def test_cloud_cancel_interrupts_stalled_http_and_fresh_job_succeeds(monkeypatch, tmp_path):
    """Real SDK and localhost HTTP: cancellation must not wait for its timeout."""
    from transcriber.openai_backend import OpenAIBackend
    entered = threading.Event()
    release = threading.Event()
    block = [True]

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            if block[0]:
                entered.set()
                release.wait(10)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"fresh cloud result")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF" + b"0" * 64)
    backend = OpenAIBackend("whisper-1", api_key="test-only-local-server")
    try:
        with ThreadPoolExecutor() as executor:
            pending = executor.submit(backend.transcribe, str(audio))
            assert entered.wait(10), "SDK upload never reached the local test server"
            workers = list(backend.client._workers)
            before = time.monotonic()
            backend.cancel_transcription()
            with pytest.raises(RuntimeError):
                pending.result(timeout=3)
            assert time.monotonic() - before < 3
            assert all(w.process.poll() is not None for w in workers)
        block[0] = False
        assert backend.transcribe(str(audio)) == "fresh cloud result"
    finally:
        release.set()
        backend.cleanup()
        server.shutdown()
        server.server_close()
        server_thread.join(2)
