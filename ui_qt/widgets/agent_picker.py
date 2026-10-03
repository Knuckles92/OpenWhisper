"""Choose who runs Meeting Mode's AI insights: an installed agent or OpenWhisper.

Settings → Meeting Mode → Intelligence opens with one tile per coding agent
OpenWhisper can drive (Claude Code, Codex, OpenCode) and one for its built-in
engine. The tiles report what a scan of this computer found, and the model row
under them lists the chosen agent's own models.

Motion follows the scan only. Tiles shimmer from the moment the scan starts
until it returns, and when a fresh result arrives the agents that were found
light up one after another: the mark warms to the agent's colour and a Found
pill draws its check. A cached result is shown as it is, without replaying
anything, so reopening Settings is quiet.
"""
from __future__ import annotations

import logging
import math
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Final, Iterable, List, Optional

from PyQt6.QtCore import (
    QElapsedTimer,
    QEvent,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from config import bundle_root
from services import installed_agents
from services.installed_agents import (
    AGENT_ORDER,
    AGENT_SPECS,
    AgentModel,
    InstalledAgent,
    sign_in_hint,
)
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.icons import design_icon
from ui_qt.utils.palette import current_palette, token_color
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets.buttons import Button
from ui_qt.widgets.no_wheel import ElidingComboBox
from ui_qt.widgets.searchable_combo import SearchableComboBox
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)

#: The picker's id for OpenWhisper's own engine (Pi or Direct).
BUILTIN: Final[str] = "openwhisper"

# Tile tones: what the scan says about one tile.
IDLE: Final[str] = "idle"
SCANNING: Final[str] = "scanning"
FOUND: Final[str] = "found"
MISSING: Final[str] = "missing"
WARNING: Final[str] = "warning"
BUILTIN_TONE: Final[str] = "builtin"

#: Gap between one tile lighting up and the next.
STAGGER_MS: Final[int] = 100
#: One tile's arrival: mark warms, pill appears, check draws.
ARRIVAL_MS: Final[int] = 380
_SHIMMER_FADE_MS: Final[int] = 160
_HOVER_MS: Final[int] = 140
_LIFT_MS: Final[int] = 220
_SHIMMER_PERIOD_MS: Final[float] = 1300.0
#: Where the band starts, so a scan shorter than one sweep still shows it.
_SHIMMER_START: Final[float] = 0.3
#: Each tile's shimmer trails its left neighbour's, so the row ripples.
_SHIMMER_OFFSET_MS: Final[float] = 140.0
_TICK_MS: Final[int] = 16
#: More model choices than this get a type-to-filter combo.
FILTER_THRESHOLD: Final[int] = 20

_BUILTIN_DETAIL = "Your API key · the chat model below"
_CHECKING_DETAIL = "Checking…"


# ---- blocking work, patched by tests ----

def scan_installed_agents(refresh: bool = False) -> Dict[str, Optional[InstalledAgent]]:
    """Scan this computer for agents. Blocking; the picker calls it off the UI thread."""
    return installed_agents.scan_agents(refresh=refresh)


def cached_agents() -> Optional[Dict[str, Optional[InstalledAgent]]]:
    """The last scan, or None before the first. Never blocks."""
    return installed_agents.cached_scan()


def list_agent_models(agent: InstalledAgent) -> List[AgentModel]:
    """``agent``'s models, its own default first. Blocking for OpenCode."""
    return installed_agents.list_models(agent)


# ---- what a tile says ----

@dataclass(frozen=True)
class TileState:
    """What one tile shows for a scan result.

    Attributes:
        tone: One of the tile tones (``FOUND``, ``MISSING``...).
        pill: Status pill text.
        detail: Line under the name ("2.1.281 · Claude Team", a problem).
        selectable: The tile can be chosen to run AI insights.
        installed: The agent is on this computer, so its mark is in colour.
    """

    tone: str
    pill: str
    detail: str
    selectable: bool
    installed: bool


IDLE_STATE: Final[TileState] = TileState(IDLE, "", "", False, False)
BUILTIN_STATE: Final[TileState] = TileState(
    BUILTIN_TONE, "Built in", _BUILTIN_DETAIL, True, True
)


def command_text(text: str) -> str:
    """``Run `claude update`.`` with the command in quotes instead of backticks."""
    return re.sub(r"`([^`]+)`", "“\\1”", text or "")


def describe_agent(agent_id: str, agent: Optional[InstalledAgent]) -> TileState:
    """The tile for ``agent_id`` given what the scan found (None: not installed)."""
    if agent is None:
        return TileState(MISSING, "Not installed", "", False, False)
    if agent.problem:
        return TileState(WARNING, "Update needed", command_text(agent.problem), False, True)
    if agent.signed_in is False:
        return TileState(
            WARNING, "Sign in needed", command_text(sign_in_hint(agent_id)), False, True
        )
    detail = " · ".join(part for part in (agent.version, agent.account) if part)
    return TileState(FOUND, "Found", detail, True, True)


def found_count_text(agents: Dict[str, Optional[InstalledAgent]]) -> str:
    """The line under the heading once a scan is in."""
    found = sum(1 for agent_id in AGENT_ORDER if agents.get(agent_id) is not None)
    if not found:
        return (
            "No coding agents found on this computer. OpenWhisper's built-in "
            "engine works without one."
        )
    noun = "agent" if found == 1 else "agents"
    return f"Found {found} coding {noun} on this computer."


def usage_caption(agent: InstalledAgent) -> str:
    """What running meeting passes through ``agent`` means, in three lines."""
    name = agent.spec.name
    account = agent.account
    if not account:
        # OpenCode reports its providers only once it is running.
        lines = [
            f"Runs through the providers you signed in to in {name}.",
            "Each meeting pass counts toward their usage.",
        ]
    else:
        lines = [f"Runs through your {name} sign-in ({account})."]
        if account == "API key":
            lines.append("Each meeting pass is billed to that key.")
        elif account.startswith("Claude") or account == "ChatGPT":
            lines.append("Each meeting pass counts toward that plan's usage.")
        else:
            lines.append("Each meeting pass counts toward that account's usage.")
    lines.append(
        f"{name} gets OpenWhisper's meeting tools and nothing else: no files, "
        "no shell."
    )
    return " ".join(lines)


