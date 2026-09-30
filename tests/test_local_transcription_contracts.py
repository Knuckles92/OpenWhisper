"""Public file transcription contracts, with a lazy fake decoder."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config import config
from transcriber.local_backend import LocalWhisperBackend


@pytest.fixture
def backend():
    backend = LocalWhisperBackend(model_name="base", load=False)
    backend.model = Mock()
    backend.should_cancel = False
    backend.is_transcribing = False
    return backend


@pytest.mark.parametrize("vad", [False, True])
def test_public_transcribe_consumes_lazy_segments_and_forwards_options(backend, monkeypatch, vad):
    monkeypatch.setattr(config, "FASTER_WHISPER_VAD_ENABLED", vad)
    consumed = []
    def segments():
        assert backend.is_transcribing
        consumed.append(True)
        yield SimpleNamespace(text="  hello\n")
        yield SimpleNamespace(text="  world  ")
    backend.model.transcribe.return_value = (segments(), SimpleNamespace(language="en", language_probability=1.0))
    backend.should_cancel = True
    assert backend.transcribe("synthetic.wav") == "hello world"
    assert consumed == [True]
    assert not backend.is_transcribing
    backend.model.transcribe.assert_called_once_with("synthetic.wav",
        beam_size=config.FASTER_WHISPER_BEAM_SIZE, vad_filter=vad,
        vad_parameters={"min_silence_duration_ms":config.FASTER_WHISPER_VAD_MIN_SILENCE_MS} if vad else None)


@pytest.mark.parametrize("failure", ["cancel", "generator"])
def test_lazy_failure_resets_busy_and_next_call_can_succeed(backend, failure):
    def segments():
        yield SimpleNamespace(text="first")
        if failure == "cancel":
            backend.cancel_transcription()
            yield SimpleNamespace(text="discard")
        else:
            raise RuntimeError("generator failed")
    info = SimpleNamespace(language="en", language_probability=1.0)
    backend.model.transcribe.return_value = (segments(), info)
    with pytest.raises(Exception, match="canceled" if failure=="cancel" else "generator failed"):
        backend.transcribe("synthetic.wav")
    assert not backend.is_transcribing
    backend.model.transcribe.return_value = (iter([SimpleNamespace(text="next")]), info)
    assert backend.transcribe("synthetic.wav") == "next"


def test_unavailable_model_is_not_transcribed(backend):
    backend.model = None
    with pytest.raises(Exception, match="not available"):
        backend.transcribe("synthetic.wav")
    assert not backend.is_transcribing


@pytest.mark.parametrize("condition", ["meeting", "missing", "recording", "busy", "ready", "submit_error"])
def test_saved_recording_dispatch_guards_and_releases_claim(tmp_path, monkeypatch, condition):
    from services.runtime.transcription import TranscriptionRuntime
    path = tmp_path/"saved.wav"
    if condition != "missing":
        path.write_bytes(b"synthetic")
    controller = SimpleNamespace(is_meeting_active=lambda: condition=="meeting",
        recorder=SimpleNamespace(is_recording=condition=="recording"),
        status_update=Mock(), overlay_state_update=Mock(), ui_controller=Mock())
    runtime = TranscriptionRuntime(controller)
    runtime._job_active = condition=="busy"
    submit = Mock(side_effect=RuntimeError("submit failed") if condition=="submit_error" else None)
    monkeypatch.setattr(runtime, "_submit_transcription_job", submit)
    runtime.retranscribe_audio(str(path))
    if condition in ("ready", "submit_error"):
        submit.assert_called_once_with(str(path))
        assert runtime._job_active == (condition == "ready")
        if condition == "ready":
            assert controller._pending_source_name == "saved.wav"
            assert controller._pending_file_size == len(b"synthetic")
        else:
            controller.ui_controller.set_status.assert_called_once_with("Error: Failed to process audio: submit failed")
            assert controller._pending_source_name is None
            assert controller._pending_file_size is None
    else:
        submit.assert_not_called()


def _worker_runtime(backend):
    from services.runtime.transcription import TranscriptionRuntime
    controller = Mock(current_backend=backend, _pending_file_size=None, _pending_audio_path=None)
    runtime = TranscriptionRuntime(controller)
    runtime._maybe_cleanup_transcript = lambda raw: (raw, None, None)
    return runtime, controller


def _statuses(controller):
    return [call.args[0] for call in controller.status_update.emit.call_args_list]


def test_local_engine_takes_a_file_over_the_api_limit_whole(backend, tmp_path):
    """OpenAI's upload limit must not split, or announce, a local transcription."""
    from ui_qt.overlay_state import OverlayState
    path = tmp_path / "long-meeting.wav"
    with open(path, "wb") as handle:
        handle.truncate(int((config.MAX_FILE_SIZE_MB + 1) * 1024 * 1024))
    info = SimpleNamespace(language="en", language_probability=1.0)
    backend.model.transcribe.return_value = (iter([SimpleNamespace(text="all of it")]), info)
    runtime, controller = _worker_runtime(backend)

    runtime._run_transcription_job(str(path))

    assert backend.model.transcribe.call_args.args == (str(path),)
    controller.large_file_detected.emit.assert_not_called()
    controller.overlay_state_update.emit.assert_any_call(OverlayState.TRANSCRIBING)
    assert _statuses(controller) == ["Transcribing..."]
    controller.transcription_completed.emit.assert_called_once_with("all of it", None, None)


def test_worker_job_shows_the_split_a_backend_reports(tmp_path):
    """The runtime only shows a split; the backend does it, inside transcribe."""
    from ui_qt.overlay_state import OverlayState
    path = tmp_path / "saved.wav"
    path.write_bytes(b"RIFF" + bytes(64))
    calls = []
    backend = SimpleNamespace(
        is_available=lambda: True,
        large_file_size_mb=lambda _path: 30.0,
        transcribe=lambda audio_path: calls.append(audio_path) or "chunked text",
    )
    runtime, controller = _worker_runtime(backend)

    runtime._run_transcription_job(str(path))

    assert calls == [str(path)]
    controller.large_file_detected.emit.assert_called_once_with(30.0)
    assert _statuses(controller) == ["Splitting large file (30.0 MB)..."]
    assert OverlayState.TRANSCRIBING not in [
        call.args[0] for call in controller.overlay_state_update.emit.call_args_list
    ], "the backend's own progress moves the overlay on once the split is done"
    runtime.report_backend_progress("Transcribing 3 chunks...", True)
    controller.overlay_state_update.emit.assert_called_with(OverlayState.TRANSCRIBING)
    assert _statuses(controller)[-1] == "Transcribing 3 chunks..."
