"""Host Mode: what the host counts, the dashboard that shows it, and the view.

The host tests run a real TLS host on 127.0.0.1 with a fake engine behind
it; the dashboard reads a fake ``RemoteEngineService``.
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt6.QtCore import QRect
from PyQt6.QtWidgets import QApplication

from config import config
from services.remote_asr import protocol, tailscale
from services.remote_asr.activity import MAX_REASON_CHARS, HostActivity, empty_snapshot
from services.remote_asr.engines import HostEngine
from services.settings import SettingsKey, settings_manager


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _pump(times: int = 5) -> None:
    for _ in range(times):
        QApplication.processEvents()


# ---- what the host counts ----

def test_activity_counts_each_computers_requests_and_lists_newest_first():
    clock = Clock()
    activity = HostActivity(clock=clock)
    activity.connected("d1", "laptop")
    clock.now += 5
    activity.transcribed("d1", "laptop", 8.8, 0.4)
    activity.previewed("d1", "laptop")
    clock.now += 5
    activity.transcribed("d2", "desk", 2.0, 0.1)

    snapshot = activity.snapshot()
    assert snapshot["transcriptions"] == 2
    assert snapshot["audio_s"] == pytest.approx(10.8)
    assert snapshot["host_s"] == pytest.approx(0.5)
    assert snapshot["previews"] == 1
    assert snapshot["last_at"] == 1010.0
    assert snapshot["devices"]["d1"]["transcriptions"] == 1
    assert snapshot["devices"]["d1"]["previews"] == 1
    # Live preview windows are counted but too frequent to list.
    assert [e["kind"] for e in snapshot["events"]] == ["transcribed", "transcribed", "connected"]
    assert snapshot["events"][0]["name"] == "desk"
    assert snapshot["serial"] == 3
    assert [e["name"] for e in activity.snapshot(events=1)["events"]] == ["desk"]


def test_a_computer_connects_once_however_many_connections_it_opens():
    activity = HostActivity()
    activity.connected("d1", "laptop")
    activity.connected("d1", "laptop")  # a meeting, beside its dictation
    activity.disconnected("d1", "laptop")
    assert [e["kind"] for e in activity.snapshot()["events"]] == ["connected"]
    activity.disconnected("d1", "laptop")
    assert [e["kind"] for e in activity.snapshot()["events"]] == ["disconnected", "connected"]


def test_sharing_again_starts_a_new_session_but_keeps_the_serial_rising():
    clock = Clock()
    activity = HostActivity(clock=clock)
    activity.connected("d1", "laptop")
    activity.transcribed("d1", "laptop", 3.0, 0.2)
    serial = activity.snapshot()["serial"]
    clock.now = 2000.0
    activity.reset()
    # A connection from before the restart closes afterwards.
    activity.disconnected("d1", "laptop")

    snapshot = activity.snapshot()
    assert snapshot["since"] == 2000.0
    assert snapshot["transcriptions"] == 0
    assert snapshot["events"] == [] and snapshot["devices"] == {}
    activity.paired("d2", "desk", "tailscale")
    assert activity.snapshot()["serial"] > serial


def test_a_failure_keeps_a_short_one_line_reason():
    activity = HostActivity()
    activity.failed("d1", "laptop", "Engine\n   exploded " + "x" * 400)
    event = activity.snapshot()["events"][0]
    assert event["kind"] == "failed"
    assert event["reason"].startswith("Engine exploded x")
    assert len(event["reason"]) == MAX_REASON_CHARS and event["reason"].endswith("…")
    assert activity.snapshot()["errors"] == 1


class _Engine(HostEngine):
    identity = ("fake", "parakeet", "v3")

    def __init__(self):
        self.fail = False

    def describe(self):
        return {"family": "parakeet", "model": "v3", "label": "Fake Parakeet", "device": "cuda",
                "streaming": True, "available": True, "status": "ready"}

    def transcribe(self, audio, language):
        if self.fail:
            raise RuntimeError("the engine fell over")
        return {"text": "ok", "segments": []}

    def stream(self, session, audio, language, finish):
        return {"events": []}


@pytest.fixture
def speech_host(tmp_path, monkeypatch):
    from services.remote_asr.host import DeviceRegistry, SpeechHost
    from services.remote_asr.tls import ensure_host_identity

    monkeypatch.setattr(tailscale, "status",
                        lambda timeout=4.0: tailscale.TailscaleStatus("not_installed"))
    monkeypatch.setattr(tailscale, "whois", lambda address, timeout=4.0: None)
    devices: list = []

    def save(entries):
        devices[:] = [dict(entry) for entry in entries]

    engine = _Engine()
    host = SpeechHost(
        engine_provider=lambda: engine,
        registry=DeviceRegistry(lambda: list(devices), save),
        identity=ensure_host_identity(str(tmp_path / "host")),
        host_name="devbox",
    )
    host.start(port=0, bind="127.0.0.1")
    yield host, engine
    host.stop()


def _paired_connection(host):
    from services.remote_asr.client import RemoteConnection, pair_with_host

    result = pair_with_host("127.0.0.1", host.port, host.open_pairing(), "laptop")
    connection = RemoteConnection("127.0.0.1", host.port, result.token, result.fingerprint)
    connection.connect()
    return connection


def _tone(samples: int) -> np.ndarray:
    return (0.1 * np.sin(np.arange(samples, dtype=np.float32) / 8)).astype(np.float32)


def test_the_host_logs_what_a_paired_computer_asked_for(speech_host):
    host, engine = speech_host
    connection = _paired_connection(host)
    try:
        # The ready frame can arrive before the host registers the connection.
        assert _wait_for(lambda: len(host.connected_clients()) == 1)
        assert host.connected_clients()[0]["since"] == pytest.approx(time.time(), abs=5)
        connection.request("transcribe", audio=_tone(2 * protocol.SAMPLE_RATE))
        connection.request("stream", audio=_tone(1600), session="preview", finish=False)
        engine.fail = True
        with pytest.raises(RuntimeError, match="fell over"):
            connection.request("transcribe", audio=_tone(1600))
    finally:
        connection.close()
    assert _wait_for(lambda: host.activity.snapshot()["events"][0]["kind"] == "disconnected")

    snapshot = host.activity.snapshot()
    assert snapshot["transcriptions"] == 1
    assert snapshot["audio_s"] == pytest.approx(2.0)
    assert snapshot["previews"] == 1 and snapshot["errors"] == 1
    assert [e["kind"] for e in reversed(snapshot["events"])] == [
        "paired", "connected", "transcribed", "failed", "disconnected",
    ]
    (device,) = snapshot["devices"].values()
    assert device["name"] == "laptop" and device["transcriptions"] == 1


def test_a_transcription_is_counted_before_the_event_that_ends_it(speech_host):
    host, _engine = speech_host
    seen = []

    def on_event(kind, detail):
        if kind == "activity" and not detail["busy"]:
            seen.append(host.activity.snapshot()["transcriptions"])

    host._on_event = on_event
    connection = _paired_connection(host)
    try:
        connection.request("transcribe", audio=_tone(1600))
    finally:
        connection.close()
    assert seen == [1]


def test_the_service_reads_activity_and_the_engine_while_not_sharing(tmp_path):
    from services.remote_asr.service import RemoteEngineService

    service = RemoteEngineService(lambda: None, identity_dir=str(tmp_path / "id"), bind="127.0.0.1")
    assert service.host_activity() == empty_snapshot()
    engine = service.engine_state()
    assert engine["available"] is False and "No transcription engine" in engine["status"]


# ---- the dashboard ----

class FakeHostService:
    """What the dashboard reads of ``RemoteEngineService``."""

    def __init__(self):
        self.listeners = []
        self.calls = []
        self.state = {
            "enabled": True, "running": True, "port": 47821, "error": "",
            "host_name": "devbox", "address": "192.168.1.5", "address_kind": "lan",
            "fingerprint": "ABCD1234EF567890ABCD1234EF567890", "pairing": None,
            "devices": [
                {"id": "d1", "name": "laptop", "paired_at": "2026-09-26T21:14:00+00:00",
                 "via": "tailscale", "last_seen": "2026-09-28T08:00:00+00:00"},
                {"id": "d2", "name": "desk", "paired_at": "2026-09-27T17:02:00+00:00",
                 "via": "code", "last_seen": ""},
            ],
            "engine": None,
            "tailscale": tailscale.TailscaleStatus("running", name="devbox", address="100.64.0.7",
                                                   owner="me@example.com"),
            "tailscale_trust": True, "model_management": False, "keep_records": True,
        }
        self.clients = []
        self.activity = empty_snapshot(time.time() - 600)
        self.engine = {"family": "parakeet", "model": "v3", "label": "Fake Parakeet",
                       "device": "cuda", "compute_type": "", "streaming": True,
                       "available": True, "status": "ready"}
        self.models = [
            {"family": "parakeet", "model": "v3", "label": "Fake Parakeet"},
            {"family": "local_whisper", "model": "turbo", "label": "Whisper turbo"},
        ]
        self.records = {"d2": {"dictation": {"count": 2, "bytes": 2048}}}

    def add_listener(self, listener):
        self.listeners.append(listener)

    def remove_listener(self, listener):
        if listener in self.listeners:
            self.listeners.remove(listener)

    def host_state(self):
        self.calls.append("host_state")
        state = dict(self.state)
        state["clients"] = list(self.clients)
        state["engine"] = self.engine if state["running"] else None
        return state

    def connected_clients(self):
        return list(self.clients)

    def host_activity(self, events=None):
        snapshot = dict(self.activity)
        snapshot["events"] = list(snapshot["events"])[:events] if events else list(snapshot["events"])
        return snapshot

    def engine_state(self):
        return dict(self.engine)

    def host_models(self):
        return list(self.models)

    def records_summary(self, device_id):
        return self.records.get(device_id, {})

    def set_host_enabled(self, enabled):
        self.calls.append(("set_host_enabled", enabled))
        self.state["enabled"] = self.state["running"] = enabled

    def open_pairing(self):
        self.calls.append("open_pairing")
        self.state["pairing"] = ("123456", 90.0)
        return "123456"

    def cancel_pairing(self):
        self.calls.append("cancel_pairing")
        self.state["pairing"] = None


def _event(serial, kind, name="laptop", **fields):
    return {"serial": serial, "at": time.time() - 60, "kind": kind, "name": name, **fields}


@pytest.fixture
def service():
    return FakeHostService()


def _page(service, mcp=None):
    from ui_qt.widgets.host_dashboard import HostDashboard

    page = HostDashboard()
    page.run_in_background = lambda work: work()
    page.bind(service)
    page.bind_mcp(mcp)
    page.refresh()
    return page


@pytest.fixture(params=["classic", "omarchy"])
def host_ui(request, monkeypatch):
    monkeypatch.setenv("OPENWHISPER_UI", request.param)


@pytest.fixture
def dashboard(service, host_ui):
    return _page(service)


def test_an_unbound_page_says_host_mode_isnt_available():
    from ui_qt.widgets.host_dashboard import HostDashboard

    page = HostDashboard()
    assert page.hero_title.text() == "Host Mode isn't available here"
    assert not page.start_button.isVisibleTo(page)


def test_the_page_waits_to_be_shown_before_asking_the_service(service):
    from ui_qt.widgets.host_dashboard import HostDashboard

    page = HostDashboard()
    page.run_in_background = lambda work: work()
    page.bind(service)
    service.listeners[0]("state")
    _pump()
    # Asking means asking Tailscale; a window that never shows it shouldn't.
    assert "host_state" not in service.calls
    page.show()
    _pump()
    assert "host_state" in service.calls
    assert page.hero_title.text() == "Sharing Fake Parakeet"


def test_sharing_off_offers_to_start(dashboard, service):
    service.state.update(enabled=False, running=False)
    dashboard.refresh()
    assert dashboard.hero_title.text() == "Sharing is off"
    assert dashboard.hero.property("state") == "off"
    assert dashboard.start_button.isVisibleTo(dashboard)
    assert not dashboard.stop_button.isVisibleTo(dashboard)
    assert not dashboard.pair_button.isVisibleTo(dashboard)
    assert dashboard.pair_note.text() == "Start sharing to pair a computer."
    dashboard.start_button.click()
    assert ("set_host_enabled", True) in service.calls
    assert dashboard.hero_title.text() == "Sharing Fake Parakeet"


def test_sharing_names_the_engine_where_to_reach_it_and_the_identity(dashboard, service):
    assert dashboard.hero.property("state") == "on"
    assert dashboard.beacon.state == "on"
    detail = dashboard.hero_detail.text()
    assert "devbox at 192.168.1.5:47821" in detail
    assert "Tailscale: devbox at 100.64.0.7" in detail
    assert dashboard.hero_identity.text() == (
        f"Identity {protocol.short_fingerprint(service.state['fingerprint'])}"
    )
    assert dashboard.stop_button.isVisibleTo(dashboard)
    dashboard.stop_button.click()
    assert ("set_host_enabled", False) in service.calls


def test_a_port_in_use_is_shown_with_a_way_to_turn_sharing_off(dashboard, service):
    service.state.update(running=False, error="Couldn't listen on port 47821 (in use).")
    dashboard.refresh()
    assert dashboard.hero.property("state") == "error"
    assert dashboard.hero_title.text() == "Sharing couldn't start"
    assert dashboard.hero_detail.text().startswith("Couldn't listen on port 47821")
    assert dashboard.stop_button.text() == "Turn off sharing"


def test_connected_computers_are_grouped_and_show_who_is_being_served(dashboard, service):
    now = time.time()
    service.clients = [
        {"device_id": "d1", "name": "laptop", "address": "100.64.0.9", "busy": True, "since": now - 600},
        {"device_id": "d1", "name": "laptop", "address": "100.64.0.9", "busy": False, "since": now - 60},
        {"device_id": "d2", "name": "desk", "address": "192.168.1.7", "busy": False, "since": now - 300},
    ]
    service.activity = dict(empty_snapshot(now - 900), transcriptions=3, audio_s=60.0, host_s=3.0,
                            devices={"d1": {"name": "laptop", "transcriptions": 3, "audio_s": 60.0,
                                            "host_s": 3.0, "previews": 0, "errors": 0,
                                            "last_at": now - 30}})
    dashboard.refresh()

    rows = dict(dashboard._client_rows)
    assert set(rows) == {"d1", "d2"}
    assert rows["d1"].state.text() == "Transcribing…"
    assert "3 transcriptions" in rows["d1"].stats.text()
    assert "2 connections" in rows["d1"].stats.text()
    assert rows["d2"].state.text() == "connected for 5 min"
    assert rows["d2"].stats.text() == "Nothing transcribed yet"
    assert dashboard.clients_card.badge.text() == "2"
    assert dashboard.stat_connected.value.text() == "2"
    assert dashboard.beacon.busy
    statuses = {device_id: row[1].text() for device_id, row in dashboard._device_rows.items()}
    assert statuses == {"d1": "Connected", "d2": "Connected"}

    service.clients = []
    dashboard._on_service_event("clients")
    dashboard._flush(force=True)
    assert dashboard._client_rows == {}
    # Gone at once, not left painted until Qt deletes them.
    assert not rows["d1"].isVisibleTo(dashboard)
    assert dashboard.clients_empty.isVisibleTo(dashboard)
    assert not dashboard.beacon.busy


def test_stats_and_recent_activity_follow_what_the_host_counted(service):
    from ui_qt.widgets.host_dashboard import event_text

    events = [
        _event(3, "transcribed", audio_s=8.8, host_s=0.41),
        _event(2, "connected"),
        _event(1, "paired", via="tailscale"),
    ]
    service.activity = dict(empty_snapshot(time.time() - 900), transcriptions=3, audio_s=90.0,
                            host_s=4.5, errors=1, serial=3, events=events)
    dashboard = _page(service)

    assert dashboard.stat_transcriptions.value.text() == "3"
    assert dashboard.stat_transcriptions.detail.text().endswith("· 1 failed")
    assert dashboard.stat_audio.value.text() == "1 min"
    assert dashboard.stat_speed.value.text() == "20×"
    assert dashboard.stat_speed.detail.text() == "1.5 s per request"
    lines = [dashboard._activity_layout.itemAt(i).widget() for i in range(dashboard._activity_layout.count())]
    assert [line.text.text() for line in lines] == [event_text(e) for e in events]
    assert lines[0].text.text() == "laptop · 8.8 s of audio, back in 410 ms"
    assert lines[2].text.text() == "Paired laptop over Tailscale"
    assert not any(line._glow for line in lines)  # Nothing is new on first sight.

    service.activity = dict(service.activity, serial=4,
                            events=[_event(4, "failed", reason="engine busy")] + events)
    dashboard._on_service_event("activity")
    dashboard._flush(force=True)
    lines = [dashboard._activity_layout.itemAt(i).widget() for i in range(dashboard._activity_layout.count())]
    assert lines[0].text.text() == "laptop · couldn't transcribe: engine busy"
    assert lines[0]._glow > 0 and not lines[1]._glow


def test_nothing_yet_says_where_activity_will_appear(dashboard):
    assert dashboard.activity_empty.isVisibleTo(dashboard)
    assert dashboard.stat_speed.value.text() == "—"
    assert dashboard.clients_empty.isVisibleTo(dashboard)


def test_pairing_shows_the_code_until_canceled(dashboard, service):
    assert dashboard.pair_button.isVisibleTo(dashboard)
    assert "me@example.com" in dashboard.pair_note.text()
    dashboard.pair_button.click()
    assert "open_pairing" in service.calls
    assert dashboard.pairing_code.text() == "123 456"
    assert dashboard.pairing_box.isVisibleTo(dashboard)
    assert not dashboard.pair_button.isVisibleTo(dashboard)
    assert "Expires in 1:" in dashboard.pairing_expiry.text()
    dashboard.cancel_pairing_button.click()
    assert "cancel_pairing" in service.calls
    assert not dashboard.pairing_box.isVisibleTo(dashboard)
    assert dashboard.pair_button.isVisibleTo(dashboard)


def test_paired_computers_show_how_they_paired_and_what_they_keep_here(dashboard):
    rows = dashboard._device_rows
    assert set(rows) == {"d1", "d2"}
    assert dashboard.devices_card.badge.text() == "2"
    desk = rows["d2"][0]
    details = " ".join(label.text() for label in desk.findChildren(type(rows["d2"][1])))
    assert "keeps 2 dictations" in details
    assert rows["d1"][1].text().startswith("Seen ")
    assert rows["d2"][1].text() == ""


def test_the_engine_picker_switches_through_the_signal_and_says_why_not(dashboard):
    combo = dashboard.model_combo
    assert [combo.itemText(i) for i in range(combo.count())] == ["Fake Parakeet", "Whisper turbo"]
    assert combo.currentText() == "Fake Parakeet" and combo.isEnabled()
    assert dashboard.engine_name.text() == "Fake Parakeet"
    assert dashboard.engine_detail.text() == "On CUDA · live preview supported"
    asked = []
    dashboard.model_requested.connect(lambda family, model: asked.append((family, model)))

    combo.activated.emit(0)  # the one already served
    assert asked == []
    combo.activated.emit(1)
    assert asked == [("local_whisper", "turbo")]
    assert dashboard.engine_detail.text() == "Switching to Whisper turbo…"
    assert not dashboard.model_combo.isEnabled()

    dashboard.show_engine_error("This computer is transcribing right now.")
    assert dashboard.engine_status.text() == "This computer is transcribing right now."
    assert dashboard.model_combo.isEnabled()


def test_a_finished_load_ends_the_switch(dashboard, service):
    dashboard.model_combo.activated.emit(1)
    dashboard.set_engine_busy(True)
    dashboard._flush(force=True)
    assert dashboard.engine_dot._busy
    service.engine = dict(service.engine, family="local_whisper", model="turbo", label="Whisper turbo",
                          streaming=False)
    dashboard.set_engine_busy(False)
    dashboard._flush(force=True)
    assert dashboard._switching is None
    assert dashboard.engine_name.text() == "Whisper turbo"
    assert dashboard.model_combo.currentText() == "Whisper turbo"
    assert dashboard.engine_detail.text() == "On CUDA · no live preview"


def test_without_ready_models_the_picker_points_to_downloads(dashboard, service):
    service.models = []
    service.engine = {"family": "", "model": "", "label": "", "device": "", "streaming": False,
                      "available": False, "status": "Select a local engine there."}
    dashboard.refresh()
    assert dashboard.model_combo.currentText() == "No models are ready"
    assert not dashboard.model_combo.isEnabled()
    assert dashboard.engine_name.text() == "No local engine selected"
    assert dashboard.engine_status.text() == "Select a local engine there."
    assert dashboard.models_link.isVisibleTo(dashboard)
    opened = []
    dashboard.settings_requested.connect(opened.append)
    dashboard.models_link.click()
    dashboard.manage_link.click()
    assert opened == ["voice_model", "remote_engine"]


def test_the_beacon_ripples_when_a_transcription_comes_back(dashboard, service):
    dashboard.show()
    _pump()
    assert dashboard.beacon._ripples == []
    service.activity = dict(service.activity, transcriptions=1, serial=1,
                            events=[_event(1, "transcribed", audio_s=2.0, host_s=0.1)])
    dashboard._on_service_event("activity")
    dashboard._flush()
    assert len(dashboard.beacon._ripples) == 1


class FakeMcp:
    """What the dashboard reads of the app's ``McpRuntime``."""

    def __init__(self, state="stopped", message="MCP is off."):
        self.calls = []
        self.access_token = "test-mcp-access-token"
        self._set(state, message)

    def _set(self, state, message=""):
        self._status = SimpleNamespace(state=state, message=message, port=8767,
                                       url="http://127.0.0.1:8767/mcp")

    def status(self):
        return self._status

    def token(self):
        return self.access_token if self._status.state == "running" else ""

    def restore(self, settings):
        self.calls.append(("restore", settings.get("mcp_enabled")))
        self._set("running", "Ready for agent connections.")

    def stop(self, *, wait=False):
        self.calls.append("stop")
        self._set("stopped", "MCP is off.")


