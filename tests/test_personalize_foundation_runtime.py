"""TranscriptionRuntime's per-job state, command branch, rewrites and delivery."""

from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services import dictation_pipeline, focus_context, recognition_context, snippets
from services import dictionary
from services.dictation_pipeline import DictationJob, JobMode
from services.focus_context import AppIdentity, ContextCaptureService, FocusSnapshot
from services.recognition_context import RecognitionContext
from services.runtime import transcription
from services.runtime.transcription import TranscriptionRuntime
from services.settings import SettingsKey
from services.snippets import Snippet, SnippetPlan
from services.transcript_cleanup import CleanupInfo
from tests.fakes.settings import InMemorySettings
from ui_qt.overlay_state import OverlayState

OUTLOOK = AppIdentity("outlook.exe", "Outlook", pid=10, window="0x1")


class Signal:
    """A pyqtSignal stand-in that delivers at once, like a direct connection."""

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler

    def emit(self, *args):
        self.calls.append(args)
        if self.handler is not None:
            self.handler(*args)


class FakeUI:
    def __init__(self):
        self.statuses = []
        self.stages = []
        self.copied = []
        self.scratchpad = []
        self.scratchpad_focused = False
        self.prefetches = 0
        self.main_window = SimpleNamespace(clear_partial_transcription=lambda: None)

    def set_transcript(self, text, raw=None):
        pass

    def set_transcription_stats(self, *args, **kwargs):
        pass

    def clear_transcription_stats(self):
        pass

    def set_status(self, text):
        self.statuses.append(text)

    def refresh_history(self):
        pass

    def prefetch_clipboard_snapshot(self):
        self.prefetches += 1

    def discard_clipboard_prefetch(self):
        pass

    def copy_to_clipboard(self, text):
        self.copied.append(text)
        return True

    def stage_transcript_for_paste(self, *args, **kwargs):
        self.stages.append((args, kwargs))
        return SimpleNamespace(written=True, lease=None, restore_unavailable=False)

    def schedule_clipboard_restore(self, stage):
        return True

    def commit_transcript_clipboard(self, stage, text):
        return True

    def insert_into_scratchpad(self, text):
        if not self.scratchpad_focused:
            return False
        self.scratchpad.append(text)
        return True


class FakeRecorder:
    def __init__(self):
        self.is_recording = False
        self.capture_canceled = False
        self.start_ok = True
        self.stop_ok = True
        self.last_start_error = "No microphone"

    def start_recording(self):
        self.is_recording = self.start_ok
        self.capture_canceled = False
        return self.start_ok

    def stop_recording(self):
        self.is_recording = False
        return self.stop_ok

    def cancel_recording(self):
        self.capture_canceled = True
        self.is_recording = False


class FakeHistory:
    def __init__(self):
        self.entries = []

    def add_entry(self, **fields):
        self.entries.append(fields)
        return SimpleNamespace(id=f"entry-{len(self.entries)}", audio_file=None)


class FakeService(ContextCaptureService):
    def __init__(self, snapshot, current=None):
        self.snapshot = snapshot
        self.current = current

    def request(self, *, include_text, include_selection=False):
        future: Future = Future()
        future.set_result(self.snapshot)
        return future

    def current_identity(self):
        return self.current

    def reread(self, identity, callback):
        callback(None)

    def shutdown(self):
        pass


@pytest.fixture
def h(monkeypatch, tmp_path):
    ui = FakeUI()
    history = FakeHistory()
    settings = InMemorySettings({SettingsKey.AUTO_PASTE: True, SettingsKey.COPY_CLIPBOARD: True})
    paste = Mock()
    monkeypatch.setattr(transcription, "history_manager", history)
    monkeypatch.setattr(transcription, "settings_manager", settings)
    monkeypatch.setattr(transcription, "send_paste", paste)
    monkeypatch.setattr(transcription, "is_accessibility_trusted", lambda: True)
    submitted = []
    controller = SimpleNamespace(
        ui_controller=ui,
        recorder=FakeRecorder(),
        streaming_runtime=Mock(),
        is_meeting_active=lambda: False,
        overlay_state_update=Signal(),
        status_update=Signal(),
        recording_state_changed=Signal(),
        streaming_overlay_hide=Signal(),
        transcription_completed=Signal(),
        transcription_failed=Signal(),
        executor=SimpleNamespace(submit=lambda fn, *args: submitted.append((fn, args))),
        persistence_executor=SimpleNamespace(submit=lambda fn, *args: fn(*args)),
        current_backend=SimpleNamespace(
            transcribe=lambda path: "hello there", is_available=lambda: True),
        command_runtime=Mock(),
        _streaming_enabled=False,
        _pending_audio_path=None,
        _pending_audio_duration=None,
        _pending_file_size=None,
        _pending_source_name=None,
        _pending_streaming_text="",
        _transcription_elapsed=None,
        _transcription_start_time=None,
        _remote_timing=None,
    )
    runtime = TranscriptionRuntime(controller)
    controller.history_persisted = Signal(runtime.on_history_persisted)
    controller.transcription_completed.handler = runtime.on_transcription_complete
    controller.transcription_failed.handler = runtime.on_transcription_error
    runtime._incremental = Mock()
    runtime._incremental.transcribe.side_effect = lambda backend, path: backend.transcribe(path)
    monkeypatch.setattr(runtime, "_model_info_for_history", lambda: "parakeet")
    audio = tmp_path / "upload.wav"
    audio.write_bytes(b"RIFF" + bytes(200))
    return SimpleNamespace(
        ui=ui, history=history, settings=settings, paste=paste, submitted=submitted,
        controller=controller, runtime=runtime, audio=str(audio),
    )


