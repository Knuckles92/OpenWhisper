"""Settings → Overview: what OpenWhisper is running right now.

The landing page of the Settings window. Every card repeats a value the rail
already shows and opens that destination when clicked, so the page never
holds a setting of its own and cannot drift out of sync with the pages it
summarizes. Settings composes an :class:`OverviewSummary` and hands it over.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QIcon, QKeyEvent, QMouseEvent, QPainter, QPainterPath
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from config import bundle_root
from services.format_utils import format_size_bytes
from ui_qt.utils.palette import token_color
from ui_qt.widgets.eliding_label import ElidingLabel
from ui_qt.widgets.wrapped_label import WrappedLabel

#: Storage bar colours, in backend legend order.
_BACKEND_TOKENS = ("accent", "success", "purple", "warning-text-soft", "accent-cyan")


def _icon(filename: str) -> QIcon:
    return QIcon(str(Path(bundle_root()) / "ui_qt" / "assets" / "tabler" / filename))


@dataclass
class OverviewSummary:
    """Everything the Overview shows, composed by Settings."""

    voice: Tuple[str, str] = ("", "")
    cleanup: Tuple[str, str] = ("", "")
    cleanup_on: bool = False
    profiles: Tuple[str, str] = ("", "")
    meeting_voice: Tuple[str, str] = ("", "")
    intelligence: Tuple[str, str] = ("", "")
    hotkeys: Tuple[str, str] = ("", "")
    local_items: List[str] = field(default_factory=list)
    cloud_items: List[str] = field(default_factory=list)
    storage_downloaded: int = 0
    storage_total: int = 0
    storage_bytes: int = 0
    storage_by_backend: Dict[str, int] = field(default_factory=dict)
    storage_checking: bool = False
    components: List[Tuple[str, bool]] = field(default_factory=list)
    footer: str = ""


class OverviewCard(QFrame):
    """A clickable summary of one destination."""

    clicked = pyqtSignal(str)

    def __init__(self, key: str, eyebrow: str, role: str, icon: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.setObjectName("overviewCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(150)

        column = QVBoxLayout(self)
        column.setContentsMargins(15, 12, 15, 13)
        column.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(7)
        glyph = QLabel()
        glyph.setObjectName("overviewCardIcon")
        glyph.setFixedSize(14, 14)
        glyph.setPixmap(_icon(icon).pixmap(13, 13))
        top.addWidget(glyph)
        self.eyebrow_label = QLabel(eyebrow.upper())
        self.eyebrow_label.setObjectName("overviewEyebrow")
        top.addWidget(self.eyebrow_label)
        top.addStretch()
        arrow = QLabel("→")
        arrow.setObjectName("overviewArrow")
        top.addWidget(arrow)
        column.addLayout(top)
        column.addSpacing(6)

        self.role_label = QLabel(role)
        self.role_label.setObjectName("overviewRole")
        column.addWidget(self.role_label)
        self.value_label = ElidingLabel("")
        self.value_label.setObjectName("overviewValue")
        column.addWidget(self.value_label)
        self.detail_label = ElidingLabel("")
        self.detail_label.setObjectName("overviewDetail")
        column.addWidget(self.detail_label)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(6)
        column.addLayout(self.body)
        column.addStretch()

    def set_values(self, value: str, detail: str, *, muted: bool = False) -> None:
        self.value_label.setText(value)
        self.detail_label.setText(detail)
        self.detail_label.setVisible(bool(detail))
        self.value_label.setProperty("muted", muted)
        self.value_label.style().unpolish(self.value_label)
        self.value_label.style().polish(self.value_label)
        self.setToolTip(f"{self.role_label.text()}: {value}" if value else "")

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit(self.key)
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(self.key)
            return
        super().keyPressEvent(event)


class StorageBar(QWidget):
    """One rounded bar split by how much each backend's models use."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("overviewStorageBar")
        self.setFixedHeight(8)
        self._segments: List[Tuple[float, str]] = []

    def set_segments(self, sizes: List[int]) -> None:
        total = sum(sizes)
        self._segments = [
            (size / total, _BACKEND_TOKENS[index % len(_BACKEND_TOKENS)])
            for index, size in enumerate(sizes)
            if total and size
        ]
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        painter.setClipPath(clip)
        painter.fillRect(rect, token_color("slate-raised"))
        x = rect.left()
        for fraction, token in self._segments:
            width = rect.width() * fraction
            painter.fillRect(QRectF(x, rect.top(), width, rect.height()), token_color(token))
            x += width
        painter.end()


