"""Public file transcription contracts, with a lazy fake decoder."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config import config
from transcriber.local_backend import LocalWhisperBackend


@pytest.fixture
def backend():
    backend = LocalWhisperBackend.__new__(LocalWhisperBackend)
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


@pytest.mark.parametrize("large,requires_split", [(False,False),(False,True),(True,False),(True,True)])
def test_worker_dispatch_uses_backend_splitting_policy(monkeypatch, large, requires_split):
    from services.runtime import transcription
    backend = SimpleNamespace(is_available=lambda:True, requires_file_splitting=requires_split)
    controller = SimpleNamespace(current_backend=backend, large_file_detected=Mock(), status_update=Mock())
    runtime = transcription.TranscriptionRuntime(controller)
    monkeypatch.setattr(transcription.audio_processor, "check_file_size", lambda _: (large,30))
    runtime.transcribe_audio_file = Mock()
    runtime.transcribe_large_audio_file = Mock()
    runtime._run_transcription_job("saved.wav")
    selected = runtime.transcribe_large_audio_file if large and requires_split else runtime.transcribe_audio_file
    selected.assert_called_once_with("saved.wav")
    other = runtime.transcribe_audio_file if large and requires_split else runtime.transcribe_large_audio_file
    other.assert_not_called()
    if large:
        controller.large_file_detected.emit.assert_called_once_with(30, requires_split)