def _run_submitted(h):
    while h.submitted:
        fn, args = h.submitted.pop(0)
        fn(*args)


# --- job lifetime ------------------------------------------------------------


def test_recording_job_starts_before_the_preview_and_is_claimed_by_the_stop(h, monkeypatch):
    order = []
    h.controller.streaming_runtime.start_streaming_session.side_effect = (
        lambda: order.append(("preview", h.runtime._job is not None)))
    h.runtime._incremental.start.side_effect = (
        lambda controller, recognition=None: order.append(("incremental", recognition)))
    stopped = []
    monkeypatch.setattr(transcription.audio_player, "stop_playback", lambda: stopped.append(True))
    monkeypatch.setattr(dictation_pipeline, "recognition_for",
                        lambda job, settings: RecognitionContext(language="de"))

    assert h.runtime.start_recording()
    job = h.runtime._job

    assert stopped == [True]
    assert job.mode == JobMode.DICTATION
    assert order == [("preview", True), ("incremental", RecognitionContext(language="de"))]
    assert h.controller.overlay_state_update.calls[-1] == (OverlayState.RECORDING,)
    assert h.ui.prefetches == 1
    assert h.runtime._active_job is None

    h.runtime.stop_recording()
    assert h.runtime._active_job is job

    h.runtime.on_transcription_complete("Hello.")
    assert h.runtime._job is None and h.runtime._active_job is None
    assert not h.runtime.has_active_job


def test_command_recording_listens_and_takes_no_clipboard_prefetch(h):
    assert h.runtime.start_recording(mode=JobMode.COMMAND, selection="the old words")

    assert h.runtime._job.mode == JobMode.COMMAND
    assert h.runtime._job.selection_text() == "the old words"
    assert h.controller.overlay_state_update.calls[-1] == (OverlayState.COMMAND_LISTENING,)
    assert h.ui.prefetches == 0


def test_cancel_and_failed_start_leave_no_job(h):
    assert h.runtime.start_recording()
    h.runtime.cancel()
    assert h.runtime._job is None

    h.controller.recorder.start_ok = False
    assert not h.runtime.start_recording()
    assert h.runtime._job is None


def test_a_failed_stop_drops_the_job_and_an_upload_gets_none(h):
    h.runtime.start_recording()
    h.runtime._recording_profile = SimpleNamespace(name="Notes")
    h.controller.recorder.stop_ok = False

    h.runtime.stop_recording()
    assert h.runtime._job is None and h.runtime._recording_profile is None

    h.runtime.upload_audio_file(h.audio)
    assert h.runtime.has_active_job
    assert h.runtime._active_job is None and h.runtime._recording_profile is None


def test_an_upload_clears_a_job_left_by_a_recording(h):
    h.runtime._job = DictationJob()
    h.runtime._profile_settings = {"stale": True}

    h.runtime.upload_audio_file(h.audio)

    assert h.runtime._job is None and h.runtime._profile_settings is None
    assert h.runtime._active_job is None


# --- final pass --------------------------------------------------------------