@pytest.fixture
def mcp_setting(monkeypatch):
    # Defined with the MCP server; a build without it has no such key.
    monkeypatch.setattr(SettingsKey, "MCP_ENABLED", "mcp_enabled", raising=False)


def test_without_an_mcp_server_there_is_no_mcp_card(dashboard):
    assert not dashboard.mcp_card.isVisibleTo(dashboard)
    assert not dashboard.mcp_copy_token.isEnabled()
    assert not dashboard.mcp_copy_prompt.isEnabled()


def test_the_page_finds_the_apps_mcp_server_when_first_shown(service, monkeypatch):
    from ui_qt.widgets import host_dashboard

    server = FakeMcp()
    monkeypatch.setattr(host_dashboard, "default_mcp_server", lambda: server)
    page = host_dashboard.HostDashboard()
    page.run_in_background = lambda work: work()
    page.bind(service)
    page.refresh()
    assert page.mcp_card.isVisibleTo(page)
    assert page.mcp_status.text() == "Off"


def test_mcp_turns_on_and_off_with_the_setting_settings_uses(service, mcp_setting, host_ui):
    server = FakeMcp()
    page = _page(service, server)
    assert page.mcp_button.text() == "Turn on"
    assert not page.mcp_copy_token.isEnabled()
    page.mcp_button.click()
    assert settings_manager.get("mcp_enabled") is True
    assert server.calls == [("restore", True)]
    assert page.mcp_status.text() == "Running at http://127.0.0.1:8767/mcp"
    assert page.mcp_button.text() == "Turn off"
    assert page.mcp_copy_token.isEnabled()
    page.mcp_button.click()
    assert settings_manager.get("mcp_enabled") is False
    assert server.calls[-1] == "stop"
    assert page.mcp_status.text() == "Off"
    assert not page.mcp_copy_token.isEnabled()


