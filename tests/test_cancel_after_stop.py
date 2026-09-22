"""A cancel after the stop press must never paste.

Several windows used to leak a result:
- cancel during post-roll, where the recorder is still capturing. The
  already-submitted ``finish_recording_job`` then saved and transcribed the
  post-roll tail.
- cancel, then the record key or a push-and-hold release while the canceled
  stream closes. That stop claimed a fresh job for the refilled capture.
- cancel while no engine is decoding (the WAV save, a queued decode, or the
  cleanup call). ``_cancel`` only sets the flag there, and the job went on to
  paste.
"""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import numpy as np

from config import config


def _runtime(tmp_path, monkeypatch, final_text="Hello there."):
    from services.runtime import transcription

    wav = tmp_path / "recorded.wav"
    wav.write_bytes(b"RIFF" + bytes(200))
    monkeypatch.setattr(transcription.config, "RECORDED_AUDIO_FILE", str(wav))
    controller = Mock()
    controller._pending_streaming_text = ""
    controller._pending_file_size = None
    controller._pending_audio_path = None
    controller._transcription_start_time = None
    controller.current_backend = SimpleNamespace(
        is_available=lambda: True, requires_file_splitting=False,
        transcribe=lambda _path: final_text)
    controller.recorder.is_recording = True
    controller.recorder.wait_for_stop_completion.return_value = True
    controller.recorder.has_recording_data.return_value = True
    controller.recorder.save_recording.return_value = True
    controller.recorder.get_recording_duration.return_value = 4.0
    runtime = transcription.TranscriptionRuntime(controller)
    monkeypatch.setattr(runtime, "_maybe_cleanup_transcript", lambda raw: (raw, None, None))
    return controller, runtime, str(wav)


def test_cancel_during_post_roll_transcribes_nothing(tmp_path, monkeypatch):
    controller, runtime, _wav = _runtime(tmp_path, monkeypatch)
    assert runtime._claim_job()  # what stop_recording does before post-roll

    runtime._cancel_recording()
    runtime.finish_recording_job()

    controller.recorder.save_recording.assert_not_called()
    controller.transcription_completed.emit.assert_not_called()
    controller.transcription_failed.emit.assert_not_called()
    assert not runtime.has_active_job


def test_cancel_before_any_stop_leaves_the_next_job_alone(tmp_path, monkeypatch):
    controller, runtime, _wav = _runtime(tmp_path, monkeypatch)

    runtime._cancel_recording()  # an ordinary cancel mid-recording
    assert not runtime._cancel_requested.is_set()

    assert runtime._claim_job()
    runtime.finish_recording_job()

    (transcript, _raw, _info), _ = controller.transcription_completed.emit.call_args
    assert transcript == "Hello there."


def test_cancel_during_cleanup_fails_the_job_instead_of_pasting(tmp_path, monkeypatch):
    controller, runtime, wav = _runtime(tmp_path, monkeypatch)
    assert runtime._claim_job()

    def cleanup_then_cancel(raw):
        runtime._cancel()  # user presses cancel while the cleanup call runs
        return raw, None, None

    controller.recorder.is_recording = False
    controller.current_backend.is_transcribing = False
    monkeypatch.setattr(runtime, "_maybe_cleanup_transcript", cleanup_then_cancel)

    runtime.transcribe_audio_file(wav)

    controller.transcription_completed.emit.assert_not_called()
    controller.transcription_failed.emit.assert_called_once_with("Transcription canceled")


def test_uncanceled_dictation_still_completes(tmp_path, monkeypatch):
    controller, runtime, _wav = _runtime(tmp_path, monkeypatch)
    assert runtime._claim_job()

    runtime.finish_recording_job()

    controller.recorder.save_recording.assert_called_once()
    (transcript, _raw, _info), _ = controller.transcription_completed.emit.call_args
    assert transcript == "Hello there."


def test_cancel_before_transcription_starts_skips_the_engine(tmp_path, monkeypatch):
    controller, runtime, _wav = _runtime(tmp_path, monkeypatch)
    decodes = []
    controller.current_backend.transcribe = lambda path: decodes.append(path) or "text"
    assert runtime._claim_job()

    def save_then_cancel():
        runtime._cancel_requested.set()  # cancel pressed while the WAV saves
        return True

    controller.recorder.save_recording.side_effect = save_then_cancel
    runtime.finish_recording_job()

    assert decodes == []
    controller.transcription_completed.emit.assert_not_called()
    controller.transcription_failed.emit.assert_not_called()
    assert not runtime.has_active_job


