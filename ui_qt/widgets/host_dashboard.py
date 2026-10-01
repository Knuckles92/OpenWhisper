"""View → Host Mode: what someone running OpenWhisper only as a host needs to see.

A host serves its engine to paired computers (services/remote_asr), so the
recording tabs are no use to it. This page shows instead whether sharing is
on and where computers reach it, the engine it serves, who is connected and
what they asked for, pairing, and whether agents on this computer can
search what it keeps over MCP. It reads ``RemoteEngineService`` and hears
its events the way Settings → Remote engine does: they arrive on server
threads and are re-posted to the UI thread, then coalesced, so a live
preview's stream of requests redraws the page once per burst.

Motion answers real events only: the beacon's waves run while a paired
computer's audio is being decoded, it ripples once when a transcription
comes back, and a new line in Recent activity glows as it arrives.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from math import pi, sin
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import (
    QElapsedTimer,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.remote_asr import protocol
from services.remote_asr.activity import empty_snapshot
from services.settings import SettingsKey, settings_manager
from ui_qt.utils.palette import token_color
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets.buttons import Button, PrimaryButton
from ui_qt.widgets.eliding_label import ElidingLabel
from ui_qt.widgets.engine_field import EngineStatus, StatusDot
from ui_qt.widgets.no_wheel import NoWheelComboBox
from ui_qt.widgets.remote_link import RemoteLinkGlyph
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)

#: Settings destinations the page links to (ui_qt/dialogs/settings_destinations.py).
REMOTE_ENGINE_DESTINATION = "remote_engine"
MODELS_DESTINATION = "voice_model"
MCP_DESTINATION = "mcp"

#: Lines kept in Recent activity.
ACTIVITY_ROWS = 12
#: The stat tiles sit four across from this page width, else two.
FOUR_STATS_WIDTH = 700
#: Relative times ("connected 12 min") are redrawn this often while shown.
CLOCK_INTERVAL_MS = 30_000
#: Events that arrive together (a live preview's windows) redraw once.
COALESCE_MS = 40
#: The MCP server has no events; its status is read this often while shown.
MCP_POLL_MS = 1000


def default_mcp_server():
    """The app's MCP server (services/agent_mcp), or None in a build without it."""
    try:
        from services.agent_mcp.runtime import runtime
    except ImportError:
        return None
    return runtime


# ---- wording ----

def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _span(seconds: float) -> str:
    """``42 s``, ``12 min``, ``1 h 5 min``: how long, for people."""
    seconds = max(0.0, float(seconds or 0.0))
    if seconds < 60:
        return f"{seconds:.0f} s" if seconds >= 10 else f"{seconds:.1f} s"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def _quick(seconds: float) -> str:
    """``410 ms`` below a second, else as ``_span``: how long a decode took."""
    seconds = max(0.0, float(seconds or 0.0))
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else _span(seconds)


def _ago(at: float, now: float) -> str:
    if not at:
        return ""
    delta = max(0.0, now - at)
    if delta < 45:
        return "just now"
    if delta < 3600:
        return f"{max(1, round(delta / 60))} min ago"
    if delta < 86400:
        return f"{round(delta / 3600)} h ago"
    return datetime.fromtimestamp(at).strftime("%b %d").replace(" 0", " ")


def _clock(at: float) -> str:
    """``9:41 AM`` for a time today."""
    return datetime.fromtimestamp(at).strftime("%I:%M %p").lstrip("0")


def _iso_ago(iso: str, now: float) -> str:
    try:
        return _ago(datetime.fromisoformat(iso).timestamp(), now)
    except (TypeError, ValueError):
        return ""


