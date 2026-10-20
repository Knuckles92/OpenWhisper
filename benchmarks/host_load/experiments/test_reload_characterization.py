"""Document the current worker-reload boundary, not a promise of lossless reload."""
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from services.application_controller import ApplicationController, _engine_switch_refusal
from services.remote_asr.engines import SpeechWorkerEngine
from transcriber.optional_backend import LocalSpeechBackend


class InterruptibleWorker:
    """Controlled request/close contract; no model weights or subprocess timing."""
    def __init__(self):
        self.started = threading.Event()
        self.closed = threading.Event()
        self.process = SimpleNamespace(poll=lambda: None)

    def request(self, *args, **kwargs):
        self.started.set()
        assert self.closed.wait(5)
        raise RuntimeError("worker closed during decode")

    def close(self):
        self.closed.set()


@pytest.mark.parametrize("operation", ["transcribe", "stream"])
def test_remote_decode_does_not_block_controller_switch_and_cleanup_interrupts(operation):
    backend = LocalSpeechBackend("parakeet", model_name="parakeet-v3")
    worker = InterruptibleWorker()
    backend._process = worker
    backend.model = object()
    engine = SpeechWorkerEngine(backend)
    controller = SimpleNamespace(current_backend=backend, transcription_runtime=None,
        is_meeting_active=lambda: False, recorder=SimpleNamespace(is_recording=False))
    controller.is_transcribing = lambda: ApplicationController.is_transcribing(controller)
    outcomes = []

    def decode():
        try:
            if operation == "transcribe":
                engine.transcribe(np.ones(1600, np.float32), "en")
            else:
                engine.stream("test-session", np.ones(1600, np.float32), "en", False)
        except Exception as error:
            outcomes.append(error)

    thread = threading.Thread(target=decode)
    try:
        thread.start()
        assert worker.started.wait(3)
        assert backend._decode_lock.locked()
        assert not controller.is_transcribing()
        assert _engine_switch_refusal(controller, "host") is None
        backend.cleanup()
        thread.join(3)
        assert not thread.is_alive()
        assert len(outcomes) == 1
        assert str(outcomes[0]) == "worker closed during decode"
    finally:
        worker.close()
        thread.join(3)
        backend.cleanup()


def test_same_model_cleanup_changes_worker_generation_but_not_host_identity(monkeypatch):
    from services.settings import settings_manager
    monkeypatch.setattr(settings_manager, "load_all_settings", lambda: {})
    backend = LocalSpeechBackend("parakeet", model_name="parakeet-v3")
    engine = SpeechWorkerEngine(backend)
    before_identity, before_generation = engine.identity, backend.generation
    backend.cleanup()
    assert backend.generation == before_generation + 1
    assert engine.identity == before_identity