def test_cancel_during_decode_never_reaches_cleanup(tmp_path, monkeypatch):
    controller, runtime, wav = _runtime(tmp_path, monkeypatch)
    cleaned = []
    monkeypatch.setattr(runtime, "_maybe_cleanup_transcript",
                        lambda raw: cleaned.append(raw) or (raw, None, None))

    def decode_then_cancel(_path):
        runtime._cancel_requested.set()  # the engine had not yet started
        return "private text"

    controller.current_backend.transcribe = decode_then_cancel
    assert runtime._claim_job()
    runtime.transcribe_audio_file(wav)

    assert cleaned == []
    controller.transcription_failed.emit.assert_called_once_with("Transcription canceled")


def test_cancel_between_emit_and_slot_does_not_paste(tmp_path, monkeypatch):
    controller, runtime, _wav = _runtime(tmp_path, monkeypatch)
    paste = Mock()
    monkeypatch.setattr(runtime, "_apply_clipboard_and_paste", paste)
    assert runtime._claim_job()
    runtime._cancel_requested.set()

    runtime.on_transcription_complete("Hello there.")

    paste.assert_not_called()
    assert not runtime.has_active_job


def test_abandoned_job_drops_its_early_decode_session(tmp_path, monkeypatch):
    controller, runtime, _wav = _runtime(tmp_path, monkeypatch)
    discard = Mock()
    monkeypatch.setattr(runtime._incremental, "discard", discard)
    assert runtime._claim_job()
    runtime._cancel_requested.set()

    runtime.finish_recording_job()

    discard.assert_called_once()
    controller.ui_controller.discard_clipboard_prefetch.assert_called()


def test_stop_after_cancel_claims_no_job(tmp_path, monkeypatch):
    controller, runtime, _wav = _runtime(tmp_path, monkeypatch)
    controller.recorder.capture_canceled = True  # canceled, stream closing

    runtime._stop_recording()

    controller.recorder.stop_recording.assert_not_called()
    controller.executor.submit.assert_not_called()
    assert not runtime.has_active_job


def _block(value=2000):
    return np.full((config.CHUNK_SIZE, 1), value, dtype=np.int16)


def test_recorder_cancel_ends_post_roll_and_keeps_nothing_after(tmp_path):
    from services import recorder as recorder_module
    from services.recorder import AudioRecorder

    with patch.object(recorder_module.sd, "InputStream", MagicMock()):
        rec = AudioRecorder(output_file=str(tmp_path / "canceled.wav"))
        try:
            assert rec.start_recording()
            for _ in range(20):
                rec._audio_callback(_block(), config.CHUNK_SIZE, None, None)
            started = time.monotonic()

            rec.cancel_recording()
            # Blocks still arriving while the stream closes are dropped.
            rec._audio_callback(_block(), config.CHUNK_SIZE, None, None)

            assert rec.capture_canceled
            assert not rec.has_recording_data()
            assert rec.wait_for_stop_completion(timeout=1.0)
            assert time.monotonic() - started < 0.5  # not the 1.2 s cap
            assert not rec.is_recording

            assert rec.start_recording()  # the next recording keeps audio again
            assert not rec.capture_canceled
            rec._audio_callback(_block(), config.CHUNK_SIZE, None, None)
            assert rec.has_recording_data()
        finally:
            rec.cleanup()


def test_native_preview_recordings_take_the_file_path(monkeypatch):
    from services.incremental_dictation import DictationSession
    from services.streaming_transcriber import NativeStreamingTranscriber
    from transcriber.optional_backend import LocalSpeechBackend

    backend = LocalSpeechBackend("nemotron")
    monkeypatch.setattr(backend, "is_available", lambda: True)
    recorder = SimpleNamespace(channels=1, dtype=np.int16)
    native = NativeStreamingTranscriber(backend=backend)
    controller = SimpleNamespace(current_backend=backend, recorder=recorder,
                                 streaming_transcriber=native)

    assert DictationSession.create(controller) is None
    controller.streaming_transcriber = None
    assert DictationSession.create(controller) is not None


def test_preview_that_cannot_start_shows_no_empty_overlay():
    from services.runtime.streaming import StreamingRuntime

    controller = Mock()
    controller._streaming_enabled = True
    controller.streaming_transcriber.is_streaming = False  # refused to start
    runtime = StreamingRuntime(controller)

    runtime.start_streaming_session()

    controller.streaming_overlay_show.emit.assert_not_called()
    controller.recorder.set_streaming_callback.assert_called_with(None)