def test_mcp_that_couldnt_start_says_why_and_offers_to_try_again(service):
    message = "Could not listen on port 8767. Choose another port or close the app using it."
    page = _page(service, FakeMcp("error", message))
    assert page.mcp_status.text() == "Couldn't start"
    assert page.mcp_detail.text() == message
    assert page.mcp_button.text() == "Try again"


def test_mcp_says_it_reaches_what_paired_computers_keep_here(service):
    page = _page(service, FakeMcp())
    assert "records paired computers keep here" in page.mcp_intro.text()
    service.state["keep_records"] = False
    page.refresh()
    assert "paired computers" not in page.mcp_intro.text()
    opened = []
    page.settings_requested.connect(opened.append)
    page.mcp_link.click()
    assert opened == ["mcp"]


def test_host_copy_prompt_targets_tailscale_and_explains_local_only(service, host_ui):
    server = FakeMcp("running", "Ready")
    server._status.remote_url = "http://100.82.22.3:8767/mcp"
    page = _page(service, server)
    assert server._status.remote_url in page.mcp_status.text()
    page.mcp_copy_prompt.click()
    copied = QApplication.clipboard().text()
    assert server._status.remote_url in copied
    assert "same Tailscale network" in copied
    assert "127.0.0.1" not in copied
    server._status.remote_url = ""
    page.refresh()
    assert "Allow agents over Tailscale" in page.mcp_detail.text()
    page.mcp_copy_prompt.click()
    assert "Do not connect to or enable a different" in QApplication.clipboard().text()


