"""The Remote engine's link on the engine card, and the host's serving row."""
from __future__ import annotations

import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from transcriber.remote_backend import RemoteLink, RemoteTiming
from ui_qt.widgets.remote_link import RemoteLinkGlyph
from ui_qt.widgets.stats_display import TranscriptionStatsWidget, remote_timing_text


@pytest.fixture
def paired(monkeypatch):
    from services.remote_asr import settings as remote_settings

    monkeypatch.setattr(remote_settings, "load_client_pairing", lambda: SimpleNamespace(host_name="jed"))


def _remote_tab():
    from ui_qt.widgets.upload_file_tab import UploadFileTab

    tab = UploadFileTab()
    tab.show()
    tab.choose_backend("Remote computer")
    QApplication.processEvents()
    return tab


def _connected(**overrides):
    fields = dict(host="jed", route="Tailscale", latency_ms=24.4, engine_label="Parakeet TDT 0.6B v3",
                  device="cuda", address="100.101.102.103:47821")
    fields.update(overrides)
    return RemoteLink("connected", **fields)


# ---- the glyph ----

def test_the_glyph_only_animates_on_real_events():
    glyph = RemoteLinkGlyph()
    glyph.show()
    glyph.set_link("offline")
    assert not glyph._timer.isActive()  # a broken wire doesn't move
    glyph.set_link("connecting")
    assert glyph._timer.isActive()
    glyph.set_link("connected", beat=1)
    # The first answer draws the wire across, once.
    assert glyph._drawn_at is not None
    glyph._drawn_at = None
    glyph._tick()
    assert not glyph._timer.isActive()  # connected and idle: still
    glyph.set_link("connected", beat=2)
    assert [kind for *_, kind in glyph._flights] == ["beat"]
    glyph._flights.clear()
    glyph.set_link("connected", busy=True, beat=2)
    assert glyph._timer.isActive()
    glyph.set_link("connected", busy=False, replies=1, beat=2)
    assert [kind for *_, kind in glyph._flights] == ["reply"]
    glyph.hide()
    assert not glyph._timer.isActive()


def test_the_glyph_greets_a_host_only_the_first_time():
    glyph = RemoteLinkGlyph()
    glyph.set_link("connected")
    glyph._drawn_at = None
    glyph.set_link("offline")
    glyph.set_link("connected")
    assert glyph._drawn_at is None


