"""Cleanup must not hold the paste or the job slot past its budget.

The HTTP timeout bounds each socket read, not the request. OpenRouter sends
200 as soon as the provider accepts and the body can then arrive a few bytes
at a time, so an 8 s timeout let a dictation sit in "Cleaning up..." for as
long as the model liked, and a Cancel there left the job slot held: every
later record press was refused as "already in progress".
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from openai import OpenAI

from services import transcript_cleanup
from services.transcript_cleanup import CANCELED_REASON, TranscriptCleanup

_COMPLETION = json.dumps({
    "id": "x", "object": "chat.completion", "created": 0, "model": "m",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "Cleaned."}}],
}).encode()


@pytest.fixture
def trickling_endpoint():
    """A chat endpoint that answers 200 at once, then one space per 0.1 s.

    The body completes after 5 s, so a regression fails instead of hanging.
    """
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            finish_at = time.monotonic() + 5.0
            try:
                while not stop.wait(0.1) and time.monotonic() < finish_at:
                    self.wfile.write(b"1\r\n \r\n")
                    self.wfile.flush()
                self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(_COMPLETION), _COMPLETION))
                self.wfile.flush()
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        stop.set()
        server.shutdown()
        server.server_close()


def _cleaner(**kwargs) -> TranscriptCleanup:
    return TranscriptCleanup(provider="openai", model="gpt-test", api_key="k", **kwargs)


def _stalled_generate(monkeypatch):
    """Replace the request with one that runs until ``release`` is set."""
    started, release = threading.Event(), threading.Event()

    def generate(*_args, **_kwargs):
        started.set()
        release.wait(10)
        return SimpleNamespace(text="Too late.")

    monkeypatch.setattr(transcript_cleanup, "generate", generate)
    return started, release


def test_trickling_response_ends_at_the_attempts_budget(trickling_endpoint, monkeypatch):
    cleaner = _cleaner()
    cleaner.client = OpenAI(api_key="k", base_url=trickling_endpoint,
                            timeout=0.5, max_retries=1)
    cleaner._timeout_s = 0.5
    monkeypatch.setattr(transcript_cleanup, "_RETRY_BACKOFF_ALLOWANCE_S", 0.2)

    started = time.monotonic()
    assert cleaner.cleanup("um raw text") == "um raw text"
    elapsed = time.monotonic() - started

    # Two 0.5 s attempts plus backoff; every read arrives well inside 0.5 s,
    # so without the deadline this waits for the server's final body.
    assert 1.0 <= elapsed < 2.5
    assert cleaner.last_error.startswith("timed out after")


def test_cancel_ends_the_wait_at_once(monkeypatch):
    cancel = threading.Event()
    cleaner = _cleaner(cancel_event=cancel)
    cleaner.client = MagicMock()
    started, release = _stalled_generate(monkeypatch)
    result = {}

    worker = threading.Thread(target=lambda: result.update(text=cleaner.cleanup("raw text")))
    worker.start()
    try:
        assert started.wait(2)
        pressed = time.monotonic()
        cancel.set()
        worker.join(1)
        assert not worker.is_alive()
        assert time.monotonic() - pressed < 0.5
    finally:
        release.set()
    assert result["text"] == "raw text"
    assert cleaner.last_error == CANCELED_REASON


def test_a_cancel_set_before_the_call_sends_nothing(monkeypatch):
    cancel = threading.Event()
    cancel.set()
    cleaner = _cleaner(cancel_event=cancel)
    cleaner.client = MagicMock()
    sent = []
    monkeypatch.setattr(transcript_cleanup, "generate", lambda *a, **k: sent.append(a))

    assert cleaner.cleanup("raw text") == "raw text"
    assert sent == []
    assert cleaner.last_error == CANCELED_REASON


def test_response_inside_the_budget_is_used(monkeypatch):
    cleaner = _cleaner(cancel_event=threading.Event())
    cleaner.client = MagicMock()
    monkeypatch.setattr(transcript_cleanup, "generate",
                        lambda *a, **k: SimpleNamespace(text=" Cleaned. "))

    assert cleaner.cleanup("um cleaned") == "Cleaned."
    assert cleaner.last_error is None


def test_request_errors_still_reach_last_error(monkeypatch):
    cleaner = _cleaner()
    cleaner.client = MagicMock()

    def fail(*_args, **_kwargs):
        raise RuntimeError("provider said no")

    monkeypatch.setattr(transcript_cleanup, "generate", fail)
    assert cleaner.cleanup("raw text") == "raw text"
    assert cleaner.last_error == "provider said no"


def test_cancel_during_a_stalled_cleanup_frees_the_dictation(tmp_path, monkeypatch):
    """The reported hang: Cancel in "Cleaning up..." must end the job now."""
    from services.runtime import transcription

    wav = tmp_path / "recorded.wav"
    wav.write_bytes(b"RIFF" + bytes(200))
    controller = Mock()
    controller._pending_streaming_text = ""
    controller._pending_file_size = None
    controller._pending_audio_path = None
    controller.recorder.is_recording = False
    controller.current_backend = SimpleNamespace(
        is_available=lambda: True, requires_file_splitting=False,
        is_transcribing=False, transcribe=lambda _path: "um private text")
    monkeypatch.setattr(transcription.settings_manager, "load_all_settings",
                        lambda: {"transcript_cleanup_enabled": True})
    runtime = transcription.TranscriptionRuntime(controller)
    cleaner = runtime._transcript_cleanup
    monkeypatch.setattr(cleaner, "configure", lambda *args: None)
    cleaner.client, cleaner.api_key, cleaner.model = MagicMock(), "k", "m"
    started, release = _stalled_generate(monkeypatch)
    assert runtime._claim_job()

    worker = threading.Thread(target=runtime.transcribe_audio_file, args=(str(wav),))
    worker.start()
    try:
        assert started.wait(2)
        runtime.cancel()
        worker.join(1)
        assert not worker.is_alive()
    finally:
        release.set()

    controller.transcription_completed.emit.assert_not_called()
    controller.transcription_failed.emit.assert_called_once_with("Transcription canceled")