def test_host_copies_the_current_access_token_with_independent_feedback(service, host_ui):
    from PyQt6.QtTest import QTest

    server = FakeMcp("running")
    page = _page(service, server)
    server.access_token = "updated-mcp-access-token"
    page._mcp_token_copy_feedback_timer.setInterval(1)
    page.mcp_copy_token.click()
    assert QApplication.clipboard().text() == server.access_token
    assert page.mcp_copy_token.text() == "Copied"
    assert page.mcp_copy_prompt.text() == "Copy agent install prompt"
    assert server.access_token not in page.mcp_status.text()
    assert server.access_token not in page.mcp_detail.text()
    deadline = time.monotonic() + 1.0
    while page.mcp_copy_token.text() == "Copied" and time.monotonic() < deadline:
        QTest.qWait(10)
    assert page.mcp_copy_token.text() == "Copy access token"
    page.mcp_copy_prompt.click()
    assert page.mcp_copy_prompt.text() == "Copied"
    assert page.mcp_copy_token.text() == "Copy access token"
    assert f"Authorization: Bearer {server.access_token}" in QApplication.clipboard().text()


@pytest.mark.parametrize("state", ["stopped", "starting", "stopping", "error"])
def test_host_does_not_copy_a_token_after_mcp_leaves_running(service, host_ui, state):
    server = FakeMcp("running")
    page = _page(service, server)
    QApplication.clipboard().setText("existing clipboard")
    server._set(state)
    page.mcp_copy_token.click()
    assert QApplication.clipboard().text() == "existing clipboard"
    assert not page.mcp_copy_token.isEnabled()
    assert not page.mcp_copy_prompt.isEnabled()
    assert page.mcp_copy_token.text() == "Copy access token"