class OverviewPage(QWidget):
    """Cards that summarize the window's destinations and open them."""

    destination_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("overviewPage")
        self.cards: Dict[str, OverviewCard] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._build_where_strip())

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        specs = (
            ("voice_model", "Dictation", "Voice model", "microphone-blue.svg"),
            ("cleanup", "Dictation", "AI cleanup", "stack-purple.svg"),
            ("cleanup_profiles", "Dictation", "Cleanup profiles", "typography-blue.svg"),
            ("meeting_voice", "Meeting Mode", "Voice & speakers", "microphone-blue.svg"),
            ("meeting_intelligence", "Meeting Mode", "Intelligence", "stack-purple.svg"),
            ("hotkeys", "App", "Hotkeys", "bolt-green.svg"),
        )
        for index, (key, eyebrow, role, icon) in enumerate(specs):
            card = OverviewCard(key, eyebrow, role, icon)
            card.clicked.connect(self.destination_requested)
            self.cards[key] = card
            row, column = divmod(index, 3)
            grid.addWidget(card, row, column)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        layout.addLayout(grid)

        storage_row = QGridLayout()
        storage_row.setContentsMargins(0, 0, 0, 0)
        storage_row.setHorizontalSpacing(12)
        self.storage_card = OverviewCard(
            "downloads", "Models & storage", "Downloaded speech models",
            "download-blue.svg",
        )
        self.storage_card.clicked.connect(self.destination_requested)
        self.storage_bar = StorageBar()
        self.storage_card.body.addSpacing(4)
        self.storage_card.body.addWidget(self.storage_bar)
        self.storage_legend = WrappedLabel("")
        self.storage_legend.setObjectName("overviewLegend")
        self.storage_legend.setTextFormat(Qt.TextFormat.RichText)
        self.storage_card.body.addWidget(self.storage_legend)
        self.cards["downloads"] = self.storage_card
        storage_row.addWidget(self.storage_card, 0, 0)

        self.components_card = OverviewCard(
            "components", "Models & storage", "Components", "box-blue.svg"
        )
        self.components_card.clicked.connect(
            lambda _key: self.destination_requested.emit("downloads")
        )
        self.components_list = QLabel("")
        self.components_list.setObjectName("overviewComponents")
        self.components_list.setTextFormat(Qt.TextFormat.RichText)
        self.components_card.body.addWidget(self.components_list)
        self.cards["components"] = self.components_card
        storage_row.addWidget(self.components_card, 0, 1)
        storage_row.setColumnStretch(0, 2)
        storage_row.setColumnStretch(1, 1)
        layout.addLayout(storage_row)

        self.footer_label = WrappedLabel("")
        self.footer_label.setObjectName("overviewFooter")
        layout.addWidget(self.footer_label)

    def _build_where_strip(self) -> QWidget:
        strip = QFrame()
        strip.setObjectName("overviewWhere")
        row = QHBoxLayout(strip)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.local_title, self.local_list = self._where_half(
            row, "Stays on this computer", "ok"
        )
        divider = QFrame()
        divider.setObjectName("overviewWhereDivider")
        divider.setFixedWidth(1)
        row.addWidget(divider)
        self.cloud_title, self.cloud_list = self._where_half(
            row, "Sent to a cloud provider, only when used", "warn"
        )
        return strip

    @staticmethod
    def _where_half(row: QHBoxLayout, title: str, tone: str):
        half = QWidget()
        half.setObjectName("overviewWhereHalf")
        layout = QHBoxLayout(half)
        layout.setContentsMargins(16, 11, 16, 11)
        layout.setSpacing(11)
        dot = QLabel()
        dot.setObjectName("overviewDot")
        dot.setProperty("tone", tone)
        dot.setFixedSize(8, 8)
        dot_column = QVBoxLayout()
        dot_column.setContentsMargins(0, 5, 0, 0)
        dot_column.addWidget(dot)
        dot_column.addStretch()
        layout.addLayout(dot_column)
        copy = QVBoxLayout()
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("overviewWhereTitle")
        copy.addWidget(title_label)
        items = WrappedLabel("")
        items.setObjectName("overviewWhereList")
        copy.addWidget(items)
        layout.addLayout(copy, stretch=1)
        row.addWidget(half, stretch=1)
        return title_label, items

    def update_summary(self, summary: OverviewSummary) -> None:
        """Repaint every card from one consistent snapshot."""
        self.cards["voice_model"].set_values(*summary.voice)
        self.cards["cleanup"].set_values(*summary.cleanup, muted=not summary.cleanup_on)
        self.cards["cleanup_profiles"].set_values(*summary.profiles)
        self.cards["meeting_voice"].set_values(*summary.meeting_voice)
        self.cards["meeting_intelligence"].set_values(*summary.intelligence)
        self.cards["hotkeys"].set_values(*summary.hotkeys)

        self.local_list.setText(" · ".join(summary.local_items) or "Nothing")
        self.cloud_list.setText(" · ".join(summary.cloud_items) or "Nothing")

        if summary.storage_checking:
            self.storage_card.set_values("Checking…", "Scanning downloaded models")
            self.storage_bar.set_segments([])
            self.storage_legend.setText("")
        else:
            self.storage_card.set_values(
                format_size_bytes(summary.storage_bytes),
                f"{summary.storage_downloaded} of {summary.storage_total} "
                "speech models downloaded",
            )
            ordered = sorted(
                summary.storage_by_backend.items(), key=lambda item: -item[1]
            )
            self.storage_bar.set_segments([size for _label, size in ordered])
            parts = []
            for index, (label, size) in enumerate(ordered):
                color = token_color(_BACKEND_TOKENS[index % len(_BACKEND_TOKENS)]).name()
                parts.append(
                    f'<span style="color:{color}">■</span>&nbsp;{label} '
                    f"{format_size_bytes(size)}"
                )
            self.storage_legend.setText("&nbsp;&nbsp;&nbsp;".join(parts))

        installed = sum(1 for _name, ok in summary.components if ok)
        self.components_card.set_values(
            f"{installed} of {len(summary.components)} installed"
            if summary.components else "None available",
            "",
        )
        ok_color = token_color("success").name()
        off_color = token_color("slate-text-4").name()
        self.components_list.setText(
            "<br>".join(
                f'<span style="color:{ok_color if ok else off_color}">●</span>'
                f"&nbsp;&nbsp;{name}"
                for name, ok in summary.components
            )
        )
        self.footer_label.setText(summary.footer)