def test_final_pass_gets_recognition_only_for_a_job(h, monkeypatch):
    seen = []
    monkeypatch.setattr(recognition_context, "transcribe",
                        lambda incremental, backend, path, recognition: seen.append(recognition) or "hi")
    monkeypatch.setattr(dictation_pipeline, "recognition_for",
                        lambda job, settings: RecognitionContext(phrases=("Acme",)))

    assert h.runtime._claim_job(DictationJob())
    h.runtime.transcribe_audio_file(h.audio)
    h.runtime._finish_job()
    assert h.runtime._claim_job()
    h.runtime.transcribe_audio_file(h.audio)

    assert seen == [RecognitionContext(phrases=("Acme",)), None]


def test_command_recordings_go_to_the_command_runtime(h, monkeypatch):
    info = CleanupInfo("openai", "gpt", 0.2, level="medium")
    h.controller.command_runtime.complete_recording.return_value = ("Rewritten.", "old words", info)
    cleanup = Mock()
    monkeypatch.setattr(h.runtime, "_maybe_cleanup_transcript", cleanup)
    job = DictationJob(mode=JobMode.COMMAND)
    h.controller.transcription_completed.handler = None

    assert h.runtime._claim_job(job)
    h.runtime.transcribe_audio_file(h.audio)

    h.controller.command_runtime.complete_recording.assert_called_once_with("hello there", job)
    cleanup.assert_not_called()
    assert h.controller.transcription_completed.calls == [("Rewritten.", "old words", info)]


def test_a_failing_command_reports_its_message(h):
    h.controller.command_runtime.complete_recording.side_effect = RuntimeError(
        "Select the text to change first")

    assert h.runtime._claim_job(DictationJob(mode=JobMode.COMMAND))
    h.runtime.transcribe_audio_file(h.audio)

    assert h.ui.statuses[-1] == "Error: Select the text to change first"
    assert h.paste.call_count == 0
    assert not h.runtime.has_active_job


# --- cleanup stage -----------------------------------------------------------


def test_dictionary_applies_with_cleanup_off(h, monkeypatch):
    monkeypatch.setattr(dictionary, "apply_replacements", lambda text, terms: text.replace("acme", "Acme"))

    assert h.runtime._maybe_cleanup_transcript("acme rocks") == ("Acme rocks", None, None)


def _cleanup_on(h, monkeypatch, result):
    h.settings.values[SettingsKey.TRANSCRIPT_CLEANUP_ENABLED] = True
    cleaner = h.runtime._transcript_cleanup
    calls = []

    def cleanup(text, system_prompt=None):
        calls.append((text, system_prompt))
        return result(text)

    monkeypatch.setattr(cleaner, "configure", lambda *args: None)
    monkeypatch.setattr(cleaner, "is_available", lambda: True)
    monkeypatch.setattr(cleaner, "cleanup", cleanup)
    cleaner.provider, cleaner.model = "openai", "gpt"
    return calls


def test_cleanup_records_its_level(h, monkeypatch):
    calls = _cleanup_on(h, monkeypatch, lambda text: "Hello there.")

    text, raw_text, info = h.runtime._maybe_cleanup_transcript("hello there")

    assert (text, raw_text) == ("Hello there.", "hello there")
    assert info.level == "medium"
    assert [call[0] for call in calls] == ["hello there"]


def test_a_whole_snippet_skips_cleanup(h, monkeypatch):
    calls = _cleanup_on(h, monkeypatch, lambda text: text.upper())
    snippet = Snippet("s", "my address", "1 Main St")
    monkeypatch.setattr(snippets, "load_snippets", lambda settings: [snippet])
    monkeypatch.setattr(snippets, "plan_expansion", lambda text, items: SnippetPlan(text, whole=snippet))
    monkeypatch.setattr(snippets, "expand", lambda text, plan: (plan.whole.text, "", True))
    assert h.runtime._claim_job(DictationJob())

    assert h.runtime._maybe_cleanup_transcript("my address") == ("1 Main St", None, None)
    assert calls == []


def test_a_lost_placeholder_reports_a_failed_cleanup(h, monkeypatch):
    _cleanup_on(h, monkeypatch, lambda text: "Thanks.")
    snippet = Snippet("s", "sig", "Best, Dana", formatted=True)
    monkeypatch.setattr(snippets, "load_snippets", lambda settings: [snippet])
    monkeypatch.setattr(snippets, "plan_expansion", lambda text, items: SnippetPlan(
        "thanks [[S1]]", placeholders=(("[[S1]]", snippet),)))
    monkeypatch.setattr(snippets, "expand", lambda text, plan: (
        (text.replace("[[S1]]", "Best, Dana"), "<b>Dana</b>", True)
        if "[[S1]]" in text else (text, "", False)))
    assert h.runtime._claim_job(DictationJob())

    result = h.runtime._maybe_cleanup_transcript("thanks sig")

    assert result == ("thanks Best, Dana", None, None)
    assert h.runtime._last_cleanup_failure == "snippet placeholder lost"
    assert h.runtime._delivery_html == "<b>Dana</b>"