@pytest.mark.parametrize("failure", ["empty_token", "token_error", "status_error", "unbound"])
def test_host_token_unavailable_leaves_the_clipboard_unchanged(service, host_ui, failure):
    server = FakeMcp("running")
    page = _page(service, server)
    QApplication.clipboard().setText("existing clipboard")
    if failure == "empty_token":
        server.access_token = ""
    elif failure == "token_error":
        server.token = Mock(side_effect=RuntimeError("unavailable"))
    elif failure == "status_error":
        server.status = Mock(side_effect=RuntimeError("unavailable"))
        page._refresh_mcp()
        assert not page.mcp_copy_token.isEnabled()
        assert not page.mcp_copy_prompt.isEnabled()
    else:
        page.bind_mcp(None)
        assert not page.mcp_copy_token.isEnabled()
    page._copy_mcp_access_token()
    assert QApplication.clipboard().text() == "existing clipboard"
    assert page.mcp_copy_token.text() == "Copy access token"


def test_host_mcp_actions_fit_narrow_windows_and_reflow_when_widened(service, host_ui):
    from PyQt6.QtWidgets import QBoxLayout

    page = _page(service, FakeMcp("running"))
    page.resize(420, 600)
    page.show()
    QApplication.processEvents()
    assert page.width() == 420
    assert page._mcp_actions.direction() == QBoxLayout.Direction.TopToBottom
    buttons = (page.mcp_copy_token, page.mcp_copy_prompt)
    for button in buttons:
        assert button.isVisibleTo(page)
        left = button.mapTo(page.scroll.viewport(), button.rect().topLeft()).x()
        assert page.scroll.viewport().rect().contains(
            QRect(left, 0, button.width(), 1)
        )
    assert buttons[1].y() > buttons[0].y()
    page.resize(1200, 600)
    QApplication.processEvents()
    assert page._mcp_actions.direction() == QBoxLayout.Direction.LeftToRight
    assert buttons[1].x() > buttons[0].x()


