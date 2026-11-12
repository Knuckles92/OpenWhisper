"""The API engine's upload worker, readied while a recording is made.

The client's bookkeeping runs against a scripted stand-in for the worker
process; the last tests cross the real process boundary to a server on this
computer, and check that nothing reaches it before the upload.
"""
import http.server
import threading
import time
from types import SimpleNamespace

import pytest

from services import isolated, isolated_worker
from services.local_asr.process import SpeechProcess


class ScriptedWorker:
    """Stands in for ``SpeechProcess``: records requests and can stall or fail one."""

    started: list = []
    #: ``op`` -> an exception to raise, or an Event the request waits on first.
    script: dict = {}

    def __init__(self, *_args, **_kwargs):
        self.requests = []
        self.closed = False
        self.exit_code = None
        self.process = SimpleNamespace(poll=lambda: self.exit_code, terminate=self._exit)
        ScriptedWorker.started.append(self)

    def _exit(self):
        self.exit_code = -15

    def request(self, op, **payload):
        self.requests.append((op, payload))
        step = ScriptedWorker.script.get(op)
        if isinstance(step, threading.Event):
            assert step.wait(5), f"{op} was never released"
            if self.closed:
                raise RuntimeError("Transcription canceled")
        elif isinstance(step, Exception):
            raise step
        return {"text": "transcript"} if op == "openai_transcribe" else {"ready": True}

    def close(self):
        self.closed = True
        self._exit()

    @property
    def ops(self):
        return [op for op, _payload in self.requests]


@pytest.fixture
def workers(monkeypatch):
    ScriptedWorker.started = []
    ScriptedWorker.script = {}
    monkeypatch.setattr(isolated, "SpeechProcess", ScriptedWorker)
    return ScriptedWorker.started


@pytest.fixture
def client(workers):
    client = isolated.IsolatedOpenAIClient(
        api_key="sk-test", base_url="http://127.0.0.1:9/v1", read_timeout=30.)
    yield client
    client.close()


def _upload(client):
    return client.create(model="whisper-1", file=SimpleNamespace(name="a.wav"),
                         response_format="json")


def test_an_upload_uses_the_worker_readied_for_it(client, workers):
    client.warm()
    assert [worker.ops for worker in workers] == [["openai_prepare"]]
    _op, connection = workers[0].requests[0]
    assert (connection["api_key"], connection["base_url"], connection["read_timeout"]) == (
        "sk-test", "http://127.0.0.1:9/v1", 30.)

    assert _upload(client) == "transcript"

    assert [worker.ops for worker in workers] == [["openai_prepare", "openai_transcribe"]]
    assert workers[0].closed


def test_an_upload_without_a_readied_worker_starts_its_own(client, workers):
    assert _upload(client) == "transcript"
    assert _upload(client) == "transcript"
    assert [worker.ops for worker in workers] == [["openai_transcribe"], ["openai_transcribe"]]
    assert all(worker.closed for worker in workers)


def test_each_upload_takes_at_most_one_readied_worker(client, workers):
    client.warm()
    client.warm()
    assert len(workers) == 1
    _upload(client)
    _upload(client)
    assert [worker.ops for worker in workers] == [
        ["openai_prepare", "openai_transcribe"], ["openai_transcribe"]]
    client.warm()
    assert workers[2].ops == ["openai_prepare"] and not workers[2].closed


def test_a_failed_warm_up_leaves_the_upload_to_start_its_own(client, workers):
    ScriptedWorker.script["openai_prepare"] = RuntimeError("no SDK")
    client.warm()
    assert workers[0].closed

    assert _upload(client) == "transcript"
    assert workers[0].ops == ["openai_prepare"]
    assert workers[1].ops == ["openai_transcribe"]


def test_an_upload_waits_for_a_warm_up_still_under_way(client, workers):
    release = ScriptedWorker.script["openai_prepare"] = threading.Event()
    warming = threading.Thread(target=client.warm)
    warming.start()
    try:
        assert _wait_for(lambda: workers and workers[0].ops == ["openai_prepare"])
        result = []
        uploading = threading.Thread(target=lambda: result.append(_upload(client)))
        uploading.start()
        uploading.join(0.2)
        # Still waiting, and not on a second worker.
        assert uploading.is_alive() and len(workers) == 1
        release.set()
        uploading.join(5)
        assert result == ["transcript"]
        assert workers[0].ops == ["openai_prepare", "openai_transcribe"]
    finally:
        release.set()
        warming.join(5)
    assert len(workers) == 1


