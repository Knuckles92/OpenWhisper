"""HTTP to the Qt thread and back: the listener's app under uvicorn, QtAppControl,
the real TranscriptionRuntime, and its events on the live stream.

The test thread is the Qt thread. HTTP calls run on helper threads while it
handles events, as the app's event loop would.
"""

import json
import socket
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import uvicorn
from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal

from services import focus_context
from services.actions.model import Action, ActionKind
from services.agent_mcp.app import create_app
from services.automation import app_control, events, permissions
from services.automation.control import set_app_control
from services.database import DatabaseManager
from services.dictation_pipeline import JobMode
from services.focus_context import ContextCaptureService, FocusSnapshot
from services.runtime import command, transcription
from services.runtime.command import CommandRuntime
from services.runtime.transcription import TranscriptionRuntime
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings

TOKEN = "live-e2e-test-token-" + "d" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ASK = Action("a1", "Ask Claude", ActionKind.OPEN_LINK, {"template": "claude://claude.ai/new?q={text}"})


class Signal:
    """A plain callback list for the controller signals the runtime emits off-thread."""

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler

    def emit(self, *args):
        self.calls.append(args)
        if self.handler is not None:
            self.handler(*args)


class FakeUI:
    def __init__(self):
        self.statuses, self.notices = [], []
        self.main_window = SimpleNamespace(clear_partial_transcription=lambda: None)

    def set_status(self, text):
        self.statuses.append(text)

    def show_notice(self, text, done=False):
        self.notices.append(text)

    def clear_transcription_stats(self):
        pass

    def set_transcript(self, text, raw=None):
        pass

    def set_transcription_stats(self, *args, **kwargs):
        pass

    def refresh_history(self):
        pass

    def discard_clipboard_prefetch(self):
        pass

    def show_transcription_error(self, message):
        pass


class FakeRecorder:
    def __init__(self):
        self.is_recording = False
        self.capture_canceled = False
        self.last_start_error = None

    def start_recording(self):
        self.is_recording = True
        return True

    def stop_recording(self):
        self.is_recording = False
        return True

    def get_recording_duration(self):
        return 1.5

    def cancel_recording(self):
        self.is_recording = False


class FakeHistory:
    def __init__(self):
        self.entries = []

    def add_entry(self, **fields):
        self.entries.append(fields)
        return SimpleNamespace(id=100 + len(self.entries), audio_file=None)


class NoFocus(ContextCaptureService):
    def request(self, *, include_text, include_selection=False):
        future = Future()
        future.set_result(FocusSnapshot())
        return future

    def current_identity(self):
        return None

    def reread(self, identity, callback):
        callback(None)

    def shutdown(self):
        pass


class Controller(QObject):
    """ApplicationController's surface: real signal for status, thin delegations."""

    status_update = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.ui_controller = FakeUI()
        self.recorder = FakeRecorder()
        self.submitted = []
        self.readiness = ""
        self.meeting = False
        self.streaming_runtime = Mock()
        self._streaming_enabled = False
        self.overlay_state_update = Signal()
        self.recording_state_changed = Signal()
        self.recording_destination_changed = Signal()
        self.transcription_completed = Signal()
        self.transcription_failed = Signal()
        self.executor = SimpleNamespace(submit=lambda fn, *args: self.submitted.append((fn, args)))
        self.persistence_executor = SimpleNamespace(submit=lambda fn, *args: fn(*args))
        self.current_backend = SimpleNamespace(is_available=lambda: True, is_transcribing=False)
        self.action_runtime = Mock()
        self._pending_audio_path = None
        self._pending_audio_duration = None
        self._pending_file_size = None
        self._pending_source_name = None
        self._pending_streaming_text = ""
        self._transcription_elapsed = None
        self._transcription_start_time = None
        self._remote_timing = None

    def transcription_readiness_message(self):
        return self.readiness or None

    def is_meeting_active(self):
        return self.meeting

    def start_action_recording(self, action_id, *, source="hotkey"):
        return self.transcription_runtime.start_recording(
            mode=JobMode.ACTION, action_id=action_id, source=source,
        ) is not False

    def stop_recording(self):
        self.transcription_runtime.stop_recording()

    def cancel(self):
        self.transcription_runtime.cancel()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pump_until(done, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not done() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.002)
    assert done()


def in_worker(fn):
    """``fn()`` on a helper thread while the Qt thread handles events."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    pump_until(lambda: not thread.is_alive())
    if "error" in box:
        raise box["error"]
    return box["value"]


class EventReader:
    """Collects one SSE stream's events on a helper thread."""

    def __init__(self, client):
        self.events = []
        self.ready = threading.Event()
        self._client = client
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self.ready.wait(5)

    def _run(self):
        try:
            with self._client.stream("GET", "/v1/events", headers=AUTH) as response:
                fields = {}
                for line in response.iter_lines():
                    if line.startswith("retry:"):
                        self.ready.set()
                    elif line == "" and "event" in fields:
                        self.events.append((fields["event"], json.loads(fields["data"])))
                        fields = {}
                    elif ":" in line and not line.startswith(":"):
                        name, _, value = line.partition(":")
                        fields[name] = value.lstrip(" ")
        except httpx.HTTPError:
            pass

    def wait_for(self, event_type, timeout=5.0):
        pump_until(lambda: any(t == event_type for t, _ in self.events), timeout)
        return [data for t, data in self.events if t == event_type]