# ---- the main window's view ----

@pytest.fixture
def window():
    from ui_qt.main_window import MainWindow

    main_window = MainWindow()
    yield main_window
    main_window._force_quit = True
    main_window.close()


def test_main_host_window_has_the_shared_mcp_copy_controls(host_ui, window, service):
    server = FakeMcp("running")
    page = window.host_dashboard
    page.run_in_background = lambda work: work()
    page.bind(service)
    page.bind_mcp(server)
    window.set_host_mode(True, persist=False)
    page.refresh()
    assert page.mcp_copy_token.isVisibleTo(window)
    assert page.mcp_copy_prompt.isVisibleTo(window)
    page.mcp_copy_token.click()
    assert QApplication.clipboard().text() == server.access_token


def test_host_mode_swaps_the_tabs_for_the_dashboard_and_back(window):
    window.setGeometry(40, 50, 680, 560)
    normal = window.geometry()

    window.set_host_mode(True)
    assert window.host_mode
    assert window.host_dashboard.isVisibleTo(window)
    assert not window.tabbed_content.isVisibleTo(window)
    assert not window.history_sidebar.isVisibleTo(window)
    assert not window.history_edge_tab.isVisibleTo(window)
    assert window.title_bar.title_label.text() == "OpenWhisper Host"
    assert window.host_mode_action.isChecked()
    assert not window.sidebar_action.isEnabled() and not window.compact_action.isEnabled()
    assert settings_manager.get(SettingsKey.HOST_MODE) is True
    assert window.width() > normal.width()

    window.set_host_mode(False)
    assert not window.host_mode
    assert window.tabbed_content.isVisibleTo(window)
    assert window.history_edge_tab.isVisibleTo(window)
    assert not window.host_dashboard.isVisibleTo(window)
    assert window.geometry() == normal
    assert window.title_bar.title_label.text() == "OpenWhisper"
    assert window.sidebar_action.isEnabled() and window.compact_action.isEnabled()
    assert not window.host_mode_action.isChecked()
    assert settings_manager.get(SettingsKey.HOST_MODE) is False


