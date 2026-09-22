"""Search across the Settings window: destinations, tiles, help, and models.

The index is rebuilt from the live widget tree every time the palette opens,
so it always reflects current values and never needs its own copy of the
settings copy. Results say where they live ("Dictation › Voice model"), which
makes the question "which part of the app owns this?" irrelevant.
"""
import html
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from PyQt6.QtCore import QEvent, QObject, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QKeySequence, QPainter
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config import bundle_root
from ui_qt.utils.palette import token_color
from ui_qt.widgets.setting_tile import TileBase

SETTING = "setting"
MODEL = "model"
HELP = "help"

SECTION_TITLES = {
    SETTING: "Settings",
    MODEL: "Models & downloads",
    HELP: "Help text",
}
#: Most results shown per section, so one section cannot bury the others.
SECTION_LIMITS = {SETTING: 6, MODEL: 5, HELP: 3}

_ENTRY_ROLE = Qt.ItemDataRole.UserRole


def _icon(filename: str) -> QIcon:
    return QIcon(str(Path(bundle_root()) / "ui_qt" / "assets" / "tabler" / filename))


@dataclass
class SearchEntry:
    """One thing the palette can find and open."""

    kind: str
    title: str
    detail: str
    destination: str
    target: Optional[QWidget] = None
    model_name: str = ""
    component_id: str = ""
    badges: Tuple[str, ...] = ()
    keywords: str = ""
    icon: str = "box-blue.svg"

    def haystack(self) -> str:
        return f"{self.title} {self.detail} {self.keywords}".casefold()


@dataclass
class PageSource:
    """A Settings destination the index walks."""

    key: str
    crumb: str
    title: str
    subtitle: str
    widget: QWidget
    icon: str


def _tokens(query: str) -> List[str]:
    return [token for token in query.casefold().split() if token]


def _score(entry: SearchEntry, query: str, tokens: Sequence[str]) -> int:
    title = entry.title.casefold()
    if title.startswith(query):
        return 0
    if query in title:
        return 1
    if all(token in title for token in tokens):
        return 2
    return 3


def match_entries(entries: Iterable[SearchEntry], query: str) -> List[SearchEntry]:
    """Entries containing every query word, best first, capped per section."""
    query = " ".join(query.split()).casefold()
    tokens = _tokens(query)
    if not tokens:
        return []
    hits = [entry for entry in entries if all(t in entry.haystack() for t in tokens)]
    hits.sort(key=lambda entry: (_score(entry, query, tokens), len(entry.title)))
    ordered: List[SearchEntry] = []
    for kind in (SETTING, MODEL, HELP):
        ordered.extend([entry for entry in hits if entry.kind == kind][: SECTION_LIMITS[kind]])
    return ordered