def blocked_notice(agent_id: str, agent: Optional[InstalledAgent]) -> str:
    """Why the chosen agent cannot run AI insights, or "" when it can."""
    name = AGENT_SPECS[agent_id].name
    if agent is None:
        until, fix = "it is installed", "Install it"
    elif agent.problem:
        until, fix = "it is updated", "Update it"
    elif agent.signed_in is False:
        until, fix = "it is signed in", "Sign in"
    else:
        return ""
    return (
        f"AI insights can't start with {name} until {until}. {fix} and choose "
        "Look again, or choose another option above."
    )


# ---- painting helpers ----

def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _ease_out(t: float) -> float:
    t = _clamp(t)
    return 1.0 - (1.0 - t) ** 3


def _ease_in_out(t: float) -> float:
    t = _clamp(t)
    return 4.0 * t ** 3 if t < 0.5 else 1.0 - (-2.0 * t + 2.0) ** 3 / 2.0


def _mix(start: QColor, end: QColor, t: float) -> QColor:
    t = _clamp(t)
    return QColor(
        round(start.red() + (end.red() - start.red()) * t),
        round(start.green() + (end.green() - start.green()) * t),
        round(start.blue() + (end.blue() - start.blue()) * t),
        round(start.alpha() + (end.alpha() - start.alpha()) * t),
    )


def _alpha(color: QColor, alpha: float) -> QColor:
    copy = QColor(color)
    copy.setAlphaF(_clamp(alpha))
    return copy


def _dark() -> bool:
    return current_palette().is_dark


def _ring_color(accent: QColor) -> QColor:
    """The accent, nudged so a 2 px ring holds against the card."""
    return accent.lighter(118) if _dark() else accent.darker(112)


def _app_mark() -> Optional[QPixmap]:
    path = Path(bundle_root()) / "ui_qt" / "assets" / "openwhisper.png"
    pixmap = QPixmap(str(path))
    return None if pixmap.isNull() else pixmap


class AgentMark(QWidget):
    """The rounded monogram square; warms from muted to the agent's colour."""

    def __init__(self, monogram: str, accent: str, pixmap: Optional[QPixmap] = None,
                 parent=None):
        super().__init__(parent)
        self.setObjectName("agentMark")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._monogram = monogram
        self._accent = QColor(accent)
        self._pixmap = pixmap
        self._warmth = 0.0
        self._missing = 0.0

    @property
    def warmth(self) -> float:
        return self._warmth

    @property
    def missing(self) -> float:
        return self._missing

    def set_warmth(self, value: float) -> None:
        value = _clamp(value)
        if value != self._warmth:
            self._warmth = value
            self.update()

    def set_missing(self, value: float) -> None:
        """Blend toward the empty-slot look (dashed outline), 0 to 1."""
        value = _clamp(value)
        if value != self._missing:
            self._missing = value
            self.update()

    def side(self) -> int:
        return max(24, round(QFontMetricsF(self.font()).height() * 2.0))

    def sizeHint(self) -> QSize:
        side = self.side()
        return QSize(side, side)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self.updateGeometry()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = float(min(self.width(), self.height()))
        rect = QRectF(0.5, 0.5, side - 1.0, side - 1.0)
        radius = side * 0.28
        if self._pixmap is not None:
            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)
            painter.setClipPath(path)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(rect.toRect(), self._pixmap)
            return
        warmth = self._warmth
        missing = self._missing
        fill = _mix(token_color("slate-raised"), self._accent, warmth)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_mix(fill, token_color("slate-field"), missing))
        painter.drawRoundedRect(rect, radius, radius)
        if missing > 0.0:
            # An empty slot: the agent could go here once it is installed.
            pen = QPen(_alpha(token_color("slate-border-strong"), missing), 1.0)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, radius, radius)
        if warmth > 0.0:
            # A soft top light, so a lit mark reads as a glossy tile.
            shine = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            shine.setColorAt(0.0, QColor(255, 255, 255, round(60 * warmth)))
            shine.setColorAt(0.55, QColor(255, 255, 255, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(shine)
            painter.drawRoundedRect(rect, radius, radius)
        ink = _mix(token_color("slate-text-3"), QColor("#ffffff"), warmth)
        ink = _mix(ink, token_color("slate-text-4"), missing)
        font = QFont(self.font())
        font.setPixelSize(max(9, round(side * 0.38)))
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(ink)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._monogram)


class StatusPill(QWidget):
    """A small rounded status label; the Found pill draws its check in."""

    PAD = 8.0
    GAP = 4.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("agentStatusPill")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._tone = IDLE
        self._text = ""
        self._appear = 1.0
        self._check = 1.0

    @property
    def tone(self) -> str:
        return self._tone

    def text(self) -> str:
        return self._text

    def set_state(self, tone: str, text: str) -> None:
        if (tone, text) == (self._tone, self._text):
            return
        self._tone, self._text = tone, text
        self.setAccessibleName(text)
        self.updateGeometry()
        self.update()

    def set_progress(self, appear: float, check: float) -> None:
        self._appear, self._check = _clamp(appear), _clamp(check)
        self.update()

    def _has_glyph(self) -> bool:
        return self._tone in (FOUND, WARNING)

    def _glyph_side(self) -> float:
        return QFontMetricsF(self.font()).ascent() * 0.9

    def sizeHint(self) -> QSize:
        metrics = QFontMetricsF(self.font())
        width = metrics.horizontalAdvance(self._text) + 2 * self.PAD if self._text else 0.0
        if self._text and self._has_glyph():
            width += self._glyph_side() + self.GAP
        return QSize(math.ceil(width), math.ceil(metrics.height() + 6))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self.updateGeometry()

    def _colors(self):
        if self._tone == FOUND:
            rgb = token_color("success-rgb")
            return _alpha(rgb, 0.15), _alpha(rgb, 0.42), token_color("success-text-strong")
        if self._tone == WARNING:
            rgb = token_color("warning-rgb")
            return _alpha(rgb, 0.15), _alpha(rgb, 0.48), token_color("warning-text")
        if self._tone == BUILTIN_TONE:
            return (token_color("accent-tint"), token_color("accent-tint-border"),
                    token_color("accent-soft"))
        return (token_color("slate-raised"), token_color("slate-border"),
                token_color("slate-text-3"))

    def paintEvent(self, _event) -> None:
        if not self._text or self._appear <= 0.0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(self._appear)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        # Grows the last few percent into place as it fades in.
        scale = 0.86 + 0.14 * _ease_out(self._appear)
        center = rect.center()
        painter.translate(center)
        painter.scale(scale, scale)
        painter.translate(-center)
        fill, border, ink = self._colors()
        radius = rect.height() / 2.0
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, radius, radius)

        x = rect.left() + self.PAD
        if self._has_glyph():
            side = self._glyph_side()
            box = QRectF(x, center.y() - side / 2.0, side, side)
            if self._tone == FOUND:
                self._paint_check(painter, box, ink)
            else:
                self._paint_alert(painter, box, ink)
            x += side + self.GAP
        painter.setOpacity(self._appear)
        painter.setPen(ink)
        painter.setFont(self.font())
        painter.drawText(
            QRectF(x, rect.top(), rect.right() - x, rect.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self._text,
        )

    def _paint_check(self, painter: QPainter, box: QRectF, ink: QColor) -> None:
        points = [
            QPointF(box.left() + box.width() * 0.14, box.top() + box.height() * 0.54),
            QPointF(box.left() + box.width() * 0.40, box.top() + box.height() * 0.80),
            QPointF(box.left() + box.width() * 0.88, box.top() + box.height() * 0.24),
        ]
        lengths = [
            math.dist((points[i].x(), points[i].y()), (points[i + 1].x(), points[i + 1].y()))
            for i in range(2)
        ]
        remaining = sum(lengths) * self._check
        if remaining <= 0.0:
            return
        path = QPainterPath(points[0])
        for start, end, length in zip(points, points[1:], lengths):
            if remaining <= 0.0:
                break
            t = min(1.0, remaining / length) if length else 1.0
            path.lineTo(start + (end - start) * t)
            remaining -= length
        pen = QPen(ink, max(1.4, box.height() * 0.16))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

    def _paint_alert(self, painter: QPainter, box: QRectF, ink: QColor) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ink)
        radius = box.width() * 0.42
        painter.drawEllipse(box.center(), radius, radius)
        pen = QPen(token_color("slate-surface"), max(1.2, box.width() * 0.14))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        top = QPointF(box.center().x(), box.center().y() - radius * 0.5)
        bottom = QPointF(box.center().x(), box.center().y() + radius * 0.12)
        painter.drawLine(top, bottom)
        painter.drawPoint(QPointF(box.center().x(), box.center().y() + radius * 0.5))