def test_host_mode_keeps_its_own_window_size_and_place(window):
    window.set_host_mode(True)
    window.setGeometry(10, 5, 700, config.MAIN_WINDOW_MIN_HEIGHT)
    window.set_host_mode(False)
    assert settings_manager.get(SettingsKey.HOST_WINDOW_GEOMETRY) == {
        "x": 10, "y": 5, "width": 700, "height": config.MAIN_WINDOW_MIN_HEIGHT,
    }
    window.set_host_mode(True)
    assert window.geometry() == QRect(10, 5, 700, config.MAIN_WINDOW_MIN_HEIGHT)


def test_the_first_host_mode_window_grows_to_fit_the_dashboard(window):
    window.setGeometry(0, 0, config.MAIN_WINDOW_DEFAULT_WIDTH, config.MAIN_WINDOW_MIN_HEIGHT)
    window.set_host_mode(True)
    available = window._available_screen_rect()
    assert window.width() == min(config.MAIN_WINDOW_HOST_DEFAULT_WIDTH, available.width())
    assert window.height() == min(config.MAIN_WINDOW_HOST_DEFAULT_HEIGHT, available.height())


def test_host_mode_leaves_compact_mode_and_the_recording_views_stay_shut(window):
    window.set_compact_mode(True)
    window.set_host_mode(True)
    assert not window._compact_mode and window.host_mode
    assert settings_manager.get(SettingsKey.COMPACT_MODE) is False

    window.toggle_history()
    window.toggle_compact_mode()
    window.set_compact_mode(True)
    assert not window.history_sidebar.is_expanded
    assert not window._compact_mode and window.host_mode