def test_a_readied_worker_that_died_is_replaced(client, workers):
    client.warm()
    workers[0].exit_code = 1

    assert _upload(client) == "transcript"
    assert workers[0].ops == ["openai_prepare"] and workers[0].closed
    assert workers[1].ops == ["openai_transcribe"]

    client.warm()
    workers[2].exit_code = 1
    client.warm()
    assert workers[2].closed and workers[3].ops == ["openai_prepare"]


def test_closing_the_client_ends_a_warm_up_and_the_upload_waiting_on_it(client, workers):
    release = ScriptedWorker.script["openai_prepare"] = threading.Event()
    warming = threading.Thread(target=client.warm)
    warming.start()
    try:
        assert _wait_for(lambda: workers and workers[0].ops == ["openai_prepare"])
        errors = []

        def upload():
            try:
                _upload(client)
            except RuntimeError as exc:
                errors.append(str(exc))

        uploading = threading.Thread(target=upload)
        uploading.start()
        uploading.join(0.1)
        client.close()
        release.set()
        uploading.join(5)
        assert errors == ["Transcription canceled"]
    finally:
        release.set()
        warming.join(5)
    assert all(worker.closed for worker in workers) and len(workers) == 1
    client.warm()
    assert len(workers) == 1


def test_a_readied_worker_nobody_uses_is_closed(workers, monkeypatch):
    monkeypatch.setattr(isolated, "STANDBY_IDLE_S", 0.05)
    client = isolated.IsolatedOpenAIClient(api_key="sk-test")
    try:
        client.warm()
        assert _wait_for(lambda: workers[0].closed)
        assert _upload(client) == "transcript"
        assert workers[1].ops == ["openai_transcribe"]
    finally:
        client.close()


def test_the_idle_limit_never_closes_a_worker_an_upload_took(client, workers):
    release = ScriptedWorker.script["openai_transcribe"] = threading.Event()
    client.warm()
    standby = client._standby
    result = []
    uploading = threading.Thread(target=lambda: result.append(_upload(client)))
    uploading.start()
    try:
        assert _wait_for(lambda: workers[0].ops == ["openai_prepare", "openai_transcribe"])
        assert standby.expiry.finished.is_set()
        # Even a limit that had already fired finds the worker taken.
        client._retire(standby)
        assert not workers[0].closed
    finally:
        release.set()
        uploading.join(5)
    assert result == ["transcript"] and len(workers) == 1


# ---- the worker's side ----

class FakeSdkClient:
    built: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.touched = []
        self.calls = []
        FakeSdkClient.built.append(self)

    @property
    def audio(self):
        self.touched.append("audio")
        return SimpleNamespace(transcriptions=SimpleNamespace(create=self._create))

    def _create(self, **call):
        self.calls.append(call)
        return SimpleNamespace(text="done")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed = True
        return False

    def close(self):
        self.closed = True


@pytest.fixture
def sdk(monkeypatch):
    import openai

    FakeSdkClient.built = []
    monkeypatch.setattr(openai, "OpenAI", FakeSdkClient)
    return FakeSdkClient.built


def _request(tmp_path, **changes):
    path = tmp_path / "a.wav"
    path.write_bytes(b"RIFF")
    return {"api_key": "sk", "base_url": "http://127.0.0.1:8000/v1", "read_timeout": 120.,
            "audio_path": str(path), "model": "whisper-1", "response_format": "json", **changes}


def test_the_worker_uploads_with_the_client_it_prepared(sdk, tmp_path):
    prepared = isolated_worker.PreparedUpload()
    request = _request(tmp_path)

    assert prepared.prepare(request) == {"ready": True}
    assert len(sdk) == 1 and sdk[0].touched == ["audio"] and sdk[0].calls == []
    assert sdk[0].kwargs["base_url"] == "http://127.0.0.1:8000/v1"
    assert sdk[0].kwargs["max_retries"] == 0

    result = isolated_worker.openai_transcribe(request, prepared.take(request))

    assert result == {"text": "done"}
    assert len(sdk) == 1 and len(sdk[0].calls) == 1 and sdk[0].closed
    # Taken once: a second upload in this worker builds its own.
    assert prepared.take(request) is None


