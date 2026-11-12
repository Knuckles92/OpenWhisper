"""Command Mode refusals are not failed transcriptions.

"Select the text to change first", an instruction nobody heard and the like
show their message and keep no instruction audio: nothing is copied into
Recordings, where retention would push out older dictation audio, and the
recording journal is dropped so the next start does not recover it either.
"""

import os
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


from config import config
from services import text_rewrite
from services.dictation_pipeline import DictationJob, JobMode
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings
from tests.test_personalize_s3_command_delivery import FakeCleaner, command_harness, transcribe_directly


class TimingOutCleaner(FakeCleaner):
    def cleanup(self, text, system_prompt=None, timeout_s=None, deadline_s=None):
        self.last_error = "timed out after 9 s"
        return text


@pytest.fixture
def h(monkeypatch, tmp_path):
    wav = tmp_path / "recorded.wav"
    wav.write_bytes(b"RIFF" + bytes(400))
    monkeypatch.setattr(config, "RECORDED_AUDIO_FILE", str(wav))
    heard = {"text": "make this shorter"}
    recorder = SimpleNamespace(
        is_recording=False, capture_canceled=False, last_capture_error=None,
        wait_for_stop_completion=lambda: True, has_recording_data=lambda: True,
        save_recording=lambda allow_incomplete=True: True,
        get_recording_duration=lambda: 1.5,
        acknowledge_recording=Mock(),
    )
    streaming = Mock()
    streaming.stop_streaming_session.return_value = ""
    streaming.finalize_streaming_text.return_value = ""
    h = command_harness(
        monkeypatch, InMemorySettings({SettingsKey.AUTO_PASTE: True}),
        recorder=recorder, streaming_runtime=streaming,
        current_backend=SimpleNamespace(
            transcribe=lambda path: heard["text"], is_available=lambda: True),
    )
    h.history.preserve_recording = Mock(return_value="recording_kept.wav")
    transcribe_directly(h.runtime)
    monkeypatch.setattr(h.runtime, "_require_backend_ready", lambda: None)
    h.recorder, h.heard = recorder, heard
    yield h
    h.controller.command_runtime.cleanup()


def _command_job(selection):
    future: Future = Future()
    future.set_result(selection)
    return DictationJob(mode=JobMode.COMMAND, selection=future)


def _record_command(h, selection=""):
    """A stopped Command Mode recording, from its saved WAV to delivery."""
    assert h.runtime._claim_job(_command_job(selection))
    h.runtime.finish_recording_job()


def _assert_refused(h, message):
    assert h.ui.statuses[-1] == message
    h.history.preserve_recording.assert_not_called()
    h.recorder.acknowledge_recording.assert_called_once_with()
    h.paste.assert_not_called()
    assert h.history.entries == []
    assert h.controller._pending_audio_path is None
    assert not h.runtime.has_active_job


def test_an_instruction_that_needs_a_selection_keeps_no_audio(h):
    h.runtime._transcript_cleanup = FakeCleaner(text_rewrite.NEEDS_SELECTION)

    _record_command(h)

    _assert_refused(h, "Select the text to change first")


def test_an_instruction_nobody_heard_keeps_no_audio(h):
    h.heard["text"] = ""

    _record_command(h)

    _assert_refused(h, "Didn't catch an instruction")


def test_nothing_selected_with_writing_turned_off_keeps_no_audio(h):
    h.settings.values[SettingsKey.COMMAND_MODE_INSERT_WITHOUT_SELECTION] = False

    _record_command(h)

    _assert_refused(h, "Select text first")


def test_a_failing_ai_model_still_keeps_the_instruction_audio(h):
    h.runtime._transcript_cleanup = TimingOutCleaner("unused")

    _record_command(h, selection="old words")

    h.history.preserve_recording.assert_called_once_with(config.RECORDED_AUDIO_FILE)
    assert h.ui.statuses[-1] == (
        f"Error: {text_rewrite.TIMED_OUT_MESSAGE}"
        " — audio saved in Recordings as recording_kept.wav"
    )
    assert not h.runtime.has_active_job


def test_a_transform_refusal_shows_its_message_plainly(h):
    h.runtime._transcript_cleanup = FakeCleaner("unused")
    job = DictationJob(mode=JobMode.TRANSFORM, selection=None)
    assert h.runtime.begin_rewrite_job(job, source_name="Transform · Polish")
    too_long = "x" * (text_rewrite.MAX_TEXT_CHARS + 1)

    h.runtime.submit_rewrite(
        lambda: h.controller.command_runtime._rewrite(too_long, "Polish it", job=job))
    fn, args = h.submitted.pop()
    fn(*args)

    assert h.ui.statuses[-1].startswith("Select less text to rewrite")
    h.history.preserve_recording.assert_not_called()
    h.paste.assert_not_called()
    assert not h.runtime.has_active_job