class AgentTile(QWidget):
    """One choice in the picker: a mark, a name, a status pill, a detail line.

    The card is painted here rather than by the stylesheet so it can lift, glow
    in the agent's colour, and shimmer while a scan runs. The widget is a little
    larger than the card it draws; the extra margin holds the lift and shadow.
    """

    clicked = pyqtSignal(str)

    BLEED_X: Final[int] = 3
    BLEED_TOP: Final[int] = 3
    BLEED_BOTTOM: Final[int] = 7
    PAD_X: Final[int] = 14
    PAD_Y: Final[int] = 12
    RADIUS: Final[float] = 12.0
    LIFT_PX: Final[float] = 2.0

    def __init__(self, tile_id: str, name: str, monogram: str, accent: str,
                 pixmap: Optional[QPixmap] = None, install_url: str = "",
                 install_hint: str = "", parent=None):
        super().__init__(parent)
        self.tile_id = tile_id
        self.name = name
        self.setObjectName("agentTile")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._accent = QColor(accent)
        self._install_url = install_url
        self._state = IDLE_STATE
        self._pending: Optional[TileState] = None
        self._selected = False
        self._scanning = False
        self._shimmer = 0.0
        self._shimmer_offset = 0.0
        self._clock = QElapsedTimer()
        self._arrival = 1.0
        self._warm_from = 0.0
        self._warm_to = 0.0
        self._missing_from = 0.0
        self._hover = 0.0
        self._lift = 0.0
        self._margin_shift: Optional[int] = None
        self._focus_visible = False

        self._layout = QVBoxLayout(self)
        self._layout.setSpacing(5)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 4)
        top.setSpacing(8)
        self.mark = AgentMark(monogram, accent, pixmap, self)
        top.addWidget(self.mark, alignment=Qt.AlignmentFlag.AlignTop)
        top.addStretch(1)
        self.pill = StatusPill(self)
        self.pill.hide()
        top.addWidget(self.pill, alignment=Qt.AlignmentFlag.AlignTop)
        self._layout.addLayout(top)
        self.name_label = QLabel(name, self)
        self.name_label.setObjectName("agentTileName")
        self._layout.addWidget(self.name_label)
        self.detail_label = QLabel(self)
        self.detail_label.setObjectName("agentTileDetail")
        self.detail_label.setWordWrap(True)
        self.detail_label.hide()
        self._layout.addWidget(self.detail_label)
        self.link = QPushButton(f"Get {name} ↗", self)
        self.link.setObjectName("agentTileLink")
        self.link.setFlat(True)
        self.link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.link.setToolTip(install_hint)
        self.link.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.link.clicked.connect(self._open_install_page)
        self.link.hide()
        self._layout.addWidget(self.link, alignment=Qt.AlignmentFlag.AlignLeft)
        # Content sits at the top of a row's tallest tile. The grid places
        # tiles by hand, so this stretch never makes a page layout expand
        # (a layout alignment would squeeze wrapped text to the preferred
        # width's height instead).
        self._layout.addStretch(1)
        self._apply_margins()

        self._hover_anim = self._animation(_HOVER_MS, self._on_hover_value)
        self._lift_anim = self._animation(_LIFT_MS, self._on_lift_value)
        self._arrival_anim = self._animation(ARRIVAL_MS, self._on_arrival_value)
        self._shimmer_anim = self._animation(_SHIMMER_FADE_MS, self._on_shimmer_value)
        self._arrival_timer = QTimer(self)
        self._arrival_timer.setSingleShot(True)
        self._arrival_timer.timeout.connect(self._begin_arrival)
        self._sync_interaction()

    # ---- state ----

    @property
    def state(self) -> TileState:
        return self._state

    @property
    def selected(self) -> bool:
        return self._selected

    @property
    def selectable(self) -> bool:
        return self._state.selectable

    @property
    def scanning(self) -> bool:
        return self._scanning

    @property
    def shimmer(self) -> float:
        return self._shimmer

    @property
    def arrival(self) -> float:
        """Progress of the last result's reveal, 0 to 1."""
        return self._arrival

    def arrival_pending(self) -> bool:
        """True while a result waits for its turn to light up."""
        return self._arrival_timer.isActive()

    def begin_scan(self, index: int) -> None:
        """Shimmer from now until :meth:`end_scan`."""
        self._scanning = True
        self._shimmer_offset = index * _SHIMMER_OFFSET_MS
        self._clock.start()
        self._shimmer_anim.stop()
        self._shimmer = 1.0
        if self._state.tone == IDLE:
            self._set_detail(_CHECKING_DETAIL, muted=True)
        self._set_pill(SCANNING, "Looking…")
        self.pill.set_progress(1.0, 1.0)
        self.advance_scan()

    def advance_scan(self) -> None:
        """One shimmer frame: move the band and let the mark breathe."""
        self._sync_mark()
        self.update()

    def end_scan(self, animate: bool) -> None:
        """The scan is back: let the shimmer go."""
        self._scanning = False
        if animate and self._shimmer > 0.0:
            self._shimmer_anim.stop()
            self._shimmer_anim.setStartValue(self._shimmer)
            self._shimmer_anim.setEndValue(0.0)
            self._shimmer_anim.start()
        else:
            self._shimmer_anim.stop()
            self._on_shimmer_value(0.0)

    def show_state(self, state: TileState, animate: bool = False,
                   delay_ms: int = 0) -> None:
        """Show ``state``; with ``animate``, light up after ``delay_ms``."""
        self._arrival_timer.stop()
        if not animate:
            self._pending = None
            self._arrival_anim.stop()
            self._apply_state(state)
            self._warm_from = self._warm_to = 1.0 if state.installed else 0.0
            self._missing_from = 1.0 if state.tone == MISSING else 0.0
            self._on_arrival_value(1.0)
            return
        self._pending = state
        if delay_ms > 0:
            self._arrival_timer.start(delay_ms)
        else:
            self._begin_arrival()

    def set_selected(self, selected: bool, animate: bool = True) -> None:
        if selected == self._selected:
            return
        self._selected = selected
        target = 1.0 if selected else 0.0
        if animate:
            self._run(self._lift_anim, self._lift, target)
        else:
            self._lift_anim.stop()
            self._on_lift_value(target)
        self.setAccessibleDescription(self._accessible_description())
        self.update()

    def scan_phase(self) -> float:
        """Where the shimmer band is, 0 to 1."""
        if not self._clock.isValid():
            return 0.0
        elapsed = (self._clock.elapsed() + (1.0 + _SHIMMER_START) * _SHIMMER_PERIOD_MS
                   - self._shimmer_offset)
        return (elapsed % _SHIMMER_PERIOD_MS) / _SHIMMER_PERIOD_MS

    def scan_breath(self) -> float:
        """0 to 1 and back once per sweep: the mark and border glow with it."""
        if self._shimmer <= 0.0:
            return 0.0
        return (0.5 - 0.5 * math.cos(2.0 * math.pi * self.scan_phase())) * self._shimmer

    # ---- internals ----

    def _animation(self, duration: int, slot) -> QVariantAnimation:
        animation = QVariantAnimation(self)
        animation.setDuration(duration)
        animation.valueChanged.connect(slot)
        return animation

    @staticmethod
    def _run(animation: QVariantAnimation, start: float, end: float) -> None:
        animation.stop()
        if abs(start - end) < 1e-3:
            animation.valueChanged.emit(float(end))
            return
        animation.setStartValue(float(start))
        animation.setEndValue(float(end))
        animation.start()

    def _begin_arrival(self) -> None:
        state, self._pending = self._pending, None
        if state is None:
            return
        self._warm_from = self.mark.warmth
        self._warm_to = 1.0 if state.installed else 0.0
        self._missing_from = self.mark.missing
        self._apply_state(state)
        self._arrival_anim.stop()
        self._on_arrival_value(0.0)
        self._arrival_anim.setStartValue(0.0)
        self._arrival_anim.setEndValue(1.0)
        self._arrival_anim.start()

    def _apply_state(self, state: TileState) -> None:
        self._state = state
        self._set_pill(state.tone, state.pill)
        self._set_detail(state.detail, muted=state.tone == MISSING,
                         warning=state.tone == WARNING)
        self.link.setVisible(state.tone == MISSING and bool(self._install_url))
        if self.name_label.property("muted") != (state.tone == MISSING):
            set_style_property(self.name_label, "muted", state.tone == MISSING)
        self._sync_interaction()
        self.setAccessibleName(f"{self.name}, {state.pill}" if state.pill else self.name)
        self.setAccessibleDescription(self._accessible_description())
        self.update()

    def _accessible_description(self) -> str:
        parts = [self._state.detail]
        if self._selected:
            parts.append("Runs AI insights")
        return ". ".join(part for part in parts if part)

    def _set_pill(self, tone: str, text: str) -> None:
        self.pill.set_state(tone, text)
        self.pill.setVisible(bool(text))

    def _set_detail(self, text: str, muted: bool = False, warning: bool = False) -> None:
        self.detail_label.setText(text)
        self.detail_label.setVisible(bool(text))
        tone = "warning" if warning else ("muted" if muted else "")
        if (self.detail_label.property("tone") or "") != tone:
            set_style_property(self.detail_label, "tone", tone)

    def _sync_interaction(self) -> None:
        selectable = self._state.selectable
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if selectable else Qt.CursorShape.ArrowCursor
        )
        self.setFocusPolicy(
            Qt.FocusPolicy.StrongFocus if selectable else Qt.FocusPolicy.NoFocus
        )
        if not selectable and self._hover > 0.0:
            self._run(self._hover_anim, self._hover, 0.0)

    def _sync_mark(self) -> None:
        """Mark colour: the result's warmth, plus a breath while scanning."""
        t = _ease_out(self._arrival / 0.75)
        warm = self._warm_from + (self._warm_to - self._warm_from) * t
        warm = max(warm, 0.3 * self.scan_breath())
        self.mark.set_warmth(warm)
        missing_to = 1.0 if self._state.tone == MISSING else 0.0
        self.mark.set_missing(self._missing_from + (missing_to - self._missing_from) * t)

    def _on_hover_value(self, value) -> None:
        self._hover = float(value)
        self._apply_margins()
        self.update()

    def _on_lift_value(self, value) -> None:
        self._lift = float(value)
        self._apply_margins()
        self.update()

    def _on_shimmer_value(self, value) -> None:
        self._shimmer = float(value)
        self._sync_mark()
        self.update()

    def _on_arrival_value(self, value) -> None:
        t = float(value)
        self._arrival = t
        self._sync_mark()
        self.pill.set_progress(_ease_out((t - 0.12) / 0.45), _ease_in_out((t - 0.35) / 0.6))
        self.update()

    def _lift_amount(self) -> float:
        hover = self._hover if self._state.selectable else 0.0
        return self.LIFT_PX * self._lift + (1.0 - self._lift) * hover

    def _apply_margins(self) -> None:
        """Move the content with the card so the whole tile lifts."""
        shift = round(self._lift_amount())
        if shift == self._margin_shift:
            return
        self._margin_shift = shift
        self._layout.setContentsMargins(
            self.BLEED_X + self.PAD_X,
            self.BLEED_TOP + self.PAD_Y - shift,
            self.BLEED_X + self.PAD_X,
            self.BLEED_BOTTOM + self.PAD_Y + shift,
        )

    def card_rect(self) -> QRectF:
        rect = QRectF(self.rect()).adjusted(
            self.BLEED_X + 0.5, self.BLEED_TOP + 0.5,
            -self.BLEED_X - 0.5, -self.BLEED_BOTTOM - 0.5,
        )
        return rect.translated(0.0, -self._lift_amount())

    def _warmth(self) -> float:
        """How lit the card is: a found agent after its arrival."""
        if self._state.tone != FOUND:
            return 0.0
        return _ease_out(self._arrival / 0.75)

    # ---- painting ----

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = self.card_rect()
        radius = self.RADIUS
        dark = _dark()
        tone = self._state.tone
        accent = token_color("accent") if tone == BUILTIN_TONE else self._accent
        blocked = self._selected and tone in (MISSING, WARNING)
        ring = token_color("warning") if blocked else _ring_color(accent)
        hover = self._hover if self._state.selectable else 0.0

        raised = max(self._lift, hover * 0.6)
        if raised > 0.0:
            self._paint_shadow(painter, card, raised, ring if self._selected else None)

        base = token_color("slate-field" if tone == MISSING else "slate-surface")
        path = QPainterPath()
        path.addRoundedRect(card, radius, radius)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(base)
        painter.drawPath(path)
        # Found, hovered, and chosen tiles glow in their own colour, never grey.
        wash = self._warmth() * (0.085 if dark else 0.05)
        wash += (1.0 - self._lift) * hover * (0.05 if dark else 0.035)
        wash += self._lift * (0.07 if dark else 0.055)
        if wash > 0.0:
            painter.setBrush(_alpha(ring if blocked else accent, wash))
            painter.drawPath(path)

        if self._shimmer > 0.0:
            self._paint_shimmer(painter, path, card, accent)

        if self._selected:
            painter.setPen(QPen(ring, 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(
                card.adjusted(0.5, 0.5, -0.5, -0.5), radius - 0.5, radius - 0.5
            )
        else:
            border = token_color(
                "slate-border-subtle" if tone == MISSING else "slate-border"
            )
            border = _mix(border, _alpha(accent, 0.6), self._warmth() * 0.45)
            # While the scan looks, the edge breathes the agent's colour.
            border = _mix(border, _alpha(accent, 0.7), self.scan_breath() * 0.5)
            border = _mix(border, _alpha(_ring_color(accent), 0.85), hover * 0.8)
            painter.setPen(QPen(border, 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        if self._focus_visible and self.hasFocus() and self._state.selectable:
            pen = QPen(token_color("accent-border"), 1.0)
            pen.setStyle(Qt.PenStyle.DotLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(card.adjusted(-2.0, -2.0, 2.0, 2.0), radius + 2, radius + 2)

    def _paint_shadow(self, painter: QPainter, card: QRectF, amount: float,
                      glow: Optional[QColor]) -> None:
        """A soft drop shadow, tinted by the ring colour when selected."""
        painter.setPen(Qt.PenStyle.NoPen)
        layers = 4
        if glow is not None:
            base = _alpha(glow, (0.17 if _dark() else 0.14) * amount)
        else:
            base = _alpha(token_color("shadow-rgb"), (0.30 if _dark() else 0.08) * amount)
        for layer in range(layers):
            spread = 0.8 + layer * 0.9
            painter.setBrush(_alpha(base, base.alphaF() * (1.0 - layer / layers)))
            rect = card.translated(0.0, 1.0 + 1.5 * amount).adjusted(
                -spread, -spread * 0.4, spread, spread
            )
            painter.drawRoundedRect(rect, self.RADIUS + spread, self.RADIUS + spread)

    def _paint_shimmer(self, painter: QPainter, path: QPainterPath, card: QRectF,
                       accent: QColor) -> None:
        """A soft band of the agent's colour sweeping across the card."""
        # Linear, and never fully off the card, so there is no dead moment.
        center = -0.15 + 1.3 * self.scan_phase()
        band = 0.32
        peak = (0.22 if _dark() else 0.14) * self._shimmer

        def at(position: float) -> QColor:
            strength = max(0.0, 1.0 - abs(position - center) / band)
            return _alpha(accent, peak * strength)

        gradient = QLinearGradient(
            card.topLeft(), QPointF(card.right(), card.bottom() + card.height() * 0.8)
        )
        stops = {0.0, 1.0, center - band, center, center + band}
        for position in sorted(stop for stop in stops if 0.0 <= stop <= 1.0):
            gradient.setColorAt(position, at(position))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawPath(path)

    # ---- input ----

    def event(self, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.HoverEnter and self._state.selectable:
            self._run(self._hover_anim, self._hover, 1.0)
        elif kind == QEvent.Type.HoverLeave and self._hover > 0.0:
            self._run(self._hover_anim, self._hover, 0.0)
        elif kind in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            self.update()
        return super().event(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._state.selectable:
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if (event.button() == Qt.MouseButton.LeftButton and self._state.selectable
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit(self.tile_id)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if self._state.selectable and event.key() in (
            Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter
        ):
            self.clicked.emit(self.tile_id)
            event.accept()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        # Ring only for keyboard focus; a click already shows the choice.
        self._focus_visible = event.reason() in (
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        )
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()

    def _open_install_page(self) -> None:
        if self._install_url:
            QDesktopServices.openUrl(QUrl(self._install_url))


class _TileGrid(QWidget):
    """Four tiles in a row, or two by two when the page is narrow.

    Placed by hand so each tile's shadow margin can bleed past the grid's
    edges: the cards line up with the other cards on the page.
    """

    GAP = 12
    MIN_CARD = 160

    def __init__(self, tiles: Iterable[AgentTile], parent=None):
        super().__init__(parent)
        self.setObjectName("agentTileGrid")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._tiles = list(tiles)
        for tile in self._tiles:
            tile.setParent(self)
        self._height = 0

    def columns(self, width: Optional[int] = None) -> int:
        width = self.width() if width is None else width
        card = round(self.MIN_CARD * current_ui_font_scale())
        if width >= 4 * card + 3 * self.GAP:
            return 4
        if width >= 2 * card + self.GAP:
            return 2
        return 1

    def _plan(self, width: int):
        columns = self.columns(width)
        cell = (width - self.GAP * (columns - 1)) / columns
        bleed_x = AgentTile.BLEED_X
        extra = AgentTile.BLEED_TOP + AgentTile.BLEED_BOTTOM
        rows = []
        for start in range(0, len(self._tiles), columns):
            row = self._tiles[start:start + columns]
            height = max(
                tile.heightForWidth(round(cell) + 2 * bleed_x) if tile.hasHeightForWidth()
                else tile.sizeHint().height()
                for tile in row
            ) - extra
            rows.append((row, height))
        return columns, cell, rows

    def _layout_tiles(self) -> None:
        width = self.width()
        if width <= 0:
            return
        _columns, cell, rows = self._plan(width)
        y = float(AgentTile.BLEED_TOP)
        for row, height in rows:
            for index, tile in enumerate(row):
                x = index * (cell + self.GAP)
                tile.setGeometry(
                    round(x) - AgentTile.BLEED_X,
                    round(y) - AgentTile.BLEED_TOP,
                    round(x + cell) - round(x) + 2 * AgentTile.BLEED_X,
                    height + AgentTile.BLEED_TOP + AgentTile.BLEED_BOTTOM,
                )
            y += height + self.GAP
        total = round(y - self.GAP) + AgentTile.BLEED_BOTTOM
        if total != self._height:
            self._height = total
            self.updateGeometry()

    def sizeHint(self) -> QSize:
        width = self.width() if self.width() > 0 else 720
        _columns, _cell, rows = self._plan(width)
        height = sum(h for _row, h in rows) + self.GAP * (len(rows) - 1)
        height += AgentTile.BLEED_TOP + AgentTile.BLEED_BOTTOM
        return QSize(2 * round(self.MIN_CARD * current_ui_font_scale()) + self.GAP, height)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.sizeHint().height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_tiles()
        if event.oldSize().width() != event.size().width():
            self.updateGeometry()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._layout_tiles()

    def event(self, event) -> bool:
        # A tile's text changed height; its parent has no layout to tell.
        if event.type() == QEvent.Type.LayoutRequest:
            self._layout_tiles()
        return super().event(event)


class AgentPicker(QWidget):
    """The "Who runs AI insights" group: tiles, a rescan, and the model row.

    The picker scans and reports; it saves nothing. The host saves a choice
    when :attr:`choice_requested` or :attr:`model_chosen` fires and answers
    with :meth:`set_choice` and :meth:`set_saved_models`.
    """

    #: A tile was chosen: an agent id, or :data:`BUILTIN`.
    choice_requested = pyqtSignal(str)
    #: ``(agent_id, model)`` picked in the model row; "" is the agent's default.
    model_chosen = pyqtSignal(str, str)
    #: A scan result was applied (fresh or cached).
    results_changed = pyqtSignal()
    _scan_done = pyqtSignal(int, object)
    _models_done = pyqtSignal(str, int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("agentPicker")
        self._agents: Optional[Dict[str, Optional[InstalledAgent]]] = None
        self._scanning = False
        self._generation = 0
        self._model_generation = 0
        self._choice = BUILTIN
        self._saved_models: Dict[str, str] = {}
        self._models: Dict[str, List[AgentModel]] = {}
        self._models_loading: Dict[str, int] = {}
        self._shown_models_for = ""
        self._long_list = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(12)
        heading = QLabel("Who runs AI insights".upper(), self)
        heading.setObjectName("settingsTileGroupTitle")
        header.addWidget(heading, alignment=Qt.AlignmentFlag.AlignBottom)
        header.addStretch(1)
        self.look_again_button = Button("Look again")
        self.look_again_button.setObjectName("agentLookAgainButton")
        self.look_again_button.setIcon(design_icon("refresh-blue.svg"))
        self.look_again_button.set_base_minimum_size(0, 32)
        self.look_again_button.setToolTip(
            "Check this computer again, for example after installing an agent"
        )
        self.look_again_button.clicked.connect(self.rescan)
        header.addWidget(self.look_again_button)
        layout.addLayout(header)

        self.count_label = WrappedLabel(
            "Coding agents you already use can run AI insights with their own "
            "sign-in.", self,
        )
        self.count_label.setObjectName("infoLabel")
        layout.addWidget(self.count_label)

        self.tiles: Dict[str, AgentTile] = {}
        for agent_id in AGENT_ORDER:
            spec = AGENT_SPECS[agent_id]
            tile = AgentTile(
                agent_id, spec.name, spec.monogram, spec.accent,
                install_url=spec.install_url, install_hint=spec.install_hint,
            )
            self.tiles[agent_id] = tile
        builtin = AgentTile(BUILTIN, "OpenWhisper", "OW", "#0a84ff", pixmap=_app_mark())
        builtin.show_state(BUILTIN_STATE)
        self.tiles[BUILTIN] = builtin
        for tile in self.tiles.values():
            tile.clicked.connect(self._on_tile_clicked)
        self.grid = _TileGrid(self.tiles.values(), self)
        layout.addWidget(self.grid)

        self.notice = QFrame(self)
        self.notice.setObjectName("settingsTile")
        self.notice.setProperty("kind", "notice")
        notice_row = QHBoxLayout(self.notice)
        notice_row.setContentsMargins(14, 10, 14, 10)
        notice_row.setSpacing(10)
        notice_icon = QLabel(self.notice)
        notice_icon.setObjectName("settingsTileIcon")
        notice_icon.setFixedSize(18, 18)
        notice_icon.setPixmap(design_icon("info-warning.svg").pixmap(16, 16))
        notice_row.addWidget(notice_icon, alignment=Qt.AlignmentFlag.AlignTop)
        self.notice_label = WrappedLabel("", self.notice)
        self.notice_label.setObjectName("settingsTileDescription")
        notice_row.addWidget(self.notice_label, stretch=1)
        self.notice.hide()
        layout.addWidget(self.notice)

        self.model_card = QFrame(self)
        self.model_card.setObjectName("settingsTile")
        self.model_card.setProperty("kind", "field")
        card = QVBoxLayout(self.model_card)
        card.setContentsMargins(16, 14, 16, 14)
        card.setSpacing(8)
        self.model_field = QWidget(self.model_card)
        self.model_field.setObjectName("modelManagerFieldGroup")
        field = QVBoxLayout(self.model_field)
        field.setContentsMargins(0, 0, 0, 0)
        field.setSpacing(5)
        self.model_field_label = QLabel("Model", self.model_field)
        self.model_field_label.setObjectName("textModelFieldLabel")
        field.addWidget(self.model_field_label)
        self.short_model_combo = ElidingComboBox(self.model_field)
        self.short_model_combo.setObjectName("agentModelCombo")
        self.short_model_combo.setMinimumHeight(40)
        self.short_model_combo.activated.connect(self._on_short_model_activated)
        field.addWidget(self.short_model_combo)
        self.long_model_combo = SearchableComboBox(self.model_field)
        self.long_model_combo.setObjectName("agentModelSearchCombo")
        self.long_model_combo.setMinimumHeight(40)
        self.long_model_combo.activated.connect(self._on_long_model_activated)
        self.long_model_combo.lineEdit().editingFinished.connect(self._restore_long_model_text)
        self.long_model_combo.lineEdit().setPlaceholderText("Type to filter models")
        self.long_model_combo.hide()
        field.addWidget(self.long_model_combo)
        card.addWidget(self.model_field)
        self.model_status = WrappedLabel("", self.model_card)
        self.model_status.setObjectName("infoLabel")
        self.model_status.hide()
        card.addWidget(self.model_status)
        self.usage_label = WrappedLabel("", self.model_card)
        self.usage_label.setObjectName("infoLabel")
        card.addWidget(self.usage_label)
        self.model_card.hide()
        layout.addWidget(self.model_card)

        self._tick = QTimer(self)
        self._tick.setInterval(_TICK_MS)
        self._tick.timeout.connect(self._on_tick)
        self._scan_done.connect(self._on_scan_done)
        self._models_done.connect(self._on_models_done)

        cached = cached_agents()
        if cached is not None:
            self._apply_results(cached, animate=False)
        self._sync_selection()

    # ---- scanning ----

    def is_scanning(self) -> bool:
        return self._scanning

    def agents(self) -> Optional[Dict[str, Optional[InstalledAgent]]]:
        """The last result the tiles show, or None before one arrives."""
        return None if self._agents is None else dict(self._agents)

    def agent(self, agent_id: str) -> Optional[InstalledAgent]:
        return (self._agents or {}).get(agent_id)

    def ensure_scanned(self) -> None:
        """Show what is on this computer: the cached scan, or a new one."""
        if self._agents is not None or self._scanning:
            return
        cached = cached_agents()
        if cached is not None:
            self._apply_results(cached, animate=False)
            return
        self.start_scan(refresh=False)

    def rescan(self) -> None:
        """Look again, for example after the user installs an agent."""
        self.start_scan(refresh=True)

    def start_scan(self, refresh: bool) -> None:
        self._generation += 1
        generation = self._generation
        self._scanning = True
        self.look_again_button.setEnabled(False)
        self.count_label.setText("Looking for coding agents on this computer…")
        for index, agent_id in enumerate(AGENT_ORDER):
            self.tiles[agent_id].begin_scan(index)
        if refresh:
            self._models.clear()
        self._tick.start()

        def worker() -> None:
            try:
                result = scan_installed_agents(refresh=refresh)
            except Exception:
                logger.warning("Scanning for installed agents failed", exc_info=True)
                result = {}
            try:
                self._scan_done.emit(generation, result)
            except RuntimeError:
                pass  # Settings closed before the scan finished.

        threading.Thread(target=worker, name="settings-agent-scan", daemon=True).start()

    def _on_tick(self) -> None:
        for agent_id in AGENT_ORDER:
            self.tiles[agent_id].advance_scan()

    def _on_scan_done(self, generation: int, result) -> None:
        if generation != self._generation or not self._scanning:
            return
        self._scanning = False
        self._tick.stop()
        self._apply_results(dict(result or {}), animate=True)

    def _apply_results(self, result: Dict[str, Optional[InstalledAgent]],
                       animate: bool) -> None:
        self._agents = {agent_id: result.get(agent_id) for agent_id in AGENT_ORDER}
        self.look_again_button.setEnabled(True)
        for index, agent_id in enumerate(AGENT_ORDER):
            tile = self.tiles[agent_id]
            tile.end_scan(animate)
            tile.show_state(
                describe_agent(agent_id, self._agents[agent_id]),
                animate=animate,
                delay_ms=index * STAGGER_MS,
            )
        self.count_label.setText(found_count_text(self._agents))
        self._sync_selection()
        self.results_changed.emit()

    # ---- choice ----

    def choice(self) -> str:
        return self._choice

    def set_choice(self, core: str) -> None:
        """Show ``core`` (a ``MeetingAgentCore`` value) as the chosen tile."""
        self._choice = core if core in AGENT_ORDER else BUILTIN
        self._sync_selection()

    def set_saved_models(self, models: Dict[str, str]) -> None:
        self._saved_models = dict(models or {})
        if self._shown_models_for:
            self._fill_models(self._shown_models_for)

    def _on_tile_clicked(self, tile_id: str) -> None:
        if tile_id != self._choice:
            self.choice_requested.emit(tile_id)

    def _sync_selection(self) -> None:
        # A hidden page jumps to the choice; only a visible change lifts.
        animate = self.isVisible()
        for tile_id, tile in self.tiles.items():
            tile.set_selected(tile_id == self._choice, animate=animate)
        if self._choice == BUILTIN:
            self.notice.hide()
            self._show_model_card(None)
            return
        if self._agents is None:
            # Not scanned yet: the tile shows the scan; nothing to say below.
            self.notice.hide()
            self._show_model_card(None)
            return
        agent = self._agents.get(self._choice)
        message = blocked_notice(self._choice, agent)
        self.notice_label.setText(message)
        self.notice.setVisible(bool(message))
        self._show_model_card(agent if agent is not None and agent.usable else None)

    # ---- models ----

    def model_combo(self):
        """The combo now showing the chosen agent's models."""
        return self.long_model_combo if self._long_list else self.short_model_combo

    def _show_model_card(self, agent: Optional[InstalledAgent]) -> None:
        # The field hides with the card so Settings search skips it too.
        self.model_field.setVisible(agent is not None)
        if agent is None:
            self._shown_models_for = ""
            self.model_card.hide()
            return
        self._shown_models_for = agent.id
        self.usage_label.setText(usage_caption(agent))
        self.model_card.show()
        if agent.id in self._models:
            self._fill_models(agent.id)
            return
        if agent.spec.transport == "acp":
            self._load_models_in_background(agent)
            return
        try:
            models = list(list_agent_models(agent))
        except Exception:
            logger.warning("Could not list %s models", agent.id, exc_info=True)
            models = []
        self._models[agent.id] = models
        self._fill_models(agent.id)

    def _load_models_in_background(self, agent: InstalledAgent) -> None:
        name = agent.spec.name
        self._set_combo_items([AgentModel("", f"{name} default")], "")
        self.short_model_combo.setEnabled(False)
        self.model_status.setText(f"Loading {name}'s models…")
        self.model_status.show()
        if agent.id in self._models_loading:
            return
        self._model_generation += 1
        generation = self._model_generation
        self._models_loading[agent.id] = generation

        def worker() -> None:
            try:
                models = list(list_agent_models(agent))
            except Exception:
                logger.warning("Could not list %s models", agent.id, exc_info=True)
                models = []
            try:
                self._models_done.emit(agent.id, generation, models)
            except RuntimeError:
                pass  # Settings closed first.

        threading.Thread(target=worker, name=f"settings-{agent.id}-models",
                         daemon=True).start()

    def _on_models_done(self, agent_id: str, generation: int, models) -> None:
        if self._models_loading.get(agent_id) != generation:
            return
        self._models_loading.pop(agent_id, None)
        self._models[agent_id] = list(models or [])
        if self._shown_models_for == agent_id:
            self._fill_models(agent_id)

    def _fill_models(self, agent_id: str) -> None:
        name = AGENT_SPECS[agent_id].name
        models = list(self._models.get(agent_id) or [])
        if not models or models[0].value != "":
            models.insert(0, AgentModel("", f"{name} default"))
        saved = self._saved_models.get(agent_id, "")
        if saved and all(model.value != saved for model in models):
            models.append(AgentModel(saved, saved))
        self._set_combo_items(models, saved)
        self.short_model_combo.setEnabled(True)
        if AGENT_SPECS[agent_id].transport == "acp" and len(models) <= 1:
            self.model_status.setText(
                f"{name} didn't list any models, so it uses its own default."
            )
            self.model_status.show()
        else:
            self.model_status.hide()

    def _set_combo_items(self, models: List[AgentModel], current: str) -> None:
        long_list = len(models) > FILTER_THRESHOLD
        self._long_list = long_list
        combo = self.long_model_combo if long_list else self.short_model_combo
        self.short_model_combo.setVisible(not long_list)
        self.long_model_combo.setVisible(long_list)
        blocker = combo.blockSignals(True)
        combo.clear()
        for model in models:
            combo.addItem(model.label, model.value)
        index = max(0, combo.findData(current))
        combo.setCurrentIndex(index)
        combo.blockSignals(blocker)
        combo.setToolTip(combo.itemText(index))

    def _on_short_model_activated(self, index: int) -> None:
        self._commit_model(self.short_model_combo, index)

    def _on_long_model_activated(self, index: int) -> None:
        self._commit_model(self.long_model_combo, index)

    def _commit_model(self, combo, index: int) -> None:
        agent_id = self._shown_models_for
        if not agent_id or index < 0:
            return
        blocker = combo.blockSignals(True)
        combo.setCurrentIndex(index)
        combo.blockSignals(blocker)
        combo.setToolTip(combo.itemText(index))
        value = combo.itemData(index) or ""
        if value == self._saved_models.get(agent_id, ""):
            return
        self._saved_models[agent_id] = value
        self.model_chosen.emit(agent_id, value)

    def _restore_long_model_text(self) -> None:
        """A filter fragment is not a choice; show the chosen model again."""
        combo = self.long_model_combo
        index = max(0, combo.findData(self._saved_models.get(self._shown_models_for, "")))
        if combo.currentText() != combo.itemText(index):
            blocker = combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.setEditText(combo.itemText(index))
            combo.blockSignals(blocker)