def _paired_on(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%b %d, %Y").replace(" 0", " ")
    except (TypeError, ValueError):
        return ""


def _device_label(device: str) -> str:
    device = str(device or "")
    return device.upper() if device.lower() in ("cuda", "cpu") else device


def _speed(audio_s: float, host_s: float) -> str:
    """How many seconds of audio each second of decoding covered: ``38×``."""
    if host_s <= 0 or audio_s <= 0:
        return "—"
    ratio = audio_s / host_s
    return f"{ratio:.0f}×" if ratio >= 10 else f"{ratio:.1f}×"


def event_text(event: dict) -> str:
    """One Recent activity line."""
    name = event.get("name") or "A paired computer"
    kind = event.get("kind")
    if kind == "transcribed":
        return (f"{name} · {_span(event.get('audio_s', 0))} of audio, "
                f"back in {_quick(event.get('host_s', 0))}")
    if kind == "failed":
        reason = event.get("reason") or "unknown error"
        return f"{name} · couldn't transcribe: {reason}"
    if kind == "connected":
        return f"{name} connected"
    if kind == "disconnected":
        return f"{name} disconnected"
    if kind == "paired":
        return f"Paired {name}" + (" over Tailscale" if event.get("via") == "tailscale" else "")
    if kind == "switched":
        return f"{name} switched the engine to {event.get('label') or 'another model'}"
    return name


#: Recent activity dot colour per event kind.
_EVENT_TONES = {
    "transcribed": "success",
    "failed": "danger",
    "connected": "accent",
    "disconnected": "text-muted",
    "paired": "purple",
    "switched": "warning",
}


def group_clients(clients: List[dict]) -> List[dict]:
    """One entry per paired computer; a meeting and dictation each connect."""
    grouped: Dict[str, dict] = {}
    for client in clients:
        key = str(client.get("device_id") or client.get("name") or "")
        entry = grouped.get(key)
        if entry is None:
            grouped[key] = {
                "device_id": key,
                "name": str(client.get("name") or "A paired computer"),
                "address": str(client.get("address") or ""),
                "busy": bool(client.get("busy")),
                "since": float(client.get("since") or 0.0),
                "connections": 1,
            }
            continue
        entry["busy"] = entry["busy"] or bool(client.get("busy"))
        since = float(client.get("since") or 0.0)
        if since and (not entry["since"] or since < entry["since"]):
            entry["since"] = since
        entry["connections"] += 1
    return sorted(grouped.values(), key=lambda entry: entry["name"].lower())


def _clear_layout(layout) -> None:
    """Empty ``layout``, hiding each widget at once (deletion comes later)."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.deleteLater()


# ---- pieces ----

class HostBeacon(QWidget):
    """This computer as a small server, lit when it shares.

    ``set_state`` takes off / starting / on / error. While ``set_busy`` is
    on (a paired computer's audio is being decoded) waves run out from it;
    ``ripple()`` sends one ring out when a transcription finishes.
    """

    SIZE = 60
    _WAVE_PERIOD_MS = 1300
    _RIPPLE_MS = 900
    _LIGHT_MS = 520

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("hostBeacon")
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._state = "off"
        self._busy = False
        self._busy_since = 0
        self._ripples: List[int] = []
        self._lit_at: Optional[int] = None
        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    @property
    def state(self) -> str:
        return self._state

    @property
    def busy(self) -> bool:
        return self._busy

    def set_state(self, state: str) -> None:
        if state == "on" and self._state != "on" and self.isVisible():
            # Sharing just came on here: the light comes on with a ring.
            self._lit_at = self._clock.elapsed()
        if state != "on":
            self._lit_at = None
            self._ripples.clear()
            self._busy = False
        self._state = state
        self._sync()

    def set_busy(self, busy: bool) -> None:
        busy = bool(busy) and self._state == "on"
        if busy and not self._busy:
            self._busy_since = self._clock.elapsed()
        self._busy = busy
        self._sync()

    def ripple(self) -> None:
        if self._state == "on" and self.isVisible():
            self._ripples.append(self._clock.elapsed())
            self._sync()

    def _animating(self) -> bool:
        return self._busy or bool(self._ripples) or self._lit_at is not None

    def _sync(self) -> None:
        if self._animating() and self.isVisible():
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _tick(self) -> None:
        now = self._clock.elapsed()
        self._ripples = [start for start in self._ripples if now - start < self._RIPPLE_MS]
        if self._lit_at is not None and now - self._lit_at >= self._LIGHT_MS:
            self._lit_at = None
        self._sync()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def _light_color(self) -> QColor:
        if self._state == "on":
            return token_color("accent-cyan" if self._busy else "success")
        if self._state == "error":
            return token_color("warning")
        if self._state == "starting":
            return token_color("accent")
        return token_color("text-muted")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        now = self._clock.elapsed()
        center = QPointF(self.width() / 2, self.height() / 2)
        on = self._state == "on"

        # A soft halo behind a sharing host.
        if on:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(token_color("success", 34))
            painter.drawEllipse(center, 27, 27)

        for start in self._ripples:
            progress = min(1.0, (now - start) / self._RIPPLE_MS)
            pen = QPen(token_color("success", int(210 * (1 - progress))), 2.0)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            radius = 18 + 12 * (1 - (1 - progress) ** 2)
            painter.drawEllipse(center, radius, radius)

        if self._busy:
            # Waves out to the right while audio is being decoded.
            elapsed = now - self._busy_since
            for index in range(3):
                phase = ((elapsed / self._WAVE_PERIOD_MS) + index / 3) % 1.0
                radius = 10 + 18 * phase
                pen = QPen(token_color("accent-cyan", int(230 * sin(phase * pi))), 1.8)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                rect = QRectF(center.x() - radius, center.y() - radius, 2 * radius, 2 * radius)
                painter.drawArc(rect, int(-38 * 16), int(76 * 16))
                painter.drawArc(rect, int(142 * 16), int(76 * 16))

        # The server: two rack units.
        body = token_color("surface-hover")
        stroke = token_color("text-secondary" if not on else "text-soft")
        pen = QPen(stroke, 1.6)
        painter.setPen(pen)
        painter.setBrush(body)
        top = QRectF(center.x() - 13, center.y() - 12, 26, 10.5)
        bottom = QRectF(center.x() - 13, center.y() + 1.5, 26, 10.5)
        painter.drawRoundedRect(top, 3, 3)
        painter.drawRoundedRect(bottom, 3, 3)
        slot = QPen(token_color("text-muted"), 1.4)
        slot.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(slot)
        for rect in (top, bottom):
            y = rect.center().y()
            painter.drawLine(QPointF(rect.left() + 5, y), QPointF(rect.left() + 12, y))

        # The status light on each unit; the top one carries the state.
        painter.setPen(Qt.PenStyle.NoPen)
        light = self._light_color()
        top_light = QPointF(top.right() - 5.5, top.center().y())
        if self._lit_at is not None:
            progress = min(1.0, (now - self._lit_at) / self._LIGHT_MS)
            painter.setPen(QPen(token_color("success", int(220 * (1 - progress))), 1.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(top_light, 2 + 6 * progress, 2 + 6 * progress)
            painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(light)
        painter.drawEllipse(top_light, 2.2, 2.2)
        painter.setBrush(token_color("success", 150) if on else token_color("text-faint"))
        painter.drawEllipse(QPointF(bottom.right() - 5.5, bottom.center().y()), 2.0, 2.0)


class _StatTile(QFrame):
    """A number with what it counts, and a line under it."""

    def __init__(self, caption: str, tone: str, parent=None):
        super().__init__(parent)
        self.setObjectName("hostStat")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(2)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(7)
        dot = QLabel()
        dot.setObjectName("hostStatDot")
        dot.setProperty("tone", tone)
        dot.setFixedSize(8, 8)
        self.caption = QLabel(caption)
        self.caption.setObjectName("hostStatCaption")
        head.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.caption, 1)
        layout.addLayout(head)
        self.value = QLabel("—")
        self.value.setObjectName("hostStatValue")
        layout.addWidget(self.value)
        self.detail = ElidingLabel("")
        self.detail.setObjectName("hostStatDetail")
        layout.addWidget(self.detail)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set(self, value: str, detail: str = "") -> None:
        self.value.setText(value)
        self.detail.setText(detail)


class _Card(QFrame):
    """A titled section of the page."""

    def __init__(self, title: str, object_name: str, parent=None):
        super().__init__(parent)
        self.setObjectName("hostCard")
        self.setAccessibleName(title)
        self.setProperty("section", object_name)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(10)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self.title = QLabel(title)
        self.title.setObjectName("hostCardTitle")
        head.addWidget(self.title)
        self.badge = QLabel("")
        self.badge.setObjectName("hostCardBadge")
        self.badge.hide()
        head.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        self.head = head
        layout.addLayout(head)
        self.body = layout

    def set_badge(self, text: str) -> None:
        self.badge.setText(text)
        self.badge.setVisible(bool(text))


def _link(text: str, object_name: str) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName(object_name)
    button.setProperty("hostLink", True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setFlat(True)
    return button


def _muted(text: str = "", object_name: str = "hostMuted") -> WrappedLabel:
    label = WrappedLabel(text)
    label.setObjectName(object_name)
    return label


class _ClientRow(QFrame):
    """One connected computer: its link, name, and what it asked for."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("hostClientRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(12)
        self.glyph = RemoteLinkGlyph(mirrored=True)
        self.glyph.setToolTip("This computer on the left, the paired computer on the right")
        layout.addWidget(self.glyph, 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(1)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        self.name = ElidingLabel("")
        self.name.setObjectName("hostClientName")
        self.address = ElidingLabel("")
        self.address.setObjectName("hostClientAddress")
        top.addWidget(self.name)
        top.addWidget(self.address, 1)
        text.addLayout(top)
        self.stats = ElidingLabel("")
        self.stats.setObjectName("hostClientStats")
        text.addWidget(self.stats)
        layout.addLayout(text, 1)
        self.state = QLabel("")
        self.state.setObjectName("hostClientState")
        layout.addWidget(self.state, 0, Qt.AlignmentFlag.AlignVCenter)

    def show_client(self, client: dict, stats: Optional[dict], now: float) -> None:
        self.name.setText(client["name"])
        self.address.setText(client["address"])
        stats = stats or {}
        served = int(stats.get("transcriptions") or 0)
        self.glyph.set_link("connected", busy=client["busy"], replies=served)
        parts = []
        if served:
            parts.append(_count(served, "transcription", "transcriptions"))
            parts.append(f"{_span(stats.get('audio_s') or 0)} of audio")
        else:
            parts.append("Nothing transcribed yet")
        last = _ago(float(stats.get("last_at") or 0.0), now)
        if served and last:
            parts.append(f"last {last}")
        if client["connections"] > 1:
            parts.append(f"{client['connections']} connections")
        self.stats.setText(" · ".join(parts))
        if client["busy"]:
            self.state.setText("Transcribing…")
        else:
            since = client["since"]
            if since and now - since >= 60:
                self.state.setText(f"connected for {_span(now - since)}")
            else:
                self.state.setText("just connected" if since else "connected")
        if self.state.property("busy") != client["busy"]:
            set_style_property(self.state, "busy", client["busy"])


class _EventRow(QFrame):
    """One Recent activity line; a new one glows as it arrives."""

    def __init__(self, event: dict, fresh: bool, parent=None):
        super().__init__(parent)
        self.setObjectName("hostEventRow")
        self.serial = int(event.get("serial") or 0)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(10)
        when = QLabel(_clock(float(event.get("at") or time.time())))
        when.setObjectName("hostEventTime")
        when.setMinimumWidth(62)
        layout.addWidget(when, 0, Qt.AlignmentFlag.AlignTop)
        dot = QLabel()
        dot.setObjectName("hostStatDot")
        dot.setProperty("tone", _EVENT_TONES.get(event.get("kind"), "text-muted"))
        dot.setFixedSize(8, 8)
        layout.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.text = WrappedLabel(event_text(event))
        self.text.setObjectName("hostEventText")
        layout.addWidget(self.text, 1)
        self._glow = 0.0
        self._animation = None
        if fresh:
            animation = QVariantAnimation(self)
            animation.setStartValue(1.0)
            animation.setEndValue(0.0)
            animation.setDuration(1600)
            animation.valueChanged.connect(self._set_glow)
            self._animation = animation
            self._glow = 1.0
            animation.start()

    def _set_glow(self, value) -> None:
        self._glow = float(value)
        self.update()

    def paintEvent(self, event):
        if self._glow > 0:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(token_color("accent", int(46 * self._glow)))
            painter.drawRoundedRect(QRectF(self.rect()), 8, 8)
        super().paintEvent(event)


# ---- the page ----

class HostDashboard(QWidget):
    """The main window's page in Host Mode."""

    #: Serve another of this computer's ready models: (family, model).
    model_requested = pyqtSignal(str, str)
    #: Open Settings on a destination key.
    settings_requested = pyqtSignal(str)

    _service_event = pyqtSignal(str)
    _models_loaded = pyqtSignal(int, object)

    _STYLE = """
        QScrollArea#hostScroll, QWidget#hostContent {
            background-color: @bg;
            border: none;
        }
        QFrame#hostHero {
            background-color: @surface;
            border: 1px solid @border;
            border-radius: 16px;
        }
        QFrame#hostHero[state="on"] {
            background-color: rgba(@success-rgb, 0.08);
            border: 1px solid rgba(@success-rgb, 0.40);
        }
        QFrame#hostHero[state="error"] {
            background-color: @warning-tint;
            border: 1px solid @warning-tint-border;
        }
        QLabel#hostHeroTitle {
            color: @text-heading;
            font-size: 20px;
            font-weight: 700;
            background: transparent;
        }
        QLabel#hostHeroDetail {
            color: @text-body;
            font-size: 13px;
            background: transparent;
        }
        QLabel#hostHeroIdentity {
            color: @text-secondary;
            font-size: 12px;
            background: transparent;
        }
        QFrame#hostStat {
            background-color: @surface;
            border: 1px solid @border;
            border-radius: 12px;
        }
        QLabel#hostStatCaption {
            color: @text-secondary;
            font-size: 12px;
            font-weight: 600;
            background: transparent;
        }
        QLabel#hostStatValue {
            color: @text-heading;
            font-size: 24px;
            font-weight: 700;
            background: transparent;
        }
        QLabel#hostStatDetail {
            color: @text-secondary;
            font-size: 12px;
            background: transparent;
        }
        QLabel#hostStatDot { border-radius: 4px; background-color: @text-muted; }
        QLabel#hostStatDot[tone="success"] { background-color: @success; }
        QLabel#hostStatDot[tone="accent"] { background-color: @accent; }
        QLabel#hostStatDot[tone="accent-cyan"] { background-color: @accent-cyan; }
        QLabel#hostStatDot[tone="purple"] { background-color: @purple; }
        QLabel#hostStatDot[tone="warning"] { background-color: @warning; }
        QLabel#hostStatDot[tone="danger"] { background-color: @danger; }
        QFrame#hostCard {
            background-color: @surface;
            border: 1px solid @border;
            border-radius: 14px;
        }
        QLabel#hostCardTitle {
            color: @text-heading;
            font-size: 15px;
            font-weight: 700;
            background: transparent;
        }
        QLabel#hostCardBadge {
            color: @text-secondary;
            background-color: @surface-hover;
            border-radius: 9px;
            padding: 1px 8px;
            font-size: 12px;
            font-weight: 600;
        }
        QLabel#hostEngineName {
            color: @text-heading;
            font-size: 17px;
            font-weight: 600;
            background: transparent;
        }
        QLabel#hostMuted, QLabel#hostEventText {
            color: @text-secondary;
            font-size: 13px;
            background: transparent;
        }
        QLabel#hostEventText { color: @text-body; }
        QLabel#hostMcpStatus {
            color: @text-heading;
            font-size: 14px;
            font-weight: 600;
            background: transparent;
        }
        QLabel#hostWarning {
            color: @warning-text;
            font-size: 13px;
            background: transparent;
        }
        QLabel#hostFieldLabel {
            color: @text-secondary;
            font-size: 13px;
            font-weight: 600;
            background: transparent;
        }
        QFrame#hostClientRow, QFrame#hostDeviceRow {
            background-color: @surface-sunken;
            border: 1px solid @border-subtle;
            border-radius: 10px;
        }
        QLabel#hostClientName, QLabel#hostDeviceName {
            color: @text-heading;
            font-size: 14px;
            font-weight: 600;
            background: transparent;
        }
        QLabel#hostClientAddress, QLabel#hostClientStats, QLabel#hostDeviceDetail {
            color: @text-secondary;
            font-size: 12px;
            background: transparent;
        }
        QLabel#hostClientState {
            color: @text-secondary;
            font-size: 12px;
            background: transparent;
        }
        QLabel#hostClientState[busy="true"] {
            color: @accent-cyan;
            font-weight: 600;
        }
        QLabel#hostDeviceStatus {
            color: @text-secondary;
            font-size: 12px;
            background: transparent;
        }
        QLabel#hostDeviceStatus[connected="true"] {
            color: @success-text;
            font-weight: 600;
        }
        QFrame#hostEventRow { background: transparent; }
        QLabel#hostEventTime {
            color: @text-muted;
            font-size: 12px;
            background: transparent;
        }
        QFrame#hostPairingBox {
            background-color: rgba(@accent-rgb, 0.10);
            border: 1px solid @accent-tint-border;
            border-radius: 12px;
        }
        QLabel#hostPairingCode {
            color: @text-heading;
            font-size: 28px;
            font-weight: 700;
            letter-spacing: 4px;
            background: transparent;
        }
        /* The theme's plain button is the card's own colour. */
        QPushButton#hostCardButton {
            background-color: @surface-hover;
        }
        QPushButton#hostCardButton:hover {
            background-color: @surface-active;
        }
        QPushButton[hostLink="true"] {
            background: transparent;
            border: none;
            color: @accent;
            font-size: 13px;
            font-weight: 600;
            padding: 2px 0px;
            text-align: left;
        }
        QPushButton[hostLink="true"]:hover {
            color: @accent-soft;
            text-decoration: underline;
        }
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("hostDashboard")
        self.setStyleSheet(self._STYLE)
        self._service = None
        self._listener = lambda kind: self._service_event.emit(kind)
        self._service_event.connect(self._on_service_event)
        self._models_loaded.connect(self._on_models_loaded)
        #: How host models are listed off the UI thread (tests run it inline).
        self.run_in_background: Callable[[Callable[[], None]], None] = (
            lambda work: threading.Thread(target=work, name="host-dashboard-models",
                                          daemon=True).start()
        )
        self._pending: set = set()
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(COALESCE_MS)
        self._flush_timer.timeout.connect(self._flush)
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(CLOCK_INTERVAL_MS)
        self._clock_timer.timeout.connect(lambda: self._schedule("live"))
        self._countdown = QTimer(self)
        self._countdown.setInterval(1000)
        self._countdown.timeout.connect(self._tick_pairing)
        self._pairing_code = ""
        self._pairing_deadline = 0.0
        self._state: Optional[dict] = None
        self._engine: dict = {}
        self._engine_busy = False
        #: (label, family, model) of a switch asked for here, until it lands.
        self._switching: Optional[tuple] = None
        self._engine_error = ""
        self._models: Optional[list] = None
        self._models_token = 0
        self._clients: List[dict] = []
        self._activity = empty_snapshot()
        #: (since, serial) of the activity shown; sharing again starts a new list.
        self._seen_activity: Optional[tuple] = None
        self._seen_transcriptions: Optional[int] = None
        self._client_rows: Dict[str, _ClientRow] = {}
        self._device_rows: Dict[str, tuple] = {}
        self._stat_columns = 0
        #: The MCP server (``McpRuntime``); looked up when first shown.
        self._mcp = None
        self._mcp_bound = False
        self._mcp_notice = ""
        self._mcp_timer = QTimer(self)
        self._mcp_timer.setInterval(MCP_POLL_MS)
        self._mcp_timer.timeout.connect(self._refresh_mcp)
        self._build()
        self._show_unbound()

    # ---- construction ----

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("hostScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("hostContent")
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(20, 18, 20, 20)
        self._layout.setSpacing(14)
        self._build_hero()
        self._build_stats()
        self._build_engine()
        self._build_clients()
        self._build_devices()
        self._build_mcp()
        self._build_activity()
        self._layout.addStretch(1)
        self.scroll.setWidget(content)
        outer.addWidget(self.scroll)

    def _build_hero(self) -> None:
        self.hero = QFrame()
        self.hero.setObjectName("hostHero")
        self.hero.setProperty("state", "off")
        row = QHBoxLayout(self.hero)
        row.setContentsMargins(18, 16, 18, 16)
        row.setSpacing(16)
        self.beacon = HostBeacon()
        row.addWidget(self.beacon, 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(3)
        self.hero_title = WrappedLabel("")
        self.hero_title.setObjectName("hostHeroTitle")
        self.hero_detail = WrappedLabel("")
        self.hero_detail.setObjectName("hostHeroDetail")
        self.hero_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.hero_identity = WrappedLabel("")
        self.hero_identity.setObjectName("hostHeroIdentity")
        self.hero_identity.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.addWidget(self.hero_title)
        text.addWidget(self.hero_detail)
        text.addWidget(self.hero_identity)
        row.addLayout(text, 1)
        self.start_button = PrimaryButton("Start sharing")
        self.start_button.set_base_minimum_size(140, 40)
        self.start_button.clicked.connect(lambda: self._set_sharing(True))
        self.stop_button = Button("Stop sharing")
        self.stop_button.set_base_minimum_size(130, 40)
        self.stop_button.clicked.connect(lambda: self._set_sharing(False))
        row.addWidget(self.start_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.stop_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self._layout.addWidget(self.hero)

    def _build_stats(self) -> None:
        self.stats = QWidget()
        self.stats.setObjectName("hostStats")
        self._stats_grid = QGridLayout(self.stats)
        self._stats_grid.setContentsMargins(0, 0, 0, 0)
        self._stats_grid.setHorizontalSpacing(12)
        self._stats_grid.setVerticalSpacing(12)
        self.stat_connected = _StatTile("Connected", "success")
        self.stat_transcriptions = _StatTile("Transcriptions", "accent")
        self.stat_audio = _StatTile("Audio transcribed", "purple")
        self.stat_speed = _StatTile("Speed", "warning")
        self._stat_tiles = (self.stat_connected, self.stat_transcriptions,
                            self.stat_audio, self.stat_speed)
        self._arrange_stats(4)
        self._layout.addWidget(self.stats)

    def _arrange_stats(self, columns: int) -> None:
        if columns == self._stat_columns:
            return
        self._stat_columns = columns
        for tile in self._stat_tiles:
            self._stats_grid.removeWidget(tile)
        for index, tile in enumerate(self._stat_tiles):
            self._stats_grid.addWidget(tile, index // columns, index % columns)
        for column in range(4):
            self._stats_grid.setColumnStretch(column, 1 if column < columns else 0)

    def _build_engine(self) -> None:
        card = self.engine_card = _Card("Engine", "engine")
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(10)
        self.engine_dot = StatusDot(diameter=10)
        head.addWidget(self.engine_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.engine_name = QLabel("")
        self.engine_name.setObjectName("hostEngineName")
        head.addWidget(self.engine_name, 1)
        card.body.addLayout(head)
        self.engine_detail = _muted()
        card.body.addWidget(self.engine_detail)
        self.engine_status = _muted(object_name="hostWarning")
        card.body.addWidget(self.engine_status)
        picker = QHBoxLayout()
        picker.setContentsMargins(0, 4, 0, 0)
        picker.setSpacing(10)
        serve = QLabel("Serve")
        serve.setObjectName("hostFieldLabel")
        picker.addWidget(serve)
        self.model_combo = NoWheelComboBox()
        self.model_combo.setObjectName("hostModelCombo")
        self.model_combo.setMinimumHeight(36)
        self.model_combo.setMinimumWidth(220)
        self.model_combo.setToolTip("The model paired computers use, from the ones ready on this computer")
        self.model_combo.activated.connect(self._on_model_activated)
        picker.addWidget(self.model_combo, 1)
        card.body.addLayout(picker)
        self.engine_hint = _muted()
        card.body.addWidget(self.engine_hint)
        self.models_link = _link("Download models in Settings", "hostModelsLink")
        self.models_link.clicked.connect(lambda: self.settings_requested.emit(MODELS_DESTINATION))
        card.body.addWidget(self.models_link, 0, Qt.AlignmentFlag.AlignLeft)
        self._layout.addWidget(card)

    def _build_clients(self) -> None:
        card = self.clients_card = _Card("Connected now", "clients")
        self._clients_layout = QVBoxLayout()
        self._clients_layout.setContentsMargins(0, 0, 0, 0)
        self._clients_layout.setSpacing(8)
        card.body.addLayout(self._clients_layout)
        self.clients_empty = _muted("")
        card.body.addWidget(self.clients_empty)
        self._layout.addWidget(card)

    def _build_devices(self) -> None:
        card = self.devices_card = _Card("Paired computers", "devices")
        self.pairing_box = QFrame()
        self.pairing_box.setObjectName("hostPairingBox")
        box = QHBoxLayout(self.pairing_box)
        box.setContentsMargins(16, 12, 12, 12)
        box.setSpacing(14)
        self.pairing_code = QLabel("")
        self.pairing_code.setObjectName("hostPairingCode")
        self.pairing_code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.pairing_code)
        self.pairing_expiry = _muted()
        box.addWidget(self.pairing_expiry, 1)
        self.cancel_pairing_button = Button("Cancel")
        self.cancel_pairing_button.set_base_minimum_size(90, 36)
        self.cancel_pairing_button.clicked.connect(self._cancel_pairing)
        box.addWidget(self.cancel_pairing_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.pairing_box.hide()
        card.body.addWidget(self.pairing_box)
        pair_row = QHBoxLayout()
        pair_row.setContentsMargins(0, 0, 0, 0)
        pair_row.setSpacing(12)
        self.pair_button = PrimaryButton("Pair a computer")
        self.pair_button.set_base_minimum_size(150, 38)
        self.pair_button.clicked.connect(self._open_pairing)
        pair_row.addWidget(self.pair_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.pair_note = _muted()
        pair_row.addWidget(self.pair_note, 1)
        card.body.addLayout(pair_row)
        self._devices_layout = QVBoxLayout()
        self._devices_layout.setContentsMargins(0, 0, 0, 0)
        self._devices_layout.setSpacing(6)
        card.body.addLayout(self._devices_layout)
        self.manage_link = _link("Manage sharing and paired computers in Settings", "hostManageLink")
        self.manage_link.clicked.connect(lambda: self.settings_requested.emit(REMOTE_ENGINE_DESTINATION))
        card.body.addWidget(self.manage_link, 0, Qt.AlignmentFlag.AlignLeft)
        self._layout.addWidget(card)

    def _build_mcp(self) -> None:
        card = self.mcp_card = _Card("MCP", "mcp")
        self.mcp_intro = _muted()
        card.body.addWidget(self.mcp_intro)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.mcp_dot = StatusDot(diameter=10)
        row.addWidget(self.mcp_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.mcp_status = WrappedLabel("")
        self.mcp_status.setObjectName("hostMcpStatus")
        self.mcp_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.mcp_status, 1)
        self.mcp_button = Button("Turn on")
        self.mcp_button.setObjectName("hostCardButton")
        self.mcp_button.set_base_minimum_size(110, 36)
        self.mcp_button.clicked.connect(self._toggle_mcp)
        row.addWidget(self.mcp_button, 0, Qt.AlignmentFlag.AlignVCenter)
        card.body.addLayout(row)
        self.mcp_detail = _muted(object_name="hostWarning")
        card.body.addWidget(self.mcp_detail)
        actions = self._mcp_actions = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(10)
        self.mcp_link = _link("Connect an agent in Settings", "hostMcpLink")
        self.mcp_link.clicked.connect(lambda: self.settings_requested.emit(MCP_DESTINATION))
        actions.addWidget(self.mcp_link, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.mcp_copy_token = Button("Copy access token")
        self.mcp_copy_token.setAccessibleName("Copy MCP access token")
        self.mcp_copy_token.setObjectName("hostCardButton")
        self.mcp_copy_token.set_base_minimum_size(110, 36)
        self.mcp_copy_token.setEnabled(False)
        self.mcp_copy_token.clicked.connect(self._copy_mcp_access_token)
        actions.addWidget(self.mcp_copy_token, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.mcp_copy_prompt = Button("Copy agent install prompt")
        self.mcp_copy_prompt.setAccessibleName("Copy agent install prompt")
        self.mcp_copy_prompt.setObjectName("hostCardButton")
        self.mcp_copy_prompt.set_base_minimum_size(110, 36)
        self.mcp_copy_prompt.setEnabled(False)
        self.mcp_copy_prompt.clicked.connect(self._copy_mcp_agent_prompt)
        actions.addWidget(self.mcp_copy_prompt, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        actions.addStretch(1)
        card.body.addLayout(actions)
        self._mcp_copy_feedback_timer = QTimer(self)
        self._mcp_copy_feedback_timer.setSingleShot(True)
        self._mcp_copy_feedback_timer.setInterval(2000)
        self._mcp_copy_feedback_timer.timeout.connect(
            lambda: self.mcp_copy_prompt.setText("Copy agent install prompt")
        )
        self._mcp_token_copy_feedback_timer = QTimer(self)
        self._mcp_token_copy_feedback_timer.setSingleShot(True)
        self._mcp_token_copy_feedback_timer.setInterval(2000)
        self._mcp_token_copy_feedback_timer.timeout.connect(
            lambda: self.mcp_copy_token.setText("Copy access token")
        )
        card.hide()
        self._layout.addWidget(card)

    def _build_activity(self) -> None:
        card = self.activity_card = _Card("Recent activity", "activity")
        self._activity_layout = QVBoxLayout()
        self._activity_layout.setContentsMargins(0, 0, 0, 0)
        self._activity_layout.setSpacing(2)
        card.body.addLayout(self._activity_layout)
        self.activity_empty = _muted(
            "Nothing yet. Each request from a paired computer shows up here as it happens."
        )
        card.body.addWidget(self.activity_empty)
        self._layout.addWidget(card)

    # ---- binding and refresh ----

    def bind(self, service) -> None:
        """Attach the controller's ``RemoteEngineService`` (created after the window)."""
        if service is self._service:
            return
        if self._service is not None:
            self._service.remove_listener(self._listener)
        self._service = service
        if service is not None:
            service.add_listener(self._listener)
            listener = self._listener
            # The service outlives this page in tests that build many windows.
            self.destroyed.connect(lambda _obj=None: service.remove_listener(listener))
        # Read when shown: host_state() asks Tailscale, which a window that
        # never shows this page shouldn't do at every launch.
        self._schedule("all", "models")

    def bind_mcp(self, server) -> None:
        """Show the MCP server's card; None hides it (a build without MCP)."""
        self._mcp_bound = True
        self._mcp = server
        self.mcp_card.setVisible(server is not None)
        if server is not None and self.isVisible():
            self._mcp_timer.start()
        self._refresh_mcp()

    def refresh(self) -> None:
        """Read everything again now, shown or not."""
        self._pending.update(("all", "models"))
        self._flush(force=True)

    def set_engine_busy(self, busy: bool) -> None:
        """This computer's engine started or finished loading."""
        self._engine_busy = bool(busy)
        if not busy:
            self._switching = None
        self.engine_dot.set_busy(self._engine_busy)
        self._schedule("engine", "models")

    def set_device_info(self, _device_info: str, _ready=None) -> None:
        """The engine readout changed (a load finished, or failed)."""
        self._schedule("engine")

    def show_engine_error(self, message: str) -> None:
        """A switch asked for here couldn't start: say why under the picker."""
        self._switching = None
        self._engine_error = str(message or "")
        self._render_engine()

    def _on_service_event(self, kind: str) -> None:
        if kind in ("activity", "clients"):
            self._schedule("live")
        elif kind == "engine":
            self._schedule("engine", "models", "live")
        elif kind in ("models", "components"):
            self._schedule("models")
        else:
            self._schedule("all")

    def _schedule(self, *parts: str) -> None:
        self._pending.update(parts)
        if self.isVisible() and not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush(self, force: bool = False) -> None:
        if not force and not self.isVisible():
            return  # Kept pending until the page is shown.
        pending, self._pending = self._pending, set()
        if self._service is None:
            self._show_unbound()
            return
        try:
            if "all" in pending:
                self._refresh_host()
                pending.update(("engine", "live"))
            if "engine" in pending:
                self._refresh_engine()
            if "live" in pending:
                self._refresh_live()
            if "models" in pending:
                self._load_models()
            if "all" in pending:
                if not self._mcp_bound:
                    self.bind_mcp(default_mcp_server())
                else:
                    self._refresh_mcp()
        except Exception:
            logger.exception("Host dashboard refresh failed")

    def showEvent(self, event):
        super().showEvent(event)
        self._pending.update(("all", "models"))
        self._flush()
        self._clock_timer.start()
        if self._mcp is not None:
            self._mcp_timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._clock_timer.stop()
        self._countdown.stop()
        self._mcp_timer.stop()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange_stats(4 if event.size().width() >= FOUR_STATS_WIDTH else 2)
        self._arrange_mcp_actions(event.size().width())

    def _arrange_mcp_actions(self, width: int) -> None:
        outer = self._layout.contentsMargins()
        card = self.mcp_card.body.contentsMargins()
        available = (width - outer.left() - outer.right() - card.left() - card.right()
                     - self.scroll.verticalScrollBar().sizeHint().width())
        needed = (self.mcp_link.sizeHint().width() + self._mcp_actions.spacing() * 2
                  + sum(max(button.minimumWidth(), button.sizeHint().width(),
                            button.fontMetrics().horizontalAdvance(label) + 40)
                        for button, label in (
                            (self.mcp_copy_token, "Copy access token"),
                            (self.mcp_copy_prompt, "Copy agent install prompt"),
                        )))
        self._mcp_actions.setDirection(
            QBoxLayout.Direction.LeftToRight if available >= needed
            else QBoxLayout.Direction.TopToBottom
        )

    def _show_unbound(self) -> None:
        set_style_property(self.hero, "state", "off")
        self.beacon.set_state("off")
        self.hero_title.setText("Host Mode isn't available here")
        self.hero_detail.setText("The remote engine hasn't started in this window.")
        self.hero_identity.hide()
        self.start_button.hide()
        self.stop_button.hide()
        for card in (self.engine_card, self.clients_card, self.devices_card, self.activity_card):
            card.setEnabled(False)

    # ---- sharing ----

    def _refresh_host(self) -> None:
        state = self._service.host_state()
        self._state = state
        for card in (self.engine_card, self.clients_card, self.devices_card, self.activity_card):
            card.setEnabled(True)
        self._render_hero(state)
        self._render_pairing(state)
        self._render_devices(state)

    def _where(self, state: dict) -> str:
        host = str(state.get("host_name") or "This computer")
        address = state.get("address")
        lines = []
        if address:
            where = f"{host} at {protocol.format_address(address, state.get('port'))}"
            if state.get("address_kind") == "vpn":
                where += " (a VPN address; computers on your network may not reach it)"
            lines.append(where)
        else:
            lines.append(f"{host} on port {state.get('port')}")
        tailnet = state.get("tailscale")
        if tailnet is not None and getattr(tailnet, "running", False) and getattr(tailnet, "address", ""):
            lines.append(f"Tailscale: {tailnet.name or tailnet.address} at {tailnet.address}")
        return "\n".join(lines)

    def _render_hero(self, state: dict) -> None:
        running = bool(state.get("running"))
        enabled = bool(state.get("enabled"))
        error = str(state.get("error") or "")
        engine = state.get("engine") or {}
        identity = ""
        if error:
            tone, title, detail = "error", "Sharing couldn't start", error
        elif running:
            tone = "on"
            if engine.get("available") and engine.get("label"):
                title = f"Sharing {engine['label']}"
            else:
                title = "Sharing, but no engine is ready"
            detail = self._where(state)
            if state.get("fingerprint"):
                identity = f"Identity {protocol.short_fingerprint(state['fingerprint'])}"
        elif enabled:
            tone, title, detail = "starting", "Starting to share…", ""
        else:
            tone = "off"
            title = "Sharing is off"
            detail = ("Start sharing so computers you pair can dictate and record "
                      "meetings with this computer's engine.")
        set_style_property(self.hero, "state", tone)
        self.beacon.set_state(tone)
        self.hero_title.setText(title)
        self.hero_detail.setText(detail)
        self.hero_detail.setVisible(bool(detail))
        self.hero_identity.setText(identity)
        self.hero_identity.setToolTip(
            "A computer that pairs with this one shows the same identity." if identity else ""
        )
        self.hero_identity.setVisible(bool(identity))
        self.start_button.setVisible(not enabled)
        self.stop_button.setVisible(enabled)
        self.stop_button.setText("Turn off sharing" if error else "Stop sharing")

    def _set_sharing(self, enabled: bool) -> None:
        if self._service is None:
            return
        if not enabled and self._clients:
            names = sorted({c["name"] for c in group_clients(self._clients)})
            who = names[0] if len(names) == 1 else f"{len(names)} computers"
            box = QMessageBox(self.window())
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Stop sharing")
            box.setText(f"{who} {'is' if len(names) == 1 else 'are'} connected.")
            box.setInformativeText("Stopping cuts them off until you share again.")
            stop = box.addButton("Stop sharing", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is not stop:
                return
        self._service.set_host_enabled(enabled)
        self.refresh()

    # ---- pairing and paired computers ----

    def _render_pairing(self, state: dict) -> None:
        running = bool(state.get("running"))
        pairing = state.get("pairing") if running else None
        if pairing:
            code, left = pairing
            self._pairing_code = str(code)
            self._pairing_deadline = time.monotonic() + float(left)
        else:
            self._pairing_code = ""
        self._show_pairing()
        tailnet = state.get("tailscale")
        owner = getattr(tailnet, "owner", "") if tailnet is not None and getattr(tailnet, "running", False) else ""
        if not running:
            note = "Start sharing to pair a computer."
        elif owner and state.get("tailscale_trust"):
            note = (f"Computers signed in to Tailscale as {owner} can pair by picking "
                    "this one from their tailnet, without a code.")
        else:
            note = "On the other computer, open Settings → Remote engine and enter the code shown here."
        self.pair_note.setText(note)
        self.pair_button.setEnabled(running)
        # A code only works while sharing; the note says so instead.
        self.pair_button.setVisible(running and not self._pairing_code)

    def _show_pairing(self) -> None:
        left = self._pairing_deadline - time.monotonic()
        if not self._pairing_code or left <= 0:
            had_code = bool(self._pairing_code)
            self._pairing_code = ""
            self.pairing_box.hide()
            self.pair_button.show()
            self._countdown.stop()
            if had_code:
                self._schedule("all")  # Expired: the host closed it too.
            return
        code = self._pairing_code
        self.pairing_code.setText(f"{code[:3]} {code[3:]}")
        minutes, seconds = divmod(int(left), 60)
        self.pairing_expiry.setText(
            f"Enter this code on the other computer. Expires in {minutes}:{seconds:02d}."
        )
        self.pairing_box.show()
        self.pair_button.hide()
        if not self._countdown.isActive():
            self._countdown.start()

    def _tick_pairing(self) -> None:
        self._show_pairing()

    def _open_pairing(self) -> None:
        if self._service is not None:
            self._service.open_pairing()
            self.refresh()

    def _cancel_pairing(self) -> None:
        if self._service is not None:
            self._service.cancel_pairing()
            self.refresh()

    def _device_records(self, device_id: str) -> dict:
        try:
            return self._service.records_summary(device_id) or {}
        except Exception:
            logger.debug("Could not count a paired computer's records", exc_info=True)
            return {}

    def _render_devices(self, state: dict) -> None:
        _clear_layout(self._devices_layout)
        self._device_rows = {}
        devices = list(state.get("devices") or [])
        self.devices_card.set_badge(str(len(devices)) if devices else "")
        if not devices:
            empty = _muted("No computers are paired yet.")
            self._devices_layout.addWidget(empty)
            return
        for device in devices:
            device_id = str(device.get("id") or "")
            row = QFrame()
            row.setObjectName("hostDeviceRow")
            layout = QHBoxLayout(row)
            layout.setContentsMargins(12, 8, 12, 8)
            layout.setSpacing(10)
            text = QVBoxLayout()
            text.setContentsMargins(0, 0, 0, 0)
            text.setSpacing(1)
            name = ElidingLabel(str(device.get("name") or "Unnamed computer"))
            name.setObjectName("hostDeviceName")
            text.addWidget(name)
            details = []
            paired = _paired_on(device.get("paired_at", ""))
            if paired:
                details.append(f"Paired {paired}" + (" over Tailscale" if device.get("via") == "tailscale" else ""))
            # Counted even while keeping records is off: what's kept stays.
            stored = self._stored_phrase(self._device_records(device_id)) if device_id else ""
            if stored:
                details.append(f"keeps {stored} here")
            detail = ElidingLabel(" · ".join(details))
            detail.setObjectName("hostDeviceDetail")
            detail.setVisible(bool(details))
            text.addWidget(detail)
            layout.addLayout(text, 1)
            status = QLabel("")
            status.setObjectName("hostDeviceStatus")
            layout.addWidget(status, 0, Qt.AlignmentFlag.AlignVCenter)
            self._devices_layout.addWidget(row)
            self._device_rows[device_id] = (row, status, device)
        self._update_device_status()

    @staticmethod
    def _stored_phrase(stored: dict) -> str:
        from ui_qt.dialogs.settings_remote import _stored_phrase

        return _stored_phrase(stored)

    def _update_device_status(self) -> None:
        connected = {str(c.get("device_id") or "") for c in self._clients}
        now = time.time()
        for device_id, (_row, status, device) in self._device_rows.items():
            if device_id in connected:
                status.setText("Connected")
                set_style_property(status, "connected", True)
                continue
            set_style_property(status, "connected", False)
            seen = _iso_ago(device.get("last_seen", ""), now)
            status.setText(f"Seen {seen}" if seen else "")

    # ---- engine ----

    def _refresh_engine(self) -> None:
        self._engine = self._service.engine_state() or {}
        if self._switching is not None:
            _label, family, model = self._switching
            if (self._engine.get("family"), self._engine.get("model")) == (family, model) \
                    and self._engine.get("available") and not self._engine_busy:
                self._switching = None
        self._render_engine()

    def _render_engine(self) -> None:
        engine = self._engine
        label = str(engine.get("label") or "")
        available = bool(engine.get("available"))
        self.engine_name.setText(label or "No local engine selected")
        if available:
            self.engine_dot.set_status(EngineStatus.READY)
        else:
            self.engine_dot.set_status(EngineStatus.ATTENTION if label or engine else EngineStatus.UNKNOWN)
        self.engine_dot.set_busy(self._engine_busy)
        details = []
        if engine.get("device"):
            device = f"On {_device_label(engine['device'])}"
            if engine.get("compute_type"):
                device += f" ({engine['compute_type']})"
            details.append(device)
        if label:
            details.append("live preview supported" if engine.get("streaming") else "no live preview")
        if self._switching is not None:
            details = [f"Switching to {self._switching[0]}…"]
        elif self._engine_busy:
            details = ["Loading…"]
        self.engine_detail.setText(" · ".join(details))
        self.engine_detail.setVisible(bool(details))
        status = ""
        if self._engine_error:
            status = self._engine_error
        elif not available and not self._engine_busy and self._switching is None:
            status = str(engine.get("status") or "")
        self.engine_status.setText(status)
        self.engine_status.setVisible(bool(status))
        self._sync_model_combo()

    def _load_models(self) -> None:
        service = self._service
        if service is None:
            return
        self._models_token += 1
        token = self._models_token

        def work():
            try:
                models = service.host_models()
            except Exception:
                logger.debug("Could not list this computer's ready models", exc_info=True)
                models = []
            try:
                self._models_loaded.emit(token, list(models or []))
            except RuntimeError:
                pass  # The page closed while the list was read.

        self.run_in_background(work)

    def _on_models_loaded(self, token: int, models) -> None:
        if token != self._models_token:
            return  # A newer listing is on its way.
        self._models = list(models)
        self._sync_model_combo()

    def _sync_model_combo(self) -> None:
        combo = self.model_combo
        engine = self._engine
        current = (engine.get("family"), engine.get("model"))
        models = self._models
        blocked = combo.blockSignals(True)
        combo.clear()
        selected = -1
        for entry in models or []:
            combo.addItem(str(entry.get("label") or entry.get("model")),
                          (entry.get("family"), entry.get("model")))
            if (entry.get("family"), entry.get("model")) == current:
                selected = combo.count() - 1
        if selected < 0:
            if models is None:
                placeholder = "Looking for models…"
            elif not models:
                placeholder = "No models are ready"
            else:
                placeholder = "Choose a model to serve"
            combo.insertItem(0, placeholder, None)
            selected = 0
        combo.setCurrentIndex(selected)
        combo.blockSignals(blocked)
        ready = bool(models)
        combo.setEnabled(ready and not self._engine_busy and self._switching is None)
        if models is not None and not models:
            self.engine_hint.setText("Download a speech model and its runtime here first.")
        elif ready:
            self.engine_hint.setText(
                "Paired computers use this model, and can switch to any of these themselves."
            )
        else:
            self.engine_hint.setText("")
        self.engine_hint.setVisible(bool(self.engine_hint.text()))
        self.models_link.setVisible(models is not None and not models)

    def _on_model_activated(self, index: int) -> None:
        data = self.model_combo.itemData(index)
        if not data:
            return
        family, model = data
        engine = self._engine
        if (engine.get("family"), engine.get("model")) == (family, model) and engine.get("available"):
            return
        self._switching = (self.model_combo.itemText(index), family, model)
        self._engine_error = ""
        self._render_engine()
        self.model_requested.emit(family, model)

    # ---- MCP: agents on this computer searching what it keeps ----

    def _refresh_mcp(self) -> None:
        server = self._mcp
        if server is None:
            self.mcp_copy_prompt.setEnabled(False)
            self.mcp_copy_token.setEnabled(False)
            return
        try:
            status = server.status()
        except Exception:
            logger.debug("Could not read the MCP server's status", exc_info=True)
            self.mcp_copy_prompt.setEnabled(False)
            self.mcp_copy_token.setEnabled(False)
            return
        state = getattr(status, "state", "stopped")
        remote_url = getattr(status, "remote_url", "")
        self.mcp_copy_prompt.setEnabled(state == "running")
        self.mcp_copy_token.setEnabled(state == "running")
        intro = (("Agents on this computer and over Tailscale" if remote_url else "Agents on this computer") + ", such as Claude Code or Cursor, can search "
                 "the dictations and meetings saved here. Choose permissions for title "
                 "and settings changes in Settings → MCP.")
        if (self._state or {}).get("keep_records"):
            intro += " That includes the records paired computers keep here."
        self.mcp_intro.setText(intro)
        detail = self._mcp_notice
        if state == "running":
            text, dot = f"Running at {remote_url or status.url}", EngineStatus.READY
            if not remote_url:
                detail = detail or "For an agent on another computer, enable Allow agents over Tailscale in Settings → MCP."
        elif state == "starting":
            text, dot = "Starting…", EngineStatus.UNKNOWN
        elif state == "stopping":
            text, dot = "Stopping…", EngineStatus.UNKNOWN
        elif state == "error":
            text, dot = "Couldn't start", EngineStatus.ATTENTION
            detail = detail or str(getattr(status, "message", "") or "")
        else:
            text, dot = "Off", EngineStatus.UNKNOWN
        self.mcp_status.setText(text)
        self.mcp_dot.set_status(dot)
        self.mcp_dot.set_busy(state in ("starting", "stopping"))
        self.mcp_detail.setText(detail)
        self.mcp_detail.setVisible(bool(detail))
        self.mcp_button.setText({"error": "Try again", "stopped": "Turn on"}.get(state, "Turn off"))
        self.mcp_button.setEnabled(state != "stopping")

    def _copy_mcp_access_token(self) -> None:
        server = self._mcp
        if server is None:
            return
        try:
            status = server.status()
            token = server.token() if status.state == "running" else ""
        except Exception:
            logger.debug("Could not read the MCP access token", exc_info=True)
            self.mcp_copy_token.setEnabled(False)
            return
        if not token:
            self._refresh_mcp()
            return
        QApplication.clipboard().setText(token)
        self.mcp_copy_token.setText("Copied")
        self._mcp_token_copy_feedback_timer.start()

    def _copy_mcp_agent_prompt(self) -> None:
        server = self._mcp
        if server is None:
            return
        try:
            status = server.status()
        except Exception:
            logger.debug("Could not read the MCP server's status", exc_info=True)
            self.mcp_copy_prompt.setEnabled(False)
            return
        if status.state != "running":
            self._refresh_mcp()
            return
        from services.agent_mcp.setup import agent_prompt

        QApplication.clipboard().setText(agent_prompt(getattr(status, "remote_url", "") or status.url))
        self.mcp_copy_prompt.setText("Copied")
        self._mcp_copy_feedback_timer.start()

    def _toggle_mcp(self) -> None:
        server = self._mcp
        if server is None:
            return
        turn_on = server.status().state in ("stopped", "error")
        try:
            # The same setting Settings → MCP saves; the server starts with
            # the app while it is on.
            settings_manager.save_setting(SettingsKey.MCP_ENABLED, turn_on)
        except Exception:
            logger.warning("Could not save the MCP setting", exc_info=True)
            self._mcp_notice = "Couldn't save the MCP setting. Try again."
        else:
            self._mcp_notice = ""
            if turn_on:
                # Starts on the saved port, as at launch.
                server.restore(settings_manager)
            else:
                server.stop()
        self._refresh_mcp()

    # ---- live: who is connected, and what they asked for ----

    def _refresh_live(self) -> None:
        clients = list(self._service.connected_clients() or [])
        activity = self._service.host_activity(ACTIVITY_ROWS) or empty_snapshot()
        self._clients = clients
        self._activity = activity
        grouped = group_clients(clients)
        now = time.time()
        running = bool((self._state or {}).get("running"))
        self._render_stats(grouped, activity, running)
        self._render_clients(grouped, activity, now, running)
        self._render_activity(activity)
        self._update_device_status()
        self.beacon.set_busy(any(c["busy"] for c in grouped))
        transcriptions = int(activity.get("transcriptions") or 0)
        if self._seen_transcriptions is not None and transcriptions > self._seen_transcriptions:
            self.beacon.ripple()
        self._seen_transcriptions = transcriptions

    def _render_stats(self, grouped: List[dict], activity: dict, running: bool) -> None:
        count = len(grouped)
        if count:
            names = ", ".join(c["name"] for c in grouped)
            self.stat_connected.set(str(count), names)
        else:
            self.stat_connected.set("0", "none right now" if running else "sharing is off")
        transcriptions = int(activity.get("transcriptions") or 0)
        since = float(activity.get("since") or 0.0)
        detail = f"since {_clock(since)}" if since and running else ("last session" if transcriptions else "")
        errors = int(activity.get("errors") or 0)
        if errors:
            detail = f"{detail} · {errors} failed" if detail else f"{errors} failed"
        self.stat_transcriptions.set(str(transcriptions), detail)
        audio_s = float(activity.get("audio_s") or 0.0)
        host_s = float(activity.get("host_s") or 0.0)
        if transcriptions:
            self.stat_audio.set(_span(audio_s), f"about {_span(audio_s / transcriptions)} each")
            average = host_s / transcriptions
            self.stat_speed.set(_speed(audio_s, host_s), f"{_quick(average)} per request")
            self.stat_speed.value.setToolTip(
                f"Each second of decoding here covered {_speed(audio_s, host_s).rstrip('×')} "
                "seconds of audio."
            )
        else:
            self.stat_audio.set("0 s", "")
            self.stat_speed.set("—", "no requests yet")
            self.stat_speed.value.setToolTip("")

    def _render_clients(self, grouped: List[dict], activity: dict, now: float, running: bool) -> None:
        devices = activity.get("devices") or {}
        seen = set()
        for index, client in enumerate(grouped):
            key = client["device_id"]
            seen.add(key)
            row = self._client_rows.get(key)
            if row is None:
                row = self._client_rows[key] = _ClientRow()
            if self._clients_layout.indexOf(row) != index:
                self._clients_layout.insertWidget(index, row)
            row.show_client(client, devices.get(key), now)
        for key in list(self._client_rows):
            if key not in seen:
                row = self._client_rows.pop(key)
                self._clients_layout.removeWidget(row)
                row.hide()
                row.deleteLater()
        self.clients_card.set_badge(str(len(grouped)) if grouped else "")
        if grouped:
            self.clients_empty.hide()
        else:
            self.clients_empty.setText(
                "No computers are connected right now. Paired computers connect "
                "when they dictate or record a meeting with this engine."
                if running else "Nobody can connect while sharing is off."
            )
            self.clients_empty.show()

    def _render_activity(self, activity: dict) -> None:
        key = (float(activity.get("since") or 0.0), int(activity.get("serial") or 0))
        if key == self._seen_activity:
            return
        first = self._seen_activity is None
        last_seen = self._seen_activity[1] if self._seen_activity else 0
        self._seen_activity = key
        _clear_layout(self._activity_layout)
        events = list(activity.get("events") or [])[:ACTIVITY_ROWS]
        for event in events:
            fresh = not first and int(event.get("serial") or 0) > last_seen
            self._activity_layout.addWidget(_EventRow(event, fresh))
        self.activity_empty.setVisible(not events)