@pytest.fixture
def world(monkeypatch, tmp_path, _session_qt_application):
    bus = events.EventBus()
    previous_bus = events.set_bus(bus)
    settings = InMemorySettings({
        SettingsKey.AUTO_PASTE: False,
        SettingsKey.RESTORE_CLIPBOARD: False,
        SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: False,
        SettingsKey.ACTIONS: [ASK.to_dict()],
        SettingsKey.API_RUN_ACTIONS: True,
        SettingsKey.API_CONTROL_RECORDING: True,
    })
    history = FakeHistory()
    for module in (transcription, command):
        monkeypatch.setattr(module, "settings_manager", settings)
    import services.settings as settings_module

    monkeypatch.setattr(settings_module, "settings_manager", settings)
    monkeypatch.setattr(transcription, "history_manager", history)
    monkeypatch.setattr(transcription, "send_paste", Mock())
    monkeypatch.setattr(transcription, "audio_player", SimpleNamespace(stop_playback=lambda: None))
    focus_context.set_service(NoFocus())

    controller = Controller()
    runtime = TranscriptionRuntime(controller)
    controller.transcription_runtime = runtime
    controller.history_persisted = Signal(runtime.on_history_persisted)
    controller.transcription_completed.handler = runtime.on_transcription_complete
    controller.transcription_failed.handler = runtime.on_transcription_error
    runtime._incremental = Mock()
    monkeypatch.setattr(runtime, "_model_info_for_history", lambda: "parakeet")
    controller.command_runtime = CommandRuntime(controller)
    app_control.install(controller)

    database = tmp_path / "live.db"
    DatabaseManager(str(database)).close()
    app = create_app(database, TOKEN, enabled=lambda: True, settings=settings)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=free_port(), log_config=None, log_level="critical",
        lifespan="on", ws="none", access_log=False, timeout_graceful_shutdown=1,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    pump_until(lambda: server.started)
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{server.config.port}", trust_env=False, timeout=10.0
    )
    try:
        yield SimpleNamespace(
            controller=controller, runtime=runtime, client=client, bus=bus,
            history=history, settings=settings,
        )
    finally:
        client.close()
        server.should_exit = True
        thread.join(10)
        set_app_control(None)
        controller.command_runtime.cleanup()
        events.set_bus(previous_bus)
        bus.close()


def post(world, path, body=None):
    return in_worker(lambda: world.client.post(path, json=body, headers=AUTH))


def test_running_an_action_over_http_goes_through_the_real_runtime(world):
    stream = EventReader(world.client)
    response = post(world, "/v1/actions/a1/run", {"text": "  what time is it  "})
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert world.runtime.has_active_job

    # The rewrite worker the runtime queued, as the executor would run it.
    fn, args = world.controller.submitted.pop(0)
    fn(*args)

    completed = stream.wait_for("transcription.completed")[0]["data"]
    assert (completed["job_id"], completed["mode"], completed["action_id"], completed["source"]) == (
        job_id, "action", "a1", "api",
    )
    assert completed["text"] == "what time is it"
    saved = stream.wait_for("history.saved")[0]["data"]
    assert saved["job_id"] == job_id
    job, text = world.controller.action_runtime.deliver.call_args[0]
    assert (job.id, text) == (job_id, "what time is it")
    assert not world.runtime.has_active_job

    unknown = post(world, "/v1/actions/nope/run", {"text": "hi"})
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "not_found")


def test_recording_over_http_starts_reports_and_stops_the_real_runtime(world):
    stream = EventReader(world.client)
    started = post(world, "/v1/recording/start", {"action_id": "a1"})
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    event = stream.wait_for("recording.started")[0]["data"]
    assert event == {"job_id": job_id, "mode": "action", "action_id": "a1",
                     "source": "api", "route": ""}

    state = in_worker(lambda: world.client.get("/v1/live/state", headers=AUTH).json())
    assert (state["recording"], state["mode"], state["action_id"], state["job_id"]) == (
        True, "action", "a1", job_id,
    )
    again = post(world, "/v1/recording/start")
    assert (again.status_code, again.json()["error"]["code"]) == (409, "busy")

    stopped = post(world, "/v1/recording/stop")
    assert stopped.status_code == 202
    assert stream.wait_for("recording.stopped")[0]["data"]["job_id"] == job_id
    assert world.runtime.has_active_job
    idle_stop = post(world, "/v1/recording/stop")
    assert (idle_stop.status_code, idle_stop.json()["error"]["code"]) == (422, "refused")

    canceled = post(world, "/v1/recording/cancel")
    assert canceled.status_code == 202
    assert stream.wait_for("recording.canceled")[0]["data"]["job_id"] == job_id


def test_a_start_the_app_refuses_answers_with_what_the_user_was_told(world):
    world.controller.readiness = "Loading the speech engine…"
    refused = post(world, "/v1/recording/start")
    assert refused.status_code == 422
    assert refused.json()["error"] == {"code": "refused", "message": "Loading the speech engine…"}
    assert not world.controller.recorder.is_recording

    world.controller.readiness = ""
    world.controller.meeting = True
    busy = post(world, "/v1/recording/start")
    assert (busy.status_code, busy.json()["error"]["code"]) == (409, "busy")


def test_permissions_still_gate_the_real_app(world):
    world.settings.save_setting(SettingsKey.API_CONTROL_RECORDING, False)
    denied = post(world, "/v1/recording/start")
    assert (denied.status_code, denied.json()["error"]["code"]) == (403, "permission_denied")
    assert not world.controller.recorder.is_recording
    assert permissions.granted(world.settings.load_all_settings(), permissions.RUN_ACTIONS)