def test_saved_host_mode_comes_back_at_launch():
    from ui_qt.main_window import MainWindow

    settings_manager.save_setting(SettingsKey.HOST_MODE, True)
    main_window = MainWindow()
    try:
        assert main_window.host_mode and main_window.host_mode_action.isChecked()
        assert main_window.host_dashboard.isVisibleTo(main_window)
    finally:
        main_window._force_quit = True
        main_window.close()


def test_the_view_menu_toggles_host_mode(window):
    view_menu = next(
        action.menu() for action in window.title_bar.menu_bar.actions() if action.text() == "View"
    )
    labels = [action.text() for action in view_menu.actions() if action.text()]
    assert labels[:4] == ["History", "History Calendar", "Compact Mode", "Host Mode"]
    action = window.host_mode_action
    assert action.isCheckable()
    assert action.shortcut().toString() == "Ctrl+Shift+H"
    action.trigger()
    assert window.host_mode
    action.trigger()
    assert not window.host_mode


def test_the_dashboards_model_choice_reaches_the_window_signal(window):
    chosen = []
    window.host_model_selected.connect(lambda family, model: chosen.append((family, model)))
    window.host_dashboard.model_requested.emit("parakeet", "v3")
    assert chosen == [("parakeet", "v3")]


def test_a_dictation_finishing_behind_the_dashboard_leaves_its_size_alone(window):
    window.set_host_mode(True)
    size = window.size()
    window._on_stats_visibility_changed(True)
    assert not hasattr(window, "_resize_animation") or (
        window._resize_animation.state() != window._resize_animation.State.Running
    )
    assert window.size() == size


# ---- the controllers ----

def test_a_meeting_starting_here_leaves_host_mode_for_its_tab():
    from ui_qt.ui_controller import UIController

    main_window = Mock(host_mode=True)
    UIController.switch_to_tab(SimpleNamespace(main_window=main_window), 2)
    main_window.set_host_mode.assert_called_once_with(False)
    main_window.tabbed_content.set_current_index.assert_called_once_with(2)


def test_the_dashboard_reads_the_service_the_controller_is_given():
    from ui_qt.ui_controller import UIController

    stand_in = SimpleNamespace(main_window=Mock(), _remote_engine=None)
    service = object()
    UIController.remote_engine.fset(stand_in, service)
    assert UIController.remote_engine.fget(stand_in) is service
    stand_in.main_window.host_dashboard.bind.assert_called_once_with(service)


def test_a_refused_model_switch_is_shown_on_the_dashboard():
    from ui_qt.ui_controller import UIController

    main_window = Mock()
    UIController._on_host_model_selected(
        SimpleNamespace(on_host_model_selected=lambda family, model: "Busy.", main_window=main_window),
        "parakeet", "v3",
    )
    main_window.host_dashboard.show_engine_error.assert_called_once_with("Busy.")
    main_window.reset_mock()
    UIController._on_host_model_selected(
        SimpleNamespace(on_host_model_selected=lambda family, model: None, main_window=main_window),
        "parakeet", "v3",
    )
    main_window.host_dashboard.show_engine_error.assert_not_called()


def _switching_controller(current="parakeet"):
    """The controller's switch methods on a stand-in whose window selects backends."""
    from services.application_controller import ApplicationController

    controller = SimpleNamespace(
        _current_model_name=current,
        _reload_pending=False,
        _reload_note="",
        recorder=SimpleNamespace(is_recording=False),
        meeting=False,
        transcription_backends={},
        ui_controller=Mock(),
        reload_whisper_model=Mock(),
    )
    controller.is_meeting_active = lambda: controller.meeting
    controller.is_transcribing = lambda: False

    def select(display):
        controller._current_model_name = config.MODEL_VALUE_MAP[display]

    controller.ui_controller.select_transcription_backend.side_effect = select
    for name in ("_switch_engine_to", "select_host_model"):
        setattr(controller, name, getattr(ApplicationController, name).__get__(controller))
    return controller


def test_host_mode_switches_models_the_way_a_paired_computer_does():
    controller = _switching_controller()
    assert controller.select_host_model("nemotron", "nemotron-3.5") is None
    models = settings_manager.load_all_settings()[SettingsKey.LOCAL_ASR_MODELS]
    assert models["nemotron"] == "nemotron-3.5"
    controller.ui_controller.select_transcription_backend.assert_called_once_with("Nemotron Streaming")
    controller.reload_whisper_model.assert_called_once()
    assert controller._reload_note == "Switching to Nemotron 3.5 ASR 0.6B..."

    controller.meeting = True
    assert controller.select_host_model("parakeet", "parakeet-tdt-0.6b-v3") == (
        "This computer is running a meeting. Change its model after the meeting ends."
    )
