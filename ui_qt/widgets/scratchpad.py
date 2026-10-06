"""The floating Scratchpad: a notepad that dictation types into while it has focus.

Created on first use and kept on the UIController as ``ui._scratchpad``. Its
text autosaves to ``config.SCRATCHPAD_FILE`` (in Backup & restore). Never log
what it holds: counts and lengths only.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
from typing import Callable, Optional

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QKeySequence, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QSizeGrip,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from config import config
from services import text_rewrite
from services.settings import SettingsKey, resolve_scratchpad_always_on_top, settings_manager
from ui_qt.utils.desktop import compositor_managed, without_window_buttons
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.icons import design_icon
from ui_qt.widgets.eliding_label import ElidingLabel

logger = logging.getLogger(__name__)

AUTOSAVE_MS = 700
GEOMETRY_SAVE_MS = 500
NOTICE_MS = 2600
UNDO_CLEAR_MS = 8000
_CONTEXT_BEFORE = 1000
_CONTEXT_AFTER = 200
_NO_SPACE_AFTER = "([{\"'“‘/-—\n\t "
_NO_SPACE_BEFORE = ".,;:!?)]}%…'\"’”"


def toggle(ui) -> None:
    """Show and focus the Scratchpad, or hide it when it already has focus."""
    pad = getattr(ui, "_scratchpad", None)
    if pad is None:
        pad = ScratchpadWindow(copy_text=getattr(ui, "copy_to_clipboard", None))
        ui._scratchpad = pad
    if pad.isVisible() and pad.isActiveWindow():
        pad.hide()
    else:
        pad.present()


def insert(ui, text: str) -> bool:
    """Insert a finished dictation into the focused Scratchpad.

    Returns:
        True only when the Scratchpad took the text, so the caller skips the
        paste; False whenever the text should go to the app as usual.
    """
    pad = getattr(ui, "_scratchpad", None)
    if not isinstance(pad, ScratchpadWindow) or not text:
        return False
    if not pad.isVisible() or not pad.isActiveWindow():
        return False
    pad.insert_dictation(text)
    return True


def _write_atomically(path: str, text: str) -> None:
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".scratchpad-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class _Saver:
    """Writes the newest text off the Qt thread; a slow disk never stalls typing.

    Every text gets a sequence number and an older one is never written over a
    newer one, whether it came from the writer thread or ``write_now``.
    """

    def __init__(self, path: Callable[[], str]):
        self._path = path
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._sequence = 0
        self._written = 0
        self._pending: Optional[tuple[int, str]] = None
        self._busy = False

    def submit(self, text: str) -> None:
        with self._lock:
            self._sequence += 1
            self._pending = (self._sequence, text)
            if self._busy:
                return
            self._busy = True
        threading.Thread(target=self._run, name="scratchpad-save", daemon=True).start()

    def write_now(self, text: str) -> None:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            self._pending = None
        self._write(sequence, text)

    def idle(self) -> bool:
        with self._lock:
            return not self._busy

    def _run(self) -> None:
        while True:
            with self._lock:
                pending, self._pending = self._pending, None
                if pending is None:
                    self._busy = False
                    return
            self._write(*pending)

    def _write(self, sequence: int, text: str) -> None:
        with self._write_lock:
            if sequence <= self._written:
                return
            try:
                _write_atomically(self._path(), text)
            except OSError as exc:
                logger.warning("Couldn't save the Scratchpad (%s)", type(exc).__name__)
                return
            self._written = sequence


def _utf16_slice(encoded: bytes, start: int, end: int) -> str:
    """Text between two QTextDocument positions, which count UTF-16 units."""
    start, end = max(0, start), max(0, end)
    return encoded[start * 2:end * 2].decode("utf-16-le", errors="ignore")


def _spaced(text: str, before: str, after: str) -> str:
    """``text`` with the spaces it needs to sit between ``before`` and ``after``."""
    if not text:
        return text
    if before and before[-1] not in _NO_SPACE_AFTER and not text[0].isspace() \
            and text[0] not in _NO_SPACE_BEFORE:
        text = " " + text
    if after and after[0].isalnum() and not text[-1].isspace():
        text += " "
    return text


class _Header(QFrame):
    """The title strip of the frameless window; dragging it moves the window."""

    def __init__(self, window: QWidget):
        super().__init__(window)
        self._window = window
        self._drag_offset: Optional[QPoint] = None
        self.setObjectName("scratchpadHeader")
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self._window.frameGeometry().topLeft()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._window.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)


class ScratchpadWindow(QWidget):
    """A small always-handy notepad window.

    Classic desktops get a frameless, rounded tool window with its own drag
    header and resize grip; compositor-managed desktops (Wayland, Omarchy)
    get a normal window that the compositor places, sizes and stacks.
    """

    _transform_finished = pyqtSignal(int, object, str)

    def __init__(self, copy_text: Optional[Callable[[str], bool]] = None):
        super().__init__(None)
        self._copy_text = copy_text
        self._compositor = compositor_managed()
        self._loaded = False
        self._suppress_save = False
        self._cleared_text: Optional[str] = None
        self._transform_generation = 0
        self._transform_cursor: Optional[QTextCursor] = None
        self._transform_name = ""
        self._saver = _Saver(lambda: config.SCRATCHPAD_FILE)

        self.setObjectName("scratchpadWindow")
        self.setWindowTitle("Scratchpad")
        # Hidden in the tray, closing this must not quit the app.
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        # Dictation and the in-app shortcut fallback treat it as OpenWhisper's.
        self.setProperty("openwhisperHotkeyWindow", True)
        self._apply_window_flags(resolve_scratchpad_always_on_top())
        if sys.platform == "darwin":
            # A Tool window hides whenever another app is frontmost; the
            # Scratchpad sits beside the app being worked in.
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        if not self._compositor:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(AUTOSAVE_MS)
        self._autosave_timer.timeout.connect(self._save_soon)
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(GEOMETRY_SAVE_MS)
        self._geometry_timer.timeout.connect(self._save_geometry)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._show_word_count)
        self._undo_timer = QTimer(self)
        self._undo_timer.setSingleShot(True)
        self._undo_timer.setInterval(UNDO_CLEAR_MS)
        self._undo_timer.timeout.connect(self._forget_cleared)

        self._build()
        self._transform_finished.connect(self._on_transform_finished)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.flush)

    def _apply_window_flags(self, on_top: bool) -> None:
        if self._compositor:
            flags = without_window_buttons(Qt.WindowType.Window)
        else:
            flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            if on_top:
                flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.surface = QFrame()
        self.surface.setObjectName("scratchpadSurface")
        self.surface.setProperty("framed", not self._compositor)
        outer.addWidget(self.surface)
        layout = QVBoxLayout(self.surface)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        self.header = None
        self.on_top_button = None
        if not self._compositor:
            self.header = _Header(self)
            row = QHBoxLayout(self.header)
            row.setContentsMargins(14, 8, 8, 8)
            row.setSpacing(8)
            icon = QLabel()
            icon.setObjectName("scratchpadIcon")
            icon.setPixmap(design_icon("notes-blue.svg").pixmap(16, 16))
            row.addWidget(icon)
            title = QLabel("Scratchpad")
            title.setObjectName("scratchpadTitle")
            row.addWidget(title)
            row.addStretch(1)
            self.on_top_button = QToolButton()
            self.on_top_button.setObjectName("scratchpadOnTop")
            self.on_top_button.setText("On top")
            self.on_top_button.setCheckable(True)
            self.on_top_button.setChecked(resolve_scratchpad_always_on_top())
            self.on_top_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.on_top_button.setToolTip("Keep the Scratchpad above other windows")
            self.on_top_button.toggled.connect(self.set_always_on_top)
            row.addWidget(self.on_top_button)
            close = QToolButton()
            close.setObjectName("scratchpadClose")
            close.setIcon(design_icon("x-gray.svg"))
            close.setIconSize(QSize(14, 14))
            close.setCursor(Qt.CursorShape.PointingHandCursor)
            close.setToolTip("Hide the Scratchpad")
            close.setAccessibleName("Hide the Scratchpad")
            close.clicked.connect(self.hide)
            row.addWidget(close)
            layout.addWidget(self.header)

        self.editor = QPlainTextEdit()
        self.editor.setObjectName("scratchpadEditor")
        self.editor.setPlaceholderText("Dictate or type notes…")
        self.editor.setFrameShape(QFrame.Shape.NoFrame)
        self.editor.textChanged.connect(self._on_text_changed)
        layout.addWidget(self.editor, 1)

        footer = QFrame()
        footer.setObjectName("scratchpadFooter")
        row = QHBoxLayout(footer)
        row.setContentsMargins(14, 8, 6 if not self._compositor else 10, 8)
        row.setSpacing(2)
        self._footer_row = row
        self.status_label = ElidingLabel("")
        self.status_label.setObjectName("scratchpadStatus")
        row.addWidget(self.status_label, 1)
        self.transform_button = self._action("Transform ▾", "wand-purple.svg", self._open_transform_menu)
        self.transform_menu = QMenu(self)
        self.copy_button = self._action("Copy all", "copy-gray.svg", self.copy_all)
        self.clear_button = self._action("Clear", "", self._clear_or_undo)
        self.size_grip = None
        if not self._compositor:
            self.size_grip = QSizeGrip(footer)
            self.size_grip.setObjectName("scratchpadGrip")
            row.addWidget(self.size_grip, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        layout.addWidget(footer)
        self._show_word_count()

    def _action(self, text: str, icon: str, slot) -> QToolButton:
        button = QToolButton()
        button.setProperty("scratchpadAction", True)
        button.setText(text)
        if icon:
            button.setIcon(design_icon(icon))
            button.setIconSize(QSize(15, 15))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        button.clicked.connect(slot)
        self._footer_row.addWidget(button)
        return button

    # ---- showing, hiding, geometry ----

    def present(self) -> None:
        """Show, raise and focus the Scratchpad, loading its text the first time."""
        if not self._loaded:
            self._load()
            self._restore_geometry()
        self.show()
        self.raise_()
        self.activateWindow()
        self.editor.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def _default_rect(self, available: QRect) -> QRect:
        scale = current_ui_font_scale()
        width = min(round(400 * scale), available.width())
        height = min(round(380 * scale), available.height())
        return QRect(available.right() - width - 40, available.top() + (available.height() - height) // 2,
                     width, height)

    def _restore_geometry(self) -> None:
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 800)
        rect = self._default_rect(available)
        saved = settings_manager.get(SettingsKey.SCRATCHPAD_GEOMETRY)
        try:
            if isinstance(saved, dict) and {"x", "y", "width", "height"}.issubset(saved):
                rect = QRect(int(saved["x"]), int(saved["y"]), int(saved["width"]), int(saved["height"]))
        except (TypeError, ValueError):
            logger.warning("Ignoring an invalid Scratchpad geometry")
        minimum = self.minimumSizeHint()
        width = max(minimum.width(), min(rect.width(), available.width()))
        height = max(minimum.height(), min(rect.height(), available.height()))
        self.resize(width, height)
        if self._compositor:
            return
        x = min(max(rect.x(), available.left()), available.right() - width + 1)
        y = min(max(rect.y(), available.top()), available.bottom() - height + 1)
        self.move(x, y)

    def _save_geometry(self) -> None:
        geometry = self.geometry()
        value = {"x": geometry.x(), "y": geometry.y(), "width": geometry.width(), "height": geometry.height()}
        try:
            if settings_manager.get(SettingsKey.SCRATCHPAD_GEOMETRY) != value:
                settings_manager.save_setting(SettingsKey.SCRATCHPAD_GEOMETRY, value)
        except Exception:
            logger.warning("Couldn't save the Scratchpad position")

    def moveEvent(self, event):
        super().moveEvent(event)
        if self.isVisible():
            self._geometry_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.isVisible():
            self._geometry_timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._geometry_timer.isActive():
            self._geometry_timer.stop()
            self._save_geometry()
        self.flush()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def set_always_on_top(self, on: bool) -> None:
        """Keep the classic window above others; the compositor decides elsewhere."""
        on = bool(on)
        try:
            settings_manager.save_setting(SettingsKey.SCRATCHPAD_ALWAYS_ON_TOP, on)
        except Exception:
            logger.warning("Couldn't save the Scratchpad's on-top choice")
        if self._compositor:
            return
        if self.on_top_button is not None and self.on_top_button.isChecked() != on:
            self.on_top_button.setChecked(on)
        visible = self.isVisible()
        geometry = self.geometry()
        # Changing window flags recreates the native window, which hides it.
        self._apply_window_flags(on)
        self.setGeometry(geometry)
        if visible:
            self.show()
            self.raise_()
            self.activateWindow()

    # ---- text, autosave ----

    def _load(self) -> None:
        self._loaded = True
        try:
            with open(config.SCRATCHPAD_FILE, encoding="utf-8", errors="replace", newline="") as file:
                text = file.read().replace("\r\n", "\n")
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("Couldn't read the Scratchpad (%s)", type(exc).__name__)
            self._notice("Couldn't open your saved notes")
            return
        self._suppress_save = True
        try:
            self.editor.setPlainText(text)
        finally:
            self._suppress_save = False
        self.editor.moveCursor(QTextCursor.MoveOperation.End)
        self._show_word_count()

    def _on_text_changed(self) -> None:
        if not self._notice_timer.isActive():
            self._show_word_count()
        if self._suppress_save:
            return
        if self._cleared_text is not None and self.editor.toPlainText():
            self._forget_cleared()
        self._autosave_timer.start()

    def _save_soon(self) -> None:
        self._saver.submit(self.editor.toPlainText())

    def flush(self) -> None:
        """Write pending edits now (hiding, quitting)."""
        if not self._loaded:
            return
        if self._autosave_timer.isActive():
            self._autosave_timer.stop()
            self._saver.write_now(self.editor.toPlainText())

    def text(self) -> str:
        return self.editor.toPlainText()

    def word_count(self) -> int:
        return len(self.editor.toPlainText().split())

    def _show_word_count(self) -> None:
        count = self.word_count()
        self.status_label.setText("" if not count else "1 word" if count == 1 else f"{count:,} words")

    def _notice(self, text: str, ms: int = NOTICE_MS) -> None:
        self.status_label.setText(text)
        self._notice_timer.start(ms)

    def insert_dictation(self, text: str) -> None:
        """Type a finished dictation at the cursor, spaced to fit the text around it."""
        from services.focus_context import TextContext, join_with_context

        cursor = self.editor.textCursor()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        encoded = self.editor.toPlainText().encode("utf-16-le")
        before = _utf16_slice(encoded, start - _CONTEXT_BEFORE, start)
        after = _utf16_slice(encoded, end, end + _CONTEXT_AFTER)
        context = TextContext(
            before=before, selected=_utf16_slice(encoded, start, end), after=after,
            caret_known=True, selection_known=True, source="scratchpad",
        )
        try:
            joined = join_with_context(text, context)
        except Exception:
            logger.debug("Couldn't join the dictation to the Scratchpad text", exc_info=True)
            joined = text
        cursor.insertText(_spaced(joined, before, after))
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()

    # ---- footer actions ----

    def copy_all(self) -> None:
        text = self.editor.toPlainText()
        if not text:
            self._notice("Nothing to copy yet")
            return
        copied = self._copy_text(text) if callable(self._copy_text) else False
        self._notice("Copied" if copied else "Couldn't copy")

    def _clear_or_undo(self) -> None:
        if self._cleared_text is not None:
            self.undo_clear()
        else:
            self.clear_text()

    def clear_text(self) -> None:
        text = self.editor.toPlainText()
        if not text:
            return
        cursor = self.editor.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()
        self._cleared_text = text
        self.clear_button.setText("Undo clear")
        self._undo_timer.start()

    def undo_clear(self) -> None:
        text, self._cleared_text = self._cleared_text, None
        self._forget_cleared()
        if text:
            cursor = self.editor.textCursor()
            cursor.select(QTextCursor.SelectionType.Document)
            cursor.insertText(text)
            self.editor.setTextCursor(cursor)

    def _forget_cleared(self) -> None:
        self._cleared_text = None
        self._undo_timer.stop()
        self.clear_button.setText("Clear")

    def _open_transform_menu(self) -> None:
        self.fill_transform_menu()
        self.transform_menu.popup(self.transform_button.mapToGlobal(QPoint(0, self.transform_button.height())))

    def fill_transform_menu(self) -> None:
        self.transform_menu.clear()
        try:
            from services.text_transforms import load_transforms

            transforms = load_transforms(settings_manager.load_all_settings())
        except Exception:
            logger.exception("Couldn't load the transforms")
            transforms = []
        if not transforms:
            self.transform_menu.addAction("No transforms yet").setEnabled(False)
            return
        for transform in transforms:
            action = self.transform_menu.addAction(transform.name)
            action.triggered.connect(lambda _checked=False, transform=transform: self.apply_transform(transform))

    def apply_transform(self, transform) -> None:
        """Rewrite the selection, or everything, with a saved transform."""
        cursor = QTextCursor(self.editor.textCursor())
        if not cursor.hasSelection():
            cursor.select(QTextCursor.SelectionType.Document)
        text = cursor.selectedText().replace(" ", "\n")
        if not text.strip():
            self._notice("Write or dictate something to transform")
            return
        self._transform_generation += 1
        generation = self._transform_generation
        self._transform_cursor = cursor
        self.editor.setReadOnly(True)
        self.transform_button.setEnabled(False)
        self._transform_name = transform.name
        self.status_label.setText(f"Applying {transform.name}…")
        self._notice_timer.stop()
        settings = settings_manager.load_all_settings()
        instruction = transform.instruction
        logger.info("Scratchpad transform started (%d characters)", len(text))

        def work() -> None:
            result, error = "", ""
            try:
                result, message = text_rewrite.rewrite_standalone(text, instruction, settings)
                error = message or ""
            except Exception as exc:
                # Expected failures come back as the message; this is a bug,
                # and its text could quote the draft.
                logger.warning("Scratchpad transform failed (%s)", type(exc).__name__)
                result, error = "", "Couldn't transform the text"
            try:
                self._transform_finished.emit(generation, result, error)
            except RuntimeError:
                pass  # The window was destroyed while the rewrite ran.

        threading.Thread(target=work, name="scratchpad-transform", daemon=True).start()

    def _on_transform_finished(self, generation: int, result, error: str) -> None:
        if generation != self._transform_generation:
            return
        cursor, self._transform_cursor = self._transform_cursor, None
        self.editor.setReadOnly(False)
        self.transform_button.setEnabled(True)
        if error or not isinstance(result, str) or not result.strip():
            self._notice(error or "The transform returned nothing")
            return
        if cursor is not None:
            cursor.insertText(result)
            self.editor.setTextCursor(cursor)
        undo = QKeySequence(QKeySequence.StandardKey.Undo).toString(QKeySequence.SequenceFormat.NativeText)
        name = self._transform_name
        self._notice(f"{name} applied · {undo} to undo" if undo else f"{name} applied")