def test_the_glyph_asks_to_reconnect_when_clicked():
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtCore import QEvent

    glyph = RemoteLinkGlyph()
    clicked = []
    glyph.clicked.connect(lambda: clicked.append(True))
    point = QPointF(10, 8)
    event = QMouseEvent(QEvent.Type.MouseButtonRelease, point, point, Qt.MouseButton.LeftButton,
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    glyph.mouseReleaseEvent(event)
    assert clicked == [True]


# ---- the engine card ----

def test_the_card_shows_the_link_instead_of_the_dot(paired):
    tab = _remote_tab()
    assert tab.status_dot.isVisible() and not tab.link_glyph.isVisible()
    tab.set_remote_link(_connected())
    assert tab.link_glyph.isVisible() and not tab.status_dot.isVisible()
    assert tab.resolved_label.text() == "Connected to jed · Tailscale · 24 ms"
    assert "Parakeet TDT 0.6B v3 on CUDA, served by jed at 100.101.102.103:47821" in tab.link_glyph.toolTip()
    tab.set_remote_link(_connected(route="local network", latency_ms=0.4))
    assert tab.resolved_label.text() == "Connected to jed · local network · <1 ms"
    # Another backend: back to the plain dot.
    tab.choose_backend("Local Whisper")
    QApplication.processEvents()
    assert not tab.link_glyph.isVisible()


def test_the_link_tooltip_names_the_model_once(paired):
    tab = _remote_tab()
    tab.set_remote_link(_connected(runtime_status="Parakeet TDT 0.6B v3 | cuda"))
    tip = "Parakeet TDT 0.6B v3 on CUDA, served by jed at 100.101.102.103:47821."
    assert tab.link_glyph.toolTip() == tip
    assert tab.remote_runtime_label.toolTip() == tip
    # The host's fallback note is news; its restated model and device are not.
    tab.set_remote_link(_connected(engine_label="Whisper turbo", device="cpu", compute_type="int8",
                                   runtime_status="turbo | cpu (int8) — GPU unavailable, using CPU"))
    assert tab.link_glyph.toolTip() == (
        "Whisper turbo on CPU (int8), served by jed at 100.101.102.103:47821.\nGPU unavailable, using CPU")
    tab.set_remote_link(_connected(runtime_status="Warming up the GPU"))
    assert tab.link_glyph.toolTip().endswith("\nWarming up the GPU")


def test_the_card_counts_down_to_the_next_try(paired):
    tab = _remote_tab()
    detail = "Couldn't reach 192.168.1.40:47821. Check that the host is on."
    tab.set_remote_link(RemoteLink("offline", host="jed", detail=detail, retry_at=time.monotonic() + 15.2))
    assert tab.resolved_label.text() == "Couldn't reach 192.168.1.40:47821. Trying again in 15 s."
    assert tab._link_countdown.isActive()
    assert detail in tab.link_glyph.toolTip()
    tab.set_remote_link(RemoteLink("connecting", host="jed"))
    assert tab.resolved_label.text() == "Connecting to jed..."
    assert not tab._link_countdown.isActive()
    tab.set_remote_link(RemoteLink("offline", host="jed", detail="jed stopped answering."))
    assert tab.resolved_label.text() == "jed stopped answering. Click to try again."


def test_clicking_an_offline_link_asks_to_reconnect(paired):
    tab = _remote_tab()
    asked = []
    tab.remote_retry_requested.connect(lambda: asked.append(True))
    tab.set_remote_link(_connected())
    tab.link_glyph.clicked.emit()
    assert asked == []  # connected: nothing to retry
    tab.set_remote_link(RemoteLink("offline", host="jed", detail="jed stopped answering."))
    tab.link_glyph.clicked.emit()
    assert asked == [True]


def test_transcribing_names_the_computer_doing_it(paired):
    from ui_qt.overlay_state import OverlayState

    tab = _remote_tab()
    tab.set_remote_link(_connected())
    tab.set_activity_state(OverlayState.TRANSCRIBING)
    assert tab.resolved_label.text() == "Transcribing on jed..."
    tab.set_activity_state(OverlayState.NONE)
    assert tab.resolved_label.text() == "Connected to jed · Tailscale · 24 ms"


def test_the_host_slides_in_who_it_is_serving():
    from ui_qt.widgets.upload_file_tab import UploadFileTab

    tab = UploadFileTab()
    tab.show()
    QApplication.processEvents()
    assert not tab.serving_row.isVisible()
    tab.set_remote_clients([{"name": "laptop", "busy": False}])
    assert tab.serving_row.isVisible()
    assert tab.serving_label.text() == "Sharing this engine with laptop"
    tab.set_remote_clients([{"name": "laptop", "busy": True}])
    assert tab.serving_label.text() == "Transcribing for laptop"
    assert tab.serving_glyph._busy
    tab.set_remote_clients([{"name": "laptop", "busy": False}])
    # The finished request flies home.
    assert [kind for *_, kind in tab.serving_glyph._flights] == ["reply"]
    tab.set_remote_clients([{"name": "laptop", "busy": False}, {"name": "desk", "busy": False}])
    assert tab.serving_label.text() == "Sharing this engine with 2 computers"
    tab.set_remote_clients([])
    tab._serving_anim.stop()
    tab._serving_anim.finished.emit()
    assert not tab.serving_row.isVisible()


# ---- the stats line ----

def test_remote_timing_text():
    assert remote_timing_text(None) == ""
    assert remote_timing_text(RemoteTiming("jed", 2, 0.5, 0.42)) == "on jed · 80 ms network"
    assert remote_timing_text(RemoteTiming("jed", 1, 0.5, None)) == "on jed"
    # Nothing was sent (every window silent): no network to speak of.
    assert remote_timing_text(RemoteTiming("jed", 0, 0.0, None)) == "on jed"


def test_the_stats_line_names_the_host():
    stats = TranscriptionStatsWidget()
    stats.set_stats(0.42, 8.8, 1024, remote=RemoteTiming("jed", 1, 0.35, 0.31))
    detail = stats.transcription_time_widget.detail_label
    assert detail.text() == "on jed · 40 ms network" and not detail.isHidden()
    stats.set_stats(0.42, 8.8, 1024)
    assert detail.isHidden()


def test_the_upload_card_names_the_host(tmp_path, paired):
    tab = _remote_tab()
    tab.set_transcription_stats(4.2, 73.3, 1024, remote=RemoteTiming("jed", 3, 3.9, 3.72))
    text = tab.file_info_card.result_label.text()
    assert "on jed" in text and "180 ms" in text and "network" in text


# ---- startup ----

def test_startup_imports_leave_faster_whisper_unloaded():
    """Only Local Whisper needs it; a remote or Parakeet session never loads it."""
    code = (
        "import sys; sys.path.insert(0, '.');"
        "import services.application_controller, ui_qt.ui_controller;"
        "print(int('faster_whisper' in sys.modules), int('ctranslate2' in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.split() == ["0", "0"]