def _snippet(text: str, tokens: Sequence[str], width: int = 110) -> str:
    """The part of a long help text around its first match."""
    if len(text) <= width:
        return text
    folded = text.casefold()
    first = min((folded.find(t) for t in tokens if t in folded), default=0)
    start = max(0, first - width // 3)
    end = min(len(text), start + width)
    snippet = text[start:end].strip()
    return ("…" if start else "") + snippet + ("…" if end < len(text) else "")


def highlight(text: str, tokens: Sequence[str], color: str) -> str:
    """HTML for ``text`` with every query word tinted."""
    folded = text.casefold()
    marks = [False] * len(text)
    for token in tokens:
        start = folded.find(token)
        while start >= 0 and token:
            for index in range(start, start + len(token)):
                marks[index] = True
            start = folded.find(token, start + len(token))
    out, run, marked = [], "", False
    for char, flag in zip(text, marks):
        if flag != marked and run:
            out.append(_wrap(run, marked, color))
            run = ""
        run += char
        marked = flag
    if run:
        out.append(_wrap(run, marked, color))
    return "".join(out)


def _wrap(text: str, marked: bool, color: str) -> str:
    escaped = html.escape(text)
    if not marked:
        return escaped
    return f'<span style="color:{color}; font-weight:700">{escaped}</span>'


def _control_value(widget: QWidget) -> str:
    combo = widget.findChild(QComboBox)
    if combo is not None and combo.currentText():
        return combo.currentText()
    return ""


def build_index(
    pages: Sequence[PageSource],
    downloads=None,
    extra: Sequence[SearchEntry] = (),
) -> List[SearchEntry]:
    """Walk every destination (and the model catalog) into search entries."""
    entries: List[SearchEntry] = []
    for page in pages:
        entries.append(SearchEntry(
            SETTING, page.title, f"{page.crumb} · {page.subtitle}",
            page.key, icon=page.icon,
        ))
        seen = set()
        for tile in page.widget.findChildren(TileBase):
            if tile.isHidden():
                continue
            title = tile.title_label.text().strip()
            if not title or (title, page.key) in seen:
                continue
            seen.add((title, page.key))
            entries.append(SearchEntry(
                SETTING, title, page.crumb, page.key, target=tile,
                keywords=tile.description_label.text(), icon=page.icon,
            ))
        for group in page.widget.findChildren(QWidget, "modelManagerFieldGroup"):
            if group.isHidden():
                continue
            caption = group.findChild(QLabel, "textModelFieldLabel")
            if caption is None or not caption.text().strip():
                continue
            value = _control_value(group)
            title = f"{caption.text().strip()}: {value}" if value else caption.text().strip()
            entries.append(SearchEntry(
                SETTING, title, page.crumb, page.key, target=group, icon=page.icon,
            ))
        for note in page.widget.findChildren(QLabel, "textModelFootnote"):
            entries.append(SearchEntry(
                HELP, page.title, note.text(), page.key,
                target=note.parentWidget(), icon="info-blue.svg",
            ))
    if downloads is not None:
        entries.extend(_catalog_entries(downloads))
    entries.extend(extra)
    return entries


def _catalog_entries(downloads) -> List[SearchEntry]:
    from services.local_asr.catalog import BACKENDS, MODELS
    entries = []
    for name, row in downloads.rows.items():
        label = MODELS[name].label if name in MODELS else name
        backend = BACKENDS.get(row.backend, "Whisper")
        size = row.size_label.text()
        badges = tuple(
            badge for badge in (
                row.usage_label.text() if row.usage_label.isVisibleTo(row) else "",
                "Downloaded" if row.is_cached else "",
            ) if badge
        )
        detail = " · ".join(part for part in ("Downloads", size, row.repo_label.text()) if part)
        entries.append(SearchEntry(
            MODEL, label, detail, "downloads", model_name=name, badges=badges,
            keywords=f"{name} {backend} model", icon="download-blue.svg",
        ))
    from services.components import component_coordinator
    for component_id, row in downloads._component_rows.items():
        try:
            info = component_coordinator.describe(component_id)
            title, summary, usable = info.display_name, info.summary, info.is_usable
        except Exception:
            title, summary, usable = row.name_label.text(), "", False
        entries.append(SearchEntry(
            MODEL, title, f"Downloads › Components · {summary}".rstrip(" ·"),
            "downloads", component_id=component_id,
            badges=("Installed",) if usable else (),
            keywords="component runtime add-on", icon="box-blue.svg",
        ))
    return entries


class _ResultRow(QWidget):
    def __init__(self, entry: SearchEntry, tokens: Sequence[str], parent=None):
        super().__init__(parent)
        self.setObjectName("settingsSearchRow")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        color = token_color("accent-soft").name()
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 7, 12, 7)
        row.setSpacing(12)
        glyph = QLabel()
        glyph.setObjectName("settingsSearchIcon")
        glyph.setFixedSize(28, 28)
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        glyph.setPixmap(_icon(entry.icon).pixmap(15, 15))
        row.addWidget(glyph)
        copy = QVBoxLayout()
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(1)
        title = QLabel(highlight(entry.title, tokens, color))
        title.setObjectName("settingsSearchTitle")
        title.setTextFormat(Qt.TextFormat.RichText)
        copy.addWidget(title)
        detail_text = _snippet(entry.detail, tokens) if entry.kind == HELP else entry.detail
        detail = QLabel(highlight(detail_text, tokens, color))
        detail.setObjectName("settingsSearchDetail")
        detail.setTextFormat(Qt.TextFormat.RichText)
        copy.addWidget(detail)
        row.addLayout(copy, stretch=1)
        for badge in entry.badges:
            pill = QLabel(badge)
            pill.setObjectName("settingsSearchBadge")
            pill.setProperty("tone", "ok" if badge in ("Downloaded", "Installed") else "use")
            row.addWidget(pill)