# --- delivery ----------------------------------------------------------------


def test_dictation_paste_call_is_unchanged_without_html(h):
    assert h.runtime._claim_job(DictationJob())
    h.runtime.on_transcription_complete("Hello.")

    assert h.ui.stages == [(("Hello.",), {})]
    assert h.paste.call_count == 1


def test_html_reaches_the_clipboard_stage(h):
    assert h.runtime._claim_job(DictationJob())
    h.runtime._delivery_html = "<p>Hello.</p>"

    h.runtime.on_transcription_complete("Hello.")

    assert h.ui.stages == [(("Hello.",), {"html": "<p>Hello.</p>"})]


def _started_in_outlook():
    """A target the paste check can confirm: Outlook then, Outlook now."""
    focus_context.set_service(FakeService(FocusSnapshot(OUTLOOK), current=OUTLOOK))
    target: Future = Future()
    target.set_result(FocusSnapshot(OUTLOOK))
    return target


def test_rewrites_paste_even_with_auto_paste_off(h):
    h.settings.values[SettingsKey.AUTO_PASTE] = False

    assert h.runtime._claim_job(DictationJob(mode=JobMode.TRANSFORM, target=_started_in_outlook()))
    h.runtime.on_transcription_complete("Shorter.", "A much longer text.")
    assert h.paste.call_count == 1

    assert h.runtime._claim_job(DictationJob())
    h.runtime.on_transcription_complete("Dictated.")
    assert h.paste.call_count == 1
    assert h.ui.copied[-1] == "Dictated."


def test_a_rewrite_is_only_copied_when_the_app_changed(h):
    focus_context.set_service(FakeService(FocusSnapshot(OUTLOOK),
                                          current=AppIdentity("slack.exe", "Slack", pid=11)))
    job = dictation_pipeline.begin_job(JobMode.COMMAND, {})

    assert h.runtime._claim_job(job)
    h.runtime.on_transcription_complete("Rewritten.")

    assert h.paste.call_count == 0
    assert h.ui.copied == ["Rewritten."]
    assert h.ui.statuses[-1] == "Rewrite copied — the app changed; paste it where you want"
    assert h.history.entries[0]["entry_kind"] == "command"


def test_the_focused_scratchpad_takes_the_dictation(h):
    h.ui.scratchpad_focused = True
    h.controller._pending_audio_path = "recording.wav"

    assert h.runtime._claim_job(DictationJob())
    h.runtime.on_transcription_complete("Note to self.")

    assert h.ui.scratchpad == ["Note to self."]
    assert h.ui.stages == [] and h.paste.call_count == 0
    assert h.ui.statuses[-1] == "Ready (Added to Scratchpad)"
    assert h.history.entries[0]["text"] == "Note to self."


def test_after_paste_follows_a_successful_paste(h, monkeypatch):
    pasted = []
    monkeypatch.setattr(
        dictation_pipeline, "after_paste",
        lambda job, text, before_cleanup: pasted.append((text, before_cleanup)),
    )

    assert h.runtime._claim_job(DictationJob())
    h.runtime.on_transcription_complete("Hello.")
    assert h.runtime._claim_job(DictationJob())
    h.runtime.on_transcription_complete("Hello, Sam.", "hello sam")
    h.settings.values[SettingsKey.AUTO_PASTE] = False
    assert h.runtime._claim_job(DictationJob())
    h.runtime.on_transcription_complete("Not pasted.")

    assert pasted == [("Hello.", None), ("Hello, Sam.", "hello sam")]


# --- history -----------------------------------------------------------------


def test_history_entries_carry_the_job_context(h):
    focus_context.set_service(FakeService(FocusSnapshot(OUTLOOK)))
    job = dictation_pipeline.begin_job(JobMode.DICTATION, {})
    h.controller._pending_audio_path = "recording.wav"

    assert h.runtime._claim_job(job)
    h.runtime.on_transcription_complete(
        "Hello.", "hello", CleanupInfo("openai", "gpt", 0.3, level="high"))
    h.runtime.upload_audio_file(h.audio)
    h.runtime.on_transcription_complete("From a file.")
    assert h.runtime._claim_job()
    h.controller._pending_audio_path = None
    h.runtime.on_transcription_complete("Re-transcribed.")

    live, upload, retranscribed = h.history.entries
    assert live["entry_kind"] == "dictation"
    assert (live["app_id"], live["app_name"], live["cleanup_level"]) == (
        "outlook.exe", "Outlook", "high")
    assert upload["entry_kind"] == "file" and "app_id" not in upload
    assert retranscribed["entry_kind"] == "file"


