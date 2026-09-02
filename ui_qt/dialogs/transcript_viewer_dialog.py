"""Reading window for a transcript, rendered as Markdown at a comfortable measure."""
from __future__ import annotations

from typing import Final, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ui_qt.utils.markdown_render import READER_STYLE, render_markdown
from ui_qt.widgets.eliding_label import ElidingLabel


class _ReaderView(QTextBrowser):
    """Text browser that keeps its column no wider than a comfortable measure.

    The document is laid out to the viewport, so a maximized window would run
    lines the full width of the screen. The view instead grows its viewport
    margins as it widens, centering a column capped at ``MAX_MEASURE`` pixels.
    """

    MAX_MEASURE: Final[int] = 780
    MIN_SIDE_MARGIN: Final[int] = 28
    TOP_MARGIN: Final[int] = 20
    BOTTOM_MARGIN: Final[int] = 32

    zoom_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setOpenExternalLinks(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def resizeEvent(self, event):
        side = max(self.MIN_SIDE_MARGIN, (self.width() - self.MAX_MEASURE) // 2)
        self.setViewportMargins(side, self.TOP_MARGIN, side, self.BOTTOM_MARGIN)
        super().resizeEvent(event)

    def wheelEvent(self, event):
        # QTextEdit's own Ctrl+wheel zoom changes the widget font, which the
        # Markdown renderer overrides on the next render; route it to the
        # dialog's zoom instead so the two stay in step.
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                self.zoom_requested.emit(1 if delta > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)


class TranscriptViewerDialog(QDialog):
    """Non-modal window that shows one transcript as formatted Markdown.

    The owning tab keeps a single instance and refreshes it through
    ``set_transcript`` as results arrive, so the window follows the tab's
    current transcript rather than freezing on the one it opened with. Text
    size and window geometry persist for the session because the dialog is
    hidden on close, not destroyed.
    """

    copy_requested = pyqtSignal(str)

    ZOOM_STEPS: Final[tuple[float, ...]] = (0.85, 1.0, 1.15, 1.3, 1.5, 1.75)
    DEFAULT_ZOOM_INDEX: Final[int] = 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fixed_text = ""
        self._raw_text: Optional[str] = None
        self._showing_raw = False
        self._zoom_index = self.DEFAULT_ZOOM_INDEX

        self.setObjectName("transcriptViewerDialog")
        self.setWindowTitle("Transcript")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.MSWindowsFixedSizeDialogHint, False)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setMinimumSize(560, 440)
        self.resize(940, 760)

        self._setup_ui()
        self._setup_shortcuts()

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        toolbar = QFrame()
        toolbar.setObjectName("transcriptViewerToolbar")
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(20, 12, 16, 12)
        bar.setSpacing(10)

        self.title_label = ElidingLabel("Transcript")
        self.title_label.setObjectName("transcriptViewerTitle")
        bar.addWidget(self.title_label, stretch=1)

        self.version_toggle = QWidget()
        self.version_toggle.setObjectName("transcriptSwitchRow")
        version_row = QHBoxLayout(self.version_toggle)
        version_row.setContentsMargins(0, 0, 0, 0)
        version_row.setSpacing(0)
        switch = QFrame()
        switch.setObjectName("transcriptSwitch")
        switch_layout = QHBoxLayout(switch)
        switch_layout.setContentsMargins(2, 2, 2, 2)
        switch_layout.setSpacing(2)
        self._version_group = QButtonGroup(self)
        self.fixed_btn = QPushButton("Fixed")
        self.raw_btn = QPushButton("Raw")
        for btn in (self.fixed_btn, self.raw_btn):
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setObjectName("transcriptSwitchBtn")
            btn.setFixedHeight(24)
            self._version_group.addButton(btn)
            switch_layout.addWidget(btn)
        version_row.addWidget(switch)
        self.fixed_btn.setChecked(True)
        self.fixed_btn.toggled.connect(self._on_version_toggled)
        self.raw_btn.toggled.connect(self._on_version_toggled)
        self.version_toggle.hide()
        bar.addWidget(self.version_toggle)

        self.zoom_out_btn = self._zoom_button("−", "Smaller text (Ctrl+-)")
        self.zoom_out_btn.clicked.connect(lambda: self.zoom(-1))
        self.zoom_in_btn = self._zoom_button("+", "Larger text (Ctrl++)")
        self.zoom_in_btn.clicked.connect(lambda: self.zoom(1))
        bar.addWidget(self.zoom_out_btn)
        bar.addWidget(self.zoom_in_btn)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setObjectName("transcriptViewerCopyButton")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setToolTip("Copy the transcript as text (Ctrl+C)")
        self.copy_btn.clicked.connect(self._copy_shown_text)
        bar.addWidget(self.copy_btn)

        outer.addWidget(toolbar)

        self.view = _ReaderView()
        self.view.setObjectName("transcriptViewerText")
        self.view.zoom_requested.connect(self.zoom)
        outer.addWidget(self.view, stretch=1)

        self._refresh_zoom_buttons()

    @staticmethod
    def _zoom_button(text: str, tooltip: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("transcriptViewerZoomButton")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tooltip)
        button.setFixedSize(28, 28)
        return button

    def _setup_shortcuts(self) -> None:
        for keys, handler in (
            (QKeySequence.StandardKey.Copy, self._copy_shown_text),
            (QKeySequence.StandardKey.ZoomIn, lambda: self.zoom(1)),
            (QKeySequence("Ctrl+="), lambda: self.zoom(1)),
            (QKeySequence.StandardKey.ZoomOut, lambda: self.zoom(-1)),
            (QKeySequence("Ctrl+0"), self.reset_zoom),
        ):
            shortcut = QShortcut(keys, self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(handler)

    def set_transcript(self, text: str, raw: Optional[str] = None, title: str = "") -> None:
        """Show ``text``, with ``raw`` behind the Raw switch when it differs."""
        self._fixed_text = text or ""
        self._raw_text = raw if raw and raw != text else None
        self.title_label.setText(title or "Transcript")
        self.setWindowTitle(f"Transcript — {title}" if title else "Transcript")
        self.version_toggle.setVisible(self._raw_text is not None)
        self.fixed_btn.blockSignals(True)
        self.raw_btn.blockSignals(True)
        self.fixed_btn.setChecked(True)
        self.raw_btn.setChecked(False)
        self.fixed_btn.blockSignals(False)
        self.raw_btn.blockSignals(False)
        self._showing_raw = False
        self._render()

    def clear(self) -> None:
        self.set_transcript("", None)

    def shown_text(self) -> str:
        if self._showing_raw and self._raw_text is not None:
            return self._raw_text
        return self._fixed_text

    @property
    def zoom_factor(self) -> float:
        return self.ZOOM_STEPS[self._zoom_index]

    def zoom(self, direction: int) -> None:
        index = max(0, min(len(self.ZOOM_STEPS) - 1, self._zoom_index + direction))
        if index == self._zoom_index:
            return
        self._zoom_index = index
        self._refresh_zoom_buttons()
        self._render(keep_position=True)

    def reset_zoom(self) -> None:
        if self._zoom_index == self.DEFAULT_ZOOM_INDEX:
            return
        self._zoom_index = self.DEFAULT_ZOOM_INDEX
        self._refresh_zoom_buttons()
        self._render(keep_position=True)

    def _refresh_zoom_buttons(self) -> None:
        self.zoom_out_btn.setEnabled(self._zoom_index > 0)
        self.zoom_in_btn.setEnabled(self._zoom_index < len(self.ZOOM_STEPS) - 1)

    def _on_version_toggled(self, checked: bool) -> None:
        if not checked:
            return
        self._showing_raw = self.raw_btn.isChecked()
        self._render()

    def _render(self, *, keep_position: bool = False) -> None:
        scrollbar = self.view.verticalScrollBar()
        fraction = 0.0
        if keep_position and scrollbar.maximum() > 0:
            fraction = scrollbar.value() / scrollbar.maximum()
        render_markdown(
            self.view.document(),
            self.shown_text(),
            READER_STYLE.scaled(self.zoom_factor),
        )
        self.copy_btn.setEnabled(bool(self.shown_text().strip()))
        if keep_position:
            scrollbar.setValue(round(fraction * scrollbar.maximum()))

    def _copy_shown_text(self) -> None:
        text = self.shown_text().strip()
        if text:
            self.copy_requested.emit(text)