@pytest.mark.parametrize("change", [
    {"api_key": "sk-other"}, {"base_url": None}, {"read_timeout": 600.},
])
def test_a_client_prepared_for_another_connection_is_not_used(sdk, tmp_path, change):
    prepared = isolated_worker.PreparedUpload()
    prepared.prepare(_request(tmp_path))
    request = _request(tmp_path, **change)

    assert prepared.take(request) is None
    assert sdk[0].closed

    assert isolated_worker.openai_transcribe(request, None) == {"text": "done"}
    assert sdk[1].kwargs["api_key"] == request["api_key"]
    assert sdk[1].kwargs["base_url"] == request["base_url"]


def test_preparing_again_closes_the_client_prepared_before(sdk, tmp_path):
    prepared = isolated_worker.PreparedUpload()
    prepared.prepare(_request(tmp_path))
    prepared.prepare(_request(tmp_path, api_key="sk-new"))
    assert sdk[0].closed and not sdk[1].closed


# ---- the backend and the recording's start ----

def test_the_api_backend_readies_its_client_and_never_raises(monkeypatch):
    from services import credentials
    from transcriber.openai_backend import OpenAIBackend

    warmed = []

    class Client:
        def __init__(self, **_connection):
            pass

        def warm(self):
            warmed.append(self)
            if len(warmed) == 2:
                raise OSError("cannot start a process")

        def close(self):
            pass

    monkeypatch.setattr("services.isolated.IsolatedOpenAIClient", Client)
    credentials.store().set("OPENAI_API_KEY", "sk-test")
    backend = OpenAIBackend("whisper-1")
    assert backend.prepares_for_transcription is True

    backend.prepare_for_transcription()
    backend.prepare_for_transcription()
    assert warmed == [backend.client, backend.client]

    credentials.store().delete("OPENAI_API_KEY")
    backend.prepare_for_transcription()
    assert backend.client is None and len(warmed) == 2


def test_only_an_engine_that_asks_is_prepared_when_a_recording_starts():
    from unittest.mock import Mock
    from services.runtime.transcription import TranscriptionRuntime
    from transcriber.base import TranscriptionBackend

    called = threading.Event()
    asking = SimpleNamespace(prepares_for_transcription=True,
                             prepare_for_transcription=called.set)
    TranscriptionRuntime._prepare_backend(asking)
    assert called.wait(5)

    # A stand-in answers every attribute; only a real True counts.
    stand_in = Mock()
    TranscriptionRuntime._prepare_backend(stand_in)
    TranscriptionRuntime._prepare_backend(None)
    stand_in.prepare_for_transcription.assert_not_called()
    assert TranscriptionBackend.prepares_for_transcription is False


# ---- across the real process boundary ----

class _CountingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _TranscriptionHandler)
        self.connections = 0
        self.uploads = []

    def get_request(self):
        self.connections += 1
        return super().get_request()


class _TranscriptionHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.server.uploads.append(
            (self.path, len(self.rfile.read(int(self.headers["Content-Length"])))))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"from the readied worker")


@pytest.fixture
def server():
    server = _CountingServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(5)


def test_readying_the_real_worker_sends_nothing_until_the_upload(server, tmp_path):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF" + bytes(4096))
    client = isolated.IsolatedOpenAIClient(
        api_key="sk-test", base_url=f"http://127.0.0.1:{server.server_port}/v1", read_timeout=30.)
    try:
        client.warm()
        standby = client._standby
        assert standby is not None and standby.ok
        assert standby.worker.process.poll() is None
        # The SDK is loaded and the client built, and the server has not
        # heard from this computer.
        time.sleep(0.2)
        assert server.connections == 0

        with open(audio, "rb") as handle:
            text = client.create(model="whisper-1", file=handle, response_format="text")

        assert text == "from the readied worker"
        assert server.connections == 1
        assert [path for path, _size in server.uploads] == ["/v1/audio/transcriptions"]
        assert standby.worker.process.poll() is not None
    finally:
        client.close()


def test_the_real_worker_answers_prepare_then_uploads_on_the_same_client(server, tmp_path):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF" + bytes(64))
    connection = {"api_key": "sk-test", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                  "read_timeout": 30.}
    worker = SpeechProcess(__import__("sys").executable, isolated=True)
    try:
        assert worker.request("openai_prepare", timeout=60., **connection) == {"ready": True}
        result = worker.request("openai_transcribe", audio_path=str(audio), model="whisper-1",
                                response_format="text", timeout=60., **connection)
        assert result == {"text": "from the readied worker"}
    finally:
        worker.close()


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()