def test_batch_entries_are_file_entries(h):
    from services.batch_upload import BatchItem, BatchItemResult, BatchResult, BatchUploadRequest

    items = (BatchItem(h.audio), BatchItem(h.audio))
    result = BatchResult(
        request=BatchUploadRequest(items),
        items=tuple(BatchItemResult(item, text=f"Part {n}.") for n, item in enumerate(items)),
    )
    assert h.runtime._claim_job()
    h.runtime._deliver_to_clipboard = False

    h.runtime.on_batch_complete(result)

    assert [entry["entry_kind"] for entry in h.history.entries] == ["file", "file"]


def test_stats_failures_never_touch_the_saved_entry(h, monkeypatch):
    recorded = []

    def record_stats(fields, row):
        recorded.append((fields["text"], row.id))
        raise RuntimeError("stats table locked")

    monkeypatch.setattr(dictation_pipeline, "record_stats", record_stats)

    assert h.runtime._claim_job(DictationJob())
    h.runtime.on_transcription_complete("Hello.")

    assert recorded == [("Hello.", "entry-1")]
    assert h.controller.history_persisted.calls == [({"error": "", "refresh": True},)]
    assert h.ui.statuses[-1] == "Ready (Pasted)"
    assert not h.runtime.has_active_job


# --- rewrite job API ---------------------------------------------------------


def test_rewrite_jobs_are_refused_while_recording_or_busy(h):
    job = DictationJob(mode=JobMode.TRANSFORM)
    h.controller.recorder.is_recording = True
    assert not h.runtime.begin_rewrite_job(job, source_name="Polish")

    h.controller.recorder.is_recording = False
    assert h.runtime._claim_job()
    assert not h.runtime.begin_rewrite_job(job, source_name="Polish")
    assert "wait before rewriting text" in h.controller.status_update.calls[-1][0]


def test_a_submitted_rewrite_is_pasted_and_saved(h):
    job = DictationJob(mode=JobMode.TRANSFORM, selection=None, target=_started_in_outlook())

    assert h.runtime.begin_rewrite_job(job, source_name="Polish")
    assert h.runtime.has_active_job and h.runtime._active_job is job
    assert h.controller.overlay_state_update.calls[-1] == (OverlayState.REWRITING,)

    h.runtime.submit_rewrite(lambda: ("Polished.", "rough text", None))
    _run_submitted(h)

    assert h.paste.call_count == 1
    entry = h.history.entries[0]
    assert (entry["text"], entry["raw_text"], entry["source_name"], entry["entry_kind"]) == (
        "Polished.", "rough text", "Polish", "transform")
    assert not h.runtime.has_active_job


def test_a_failed_rewrite_frees_the_slot(h):
    assert h.runtime.begin_rewrite_job(DictationJob(mode=JobMode.TRANSFORM), source_name="Polish")

    def work():
        raise RuntimeError("No AI provider is set up")

    h.runtime.submit_rewrite(work)
    _run_submitted(h)

    assert h.ui.statuses[-1] == "Error: No AI provider is set up"
    assert not h.runtime.has_active_job and h.history.entries == []


def test_an_abandoned_rewrite_frees_the_slot_once(h):
    assert h.runtime.begin_rewrite_job(DictationJob(mode=JobMode.TRANSFORM), source_name="Polish")

    h.runtime.abandon_rewrite_job("Select the text to change first")

    assert not h.runtime.has_active_job
    assert h.controller.status_update.calls[-1] == ("Select the text to change first",)
    assert h.controller.overlay_state_update.calls[-1] == (OverlayState.NONE,)

    # Not a rewrite's slot any more: a later abandon leaves other jobs alone.
    assert h.runtime._claim_job()
    h.runtime.abandon_rewrite_job("late")
    assert h.runtime.has_active_job


def test_paste_text_now_pastes_without_history_and_refuses_while_busy(h):
    h.settings.values[SettingsKey.AUTO_PASTE] = False

    assert h.runtime.paste_text_now("original words")
    assert h.ui.stages == [(("original words",), {})]
    assert h.history.entries == []

    assert h.runtime._claim_job()
    assert not h.runtime.paste_text_now("again")
    assert h.paste.call_count == 1