class SearchPalette(QWidget):
    """A command-palette overlay that covers the Settings window."""

    activated = pyqtSignal(object)

    PANEL_WIDTH = 640
    RESULTS_MAX_HEIGHT = 380

    def __init__(self, host: QWidget, index: Callable[[], List[SearchEntry]]):
        super().__init__(host)
        self.setObjectName("settingsSearchOverlay")
        self._host = host
        self._index_provider = index
        self._entries: List[SearchEntry] = []
        self.hide()

        self.panel = QFrame(self)
        self.panel.setObjectName("settingsSearchPanel")
        column = QVBoxLayout(self.panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        self.input = QLineEdit()
        self.input.setObjectName("settingsSearchInput")
        self.input.setPlaceholderText("Search settings, models, and help")
        self.input.addAction(_icon("search-slate.svg"), QLineEdit.ActionPosition.LeadingPosition)
        self.input.textChanged.connect(self._run_query)
        self.input.installEventFilter(self)
        column.addWidget(self.input)

        self.results = QListWidget()
        self.results.setObjectName("settingsSearchResults")
        self.results.setFrameShape(QFrame.Shape.NoFrame)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.results.itemClicked.connect(self._activate_item)
        column.addWidget(self.results)

        self.empty_label = QLabel("")
        self.empty_label.setObjectName("settingsSearchEmpty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        column.addWidget(self.empty_label)

        footer = QWidget()
        footer.setObjectName("settingsSearchFooter")
        hints = QHBoxLayout(footer)
        hints.setContentsMargins(16, 8, 16, 9)
        hints.setSpacing(16)
        for text in ("↑ ↓  move", "Enter  open", "Esc  close"):
            label = QLabel(text)
            label.setObjectName("settingsSearchHint")
            hints.addWidget(label)
        hints.addStretch()
        self.count_label = QLabel("")
        self.count_label.setObjectName("settingsSearchHint")
        hints.addWidget(self.count_label)
        column.addWidget(footer)

        host.installEventFilter(self)

    # ---- opening and closing ----

    def open(self, text: str = "") -> None:
        self._entries = self._index_provider()
        self._place()
        self.show()
        self.raise_()
        self.input.setText(text)
        self.input.selectAll()
        self._run_query(self.input.text())
        self.input.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def close_palette(self) -> None:
        self.hide()
        self._host.setFocus(Qt.FocusReason.OtherFocusReason)

    def _place(self) -> None:
        self.setGeometry(self._host.rect())
        width = min(self.PANEL_WIDTH, max(360, self.width() - 80))
        self.panel.setFixedWidth(width)
        self.panel.adjustSize()
        self.panel.move((self.width() - width) // 2, 58)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._host and event.type() == QEvent.Type.Resize and self.isVisible():
            self._place()
        elif watched is self.input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self.close_palette()
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                item = self.results.currentItem()
                if item is not None:
                    self._activate_item(item)
                return True
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self._move(1 if key == Qt.Key.Key_Down else -1)
                return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event) -> None:
        if not self.panel.geometry().contains(event.position().toPoint()):
            self.close_palette()
            return
        super().mousePressEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        scrim = QColor(token_color("slate-rail"))
        scrim.setAlpha(170)
        painter.fillRect(self.rect(), scrim)
        painter.end()

    # ---- results ----

    def current_entries(self) -> List[SearchEntry]:
        """The entries now listed, in order (headers excluded)."""
        entries = []
        for index in range(self.results.count()):
            entry = self.results.item(index).data(_ENTRY_ROLE)
            if entry is not None:
                entries.append(entry)
        return entries

    def _run_query(self, text: str) -> None:
        tokens = _tokens(text)
        matches = match_entries(self._entries, text)
        self.results.clear()
        current_kind = None
        first = None
        for entry in matches:
            if entry.kind != current_kind:
                current_kind = entry.kind
                header = QListWidgetItem(self.results)
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                label = QLabel(SECTION_TITLES[entry.kind].upper())
                label.setObjectName("settingsSearchSection")
                header.setSizeHint(QSize(0, label.sizeHint().height() + 12))
                self.results.setItemWidget(header, label)
            item = QListWidgetItem(self.results)
            item.setData(_ENTRY_ROLE, entry)
            row = _ResultRow(entry, tokens)
            item.setSizeHint(QSize(0, row.sizeHint().height()))
            self.results.setItemWidget(item, row)
            first = first or item
        if first is not None:
            self.results.setCurrentItem(first)
        has_query = bool(tokens)
        self.results.setVisible(bool(matches))
        self.empty_label.setVisible(not matches)
        self.empty_label.setText(
            f'Nothing matches "{text.strip()}".' if has_query
            else "Type to search every setting, model, and help note."
        )
        noun = "result" if len(matches) == 1 else "results"
        self.count_label.setText(f"{len(matches)} {noun}" if has_query else "")
        height = sum(
            self.results.sizeHintForRow(index) + 2 * self.results.spacing()
            for index in range(self.results.count())
        )
        self.results.setFixedHeight(min(self.RESULTS_MAX_HEIGHT, height + 12))
        self.panel.adjustSize()

    def _move(self, step: int) -> None:
        count = self.results.count()
        if not count:
            return
        index = self.results.currentRow()
        for _ in range(count):
            index = (index + step) % count
            item = self.results.item(index)
            if item.data(_ENTRY_ROLE) is not None:
                self.results.setCurrentItem(item)
                self.results.scrollToItem(item)
                return

    def _activate_item(self, item: QListWidgetItem) -> None:
        entry = item.data(_ENTRY_ROLE)
        if entry is None:
            return
        self.close_palette()
        self.activated.emit(entry)


def shortcut_text(sequence: str = "Ctrl+K") -> str:
    """The platform's spelling of a shortcut, for the rail hint."""
    return QKeySequence(sequence).toString(QKeySequence.SequenceFormat.NativeText)


def keyword_entries(entries: Dict[str, Tuple[str, str, str]]) -> List[SearchEntry]:
    """Alias entries: ``{destination: (title, detail, keywords)}``."""
    return [
        SearchEntry(SETTING, title, detail, destination, keywords=keywords)
        for destination, (title, detail, keywords) in entries.items()
    ]
