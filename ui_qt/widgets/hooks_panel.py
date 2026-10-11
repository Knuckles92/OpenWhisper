"""Settings → Automate → Hooks: webhooks, programs and text filters.

Each section lists its saved items with an editor that opens under the item
being edited. Saving goes through ``manager.mutate_settings`` with the models
in services/automation/models.py. The engines (webhooks, programs, filters)
answer for secrets, tests, recent deliveries and leases; their slow calls run
on a worker or a Future and come back through a session-lived relay, so no
other thread ever holds or emits on a widget.
"""

from __future__ import annotations

import itertools
import os
import threading
import time
from dataclasses import replace
from datetime import datetime
from typing import Callable, Optional
from urllib.parse import urlsplit

from PyQt6.QtCore import QEvent, QObject, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.automation import catalog, models
from services.automation import filters as filter_engine
from services.automation import programs as program_engine
from services.automation import webhooks as webhook_engine
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets.buttons import Button, compact_primary_button, neutral_button
from ui_qt.widgets.eliding_label import ElidingLabel
from ui_qt.widgets.engine_field import EngineStatus, StatusDot
from ui_qt.widgets.no_wheel import NoWheelSpinBox
from ui_qt.widgets.segmented_bar import SegmentedBar
from ui_qt.widgets.settings_switch import SettingsSwitch
from ui_qt.widgets.wrapped_label import WrappedLabel

#: Friendly names for event types, in the order the chooser offers them.
EVENT_LABELS: dict[str, str] = {
    catalog.TRANSCRIPTION_COMPLETED: "Finished dictations",
    catalog.HISTORY_SAVED: "Saved to history",
    catalog.ACTION_RAN: "Actions that ran",
    catalog.RECORDING_STARTED: "Recording started",
    catalog.RECORDING_STOPPED: "Recording stopped",
    catalog.RECORDING_CANCELED: "Recording canceled",
    catalog.TRANSCRIPTION_FAILED: "Transcription failed",
    catalog.TRANSCRIPTION_PREVIEW: "Live preview text",
    catalog.ROUTE_DECIDED: "Spoken routing",
    catalog.FILTER_FAILED: "Text filter problems",
    catalog.CHANNEL_MESSAGE: "Claude Code channel",
    catalog.MEETING_STARTED: "Meeting started",
    catalog.MEETING_ENDED: "Meeting ended",
    catalog.MEETING_SEGMENTS: "Meeting transcript lines",
    catalog.MEETING_VOICE_COMMAND: "Meeting voice commands",
}
#: What a new webhook or program sends until the user picks something else.
DEFAULT_EVENTS: tuple[str, ...] = (catalog.TRANSCRIPTION_COMPLETED,)
#: Recent deliveries or runs shown in an editor.
RECENT_ROWS = 5
#: How often a visible section re-reads deliveries, runs and leases (memory only).
LIVE_INTERVAL_MS = 1000

STAGE_LABELS = {
    models.BEFORE_CLEANUP: "Before AI cleanup",
    models.AFTER_CLEANUP: "After AI cleanup",
}


def event_label(event_type: str) -> str:
    if event_type.endswith(".test"):
        return "Test"
    return EVENT_LABELS.get(event_type, event_type)


def event_types() -> list[str]:
    """Every public event type: the friendly ones first, any newer ones after."""
    ordered = [t for t in EVENT_LABELS if t in catalog.PUBLIC_TYPES]
    return ordered + [t for t in catalog.PUBLIC_TYPES if t not in ordered]


def events_summary(events) -> str:
    labels = [event_label(t) for t in events]
    if not labels:
        return "No events"
    return labels[0] if len(labels) == 1 else f"{labels[0]} +{len(labels) - 1}"


def clock(at: str) -> str:
    """``HH:MM:SS`` in local time for an ISO-8601 instant."""
    try:
        moment = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return str(at or "")
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%H:%M:%S")


def delivery_result(record) -> tuple[str, bool]:
    """``("200 · 120 ms", True)``, or the error, for a webhook DeliveryRecord."""
    if record.status is not None:
        text = f"{record.status} · {int(record.elapsed_ms)} ms"
    else:
        text = record.error or "No answer"
    if getattr(record, "attempt", 1) > 1:
        text += f" · try {record.attempt}"
    return text, bool(record.ok)


def run_result(record) -> tuple[str, bool]:
    """``("Exit 0 · 340 ms", True)``, or the error, for a program RunRecord."""
    if record.exit_code is not None:
        return f"Exit {record.exit_code} · {int(record.elapsed_ms)} ms", bool(record.ok)
    return record.error or "Didn't run", bool(record.ok)


def unencrypted_note(url: str) -> str:
    """A gentle note for plain http to a host outside this computer and network."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    if parts.scheme != "http" or not parts.hostname or models.url_is_private(url.strip()):
        return ""
    return ("Sent unencrypted. That's fine on your own network; "
            "use https:// for anything farther away.")


# ---- answers from other threads ----------------------------------------------

class _EngineRelay(QObject):
    """Brings engine answers to the Qt thread for whichever editors exist then.

    It lives for the session, so a worker or Future callback never holds or
    emits on a widget; Qt drops a deleted widget's connection.
    """

    answered = pyqtSignal(int, object, object)  # token, result, error text or None


_relay: Optional[_EngineRelay] = None
#: Request tokens, unique across widgets because they share the relay.
_tokens = itertools.count(1)


def _engine_relay() -> _EngineRelay:
    global _relay
    if _relay is None:
        _relay = _EngineRelay()
    return _relay


def _error_text(exc: BaseException) -> str:
    return str(exc).strip() or type(exc).__name__


class _Requests:
    """Callbacks waiting for relay answers, owned by one widget."""

    def __init__(self, owner: QObject):
        self._waiting: dict[int, Callable] = {}
        _engine_relay().answered.connect(owner._on_engine_answer)

    def expect(self, callback: Callable[[object, Optional[str]], None]) -> int:
        token = next(_tokens)
        self._waiting[token] = callback
        return token

    def answer(self, token: int, result, error) -> None:
        callback = self._waiting.pop(token, None)
        if callback is not None:
            callback(result, error)

    def forget(self) -> None:
        """Drop the answers still on their way; they arrive to nothing."""
        self._waiting.clear()

    def in_background(self, call: Callable[[], object], name: str,
                      callback: Callable[[object, Optional[str]], None]) -> None:
        """Run ``call()`` on a worker; ``callback(result, error)`` runs on the Qt thread."""
        token = self.expect(callback)
        answered = _engine_relay().answered

        def worker() -> None:
            try:
                result = call()
            except Exception as exc:
                answered.emit(token, None, _error_text(exc))
            else:
                answered.emit(token, result, None)

        threading.Thread(target=worker, name=name, daemon=True).start()

    def when_done(self, start: Callable[[], object],
                  callback: Callable[[object, Optional[str]], None]) -> None:
        """``start()`` returns a Future; its outcome reaches ``callback`` on the Qt thread."""
        token = self.expect(callback)
        answered = _engine_relay().answered
        try:
            future = start()
        except Exception as exc:
            self.answer(token, None, _error_text(exc))
            return

        def done(finished) -> None:
            try:
                result = finished.result()
            except Exception as exc:
                answered.emit(token, None, _error_text(exc))
            else:
                answered.emit(token, result, None)

        future.add_done_callback(done)


# ---- small pieces -------------------------------------------------------------

class _FlowLayout(QLayout):
    """Lays children out left to right and wraps them, each at its own width."""

    def __init__(self, parent=None, *, spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self._gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, move=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect: QRect, *, move: bool) -> int:
        x, y, line = rect.x(), rect.y(), 0
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            if x > rect.x() and x + hint.width() > rect.right() + 1:
                x, y, line = rect.x(), y + line + self._gap, 0
            if move:
                item.setGeometry(QRect(x, y, min(hint.width(), rect.width()), hint.height()))
            x += hint.width() + self._gap
            line = max(line, hint.height())
        return y + line - rect.y()


class EventChooser(QWidget):
    """The events an item sends, as toggle chips that wrap like tags."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("hooksEventChooser")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = _FlowLayout(self, spacing=6)
        self.chips: dict[str, QPushButton] = {}
        for event_type in event_types():
            chip = QPushButton(event_label(event_type))
            chip.setObjectName("hooksEventChip")
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            description = catalog.EVENT_TYPES.get(event_type, "")
            chip.setToolTip(description)
            chip.setAccessibleName(f"Send {event_label(event_type)}")
            chip.setAccessibleDescription(description)
            chip.toggled.connect(self.changed)
            layout.addWidget(chip)
            self.chips[event_type] = chip

    def events(self) -> tuple[str, ...]:
        return tuple(t for t, chip in self.chips.items() if chip.isChecked())

    def set_events(self, events) -> None:
        wanted = set(events)
        for event_type, chip in self.chips.items():
            blocked = chip.blockSignals(True)
            chip.setChecked(event_type in wanted)
            chip.blockSignals(blocked)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self.layout().heightForWidth(width)

    def sizeHint(self):
        width = self.width() if self.width() > 0 else round(420 * current_ui_font_scale())
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self):
        return QSize(0, self.heightForWidth(max(self.width(), 1)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self.updateGeometry()


class StatusChip(QLabel):
    """A short, honest result: "Sending…", "200 · 120 ms", or the error."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("hooksChip")
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.hide()

    def show_result(self, text: str, tone: str) -> None:
        self.setText(text)
        self.setToolTip(text)
        set_style_property(self, "tone", tone)
        self.setVisible(bool(text))


class RecentList(QWidget):
    """The newest deliveries or runs: time, what, result."""

    def __init__(self, empty_text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("hooksRecent")
        self._empty_text = empty_text
        self._shown = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self.set_rows([])

    def set_rows(self, rows) -> None:
        """``rows``: ``(time, what, result, ok)`` tuples, newest first."""
        rows = tuple(rows)[:RECENT_ROWS]
        if rows == self._shown:
            return
        self._shown = rows
        while self._layout.count():
            widget = self._layout.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        if not rows:
            empty = QLabel(self._empty_text)
            empty.setObjectName("hooksRecentEmpty")
            self._layout.addWidget(empty)
            return
        for at, what, result, ok in rows:
            line = QWidget()
            line.setObjectName("hooksRecentRow")
            row = QHBoxLayout(line)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(10)
            stamp = QLabel(at)
            stamp.setObjectName("hooksRecentTime")
            row.addWidget(stamp)
            label = ElidingLabel(what)
            label.setObjectName("hooksRecentWhat")
            row.addWidget(label, 1)
            outcome = ElidingLabel(result)
            outcome.setObjectName("hooksRecentResult")
            outcome.setProperty("tone", "ok" if ok else "error")
            row.addWidget(outcome, 0, Qt.AlignmentFlag.AlignRight)
            self._layout.addWidget(line)


class _AdaptiveButtons(QWidget):
    """Buttons in a row while they fit, stacked when the column is narrow.

    ``None`` in ``items`` is a flexible gap that only exists side by side.
    """

    def __init__(self, items, *, spacing: int = 8, parent=None):
        super().__init__(parent)
        self.setObjectName("hooksButtonRow")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._widgets = [item for item in items if item is not None]
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(spacing)
        self._gaps = []
        for item in items:
            if item is None:
                self._layout.addStretch(1)
                self._gaps.append(self._layout.count() - 1)
            else:
                self._layout.addWidget(item)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.reflow)

    def reflow(self) -> None:
        shown = [w for w in self._widgets if not w.isHidden()]
        needed = sum(max(w.sizeHint().width(), w.minimumWidth()) for w in shown)
        needed += self._layout.spacing() * max(0, len(shown) - 1)
        narrow = self.width() < needed
        direction = QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight
        if self._layout.direction() != direction:
            self._layout.setDirection(direction)
        for index in self._gaps:
            self._layout.setStretch(index, 0 if narrow else 1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()

    def event(self, event):
        if event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.FontChange,
                            QEvent.Type.StyleChange) and hasattr(self, "_timer"):
            self._timer.start(0)
        return super().event(event)


def _restyle(widget: QWidget, name: str, value) -> None:
    """Set a style property, repolishing only when it changes (live refreshes are frequent)."""
    if widget.property(name) != value:
        set_style_property(widget, name, value)


def _field_label(text: str, buddy: QWidget) -> QLabel:
    label = QLabel(text)
    label.setObjectName("textModelFieldLabel")
    label.setBuddy(buddy)
    return label


def _hint(text: str = "") -> WrappedLabel:
    label = WrappedLabel(text)
    label.setObjectName("hooksHint")
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _small_button(text: str, *, primary: bool = False) -> Button:
    button = Button(text)
    (compact_primary_button if primary else neutral_button)(button)
    button.set_base_minimum_size(0, 34)
    return button


def _choose_program(parent: QWidget, current: str) -> str:
    start = os.path.dirname(current) if current else os.path.expanduser("~")
    path, _filter = QFileDialog.getOpenFileName(parent, "Choose a program", start)
    return os.path.abspath(path) if path else ""


# ---- editors ------------------------------------------------------------------

class _Editor(QFrame):
    """Fields for one item, with Save, Cancel and Delete.

    ``source`` is the item being edited (saved, or the blank a new one starts
    from); it supplies the id and the on/off state, which the editor doesn't
    show. ``baseline`` is ``source`` as the fields read it back, so an
    untouched editor never reads as unsaved.
    """

    save_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    delete_requested = pyqtSignal()

    NOUN = "item"
    NAME_PLACEHOLDER = ""

    #: Set by the section: saves the draft first and returns whether it did.
    save_first: Optional[Callable[[], bool]] = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("hooksEditor")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.source = None
        self.baseline = None
        self.saved = False
        self._requests = _Requests(self)
        layout = self._form = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)
        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(models.NAME_MAX_CHARS)
        self.name_edit.setPlaceholderText(self.NAME_PLACEHOLDER)
        self.name_edit.setAccessibleName(f"{self.NOUN.capitalize()} name")
        layout.addWidget(_field_label("Name", self.name_edit))
        layout.addWidget(self.name_edit)
        self._build_fields(layout)
        self.message = WrappedLabel("")
        self.message.setObjectName("hooksMessage")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.hide()
        layout.addSpacing(4)
        layout.addWidget(self.message)
        self.delete_button = _small_button("Delete")
        self.delete_button.setAccessibleName(f"Delete this {self.NOUN}")
        self.delete_button.clicked.connect(self.delete_requested)
        self.cancel_button = _small_button("Cancel")
        self.cancel_button.setToolTip("Close without saving")
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.save_button = _small_button(f"Save {self.NOUN}", primary=True)
        self.save_button.clicked.connect(self.save_requested)
        self.footer = _AdaptiveButtons(
            [self.delete_button, None, self.cancel_button, self.save_button]
        )
        layout.addWidget(self.footer)

    # Subclasses add their fields here, between the name and the footer.
    def _build_fields(self, layout: QVBoxLayout) -> None:
        raise NotImplementedError

    def _show_fields(self, item) -> None:
        raise NotImplementedError

    def draft(self):
        raise NotImplementedError

    def show_item(self, item, *, saved: bool) -> None:
        self._start_over()
        self.source = item
        self.saved = saved
        self.name_edit.setText(item.name)
        self._show_fields(item)
        # Compared through the editor, so text it trims never reads as an edit.
        self.baseline = self.draft()
        self.delete_button.setVisible(saved)
        self.set_message("")
        self.refresh_live()

    def set_enabled(self, enabled: bool) -> None:
        """The row's switch changed the saved item; keep the draft in step."""
        if self.source is not None:
            self.source = replace(self.source, enabled=enabled)
            self.baseline = replace(self.baseline, enabled=enabled)

    def clear(self) -> None:
        self._start_over()
        self.source = self.baseline = None
        self.saved = False

    def _start_over(self) -> None:
        """One editor serves every item, and a test or secret still on its way
        was asked for what it showed before (or before a save changed it), so
        its answer is dropped rather than shown, or copied, here."""
        self._requests.forget()
        self._show_idle()

    def _show_idle(self) -> None:
        """Buttons and results as they are with nothing on its way."""

    def item_id(self) -> str:
        return self.source.id if self.source is not None else ""

    def has_unsaved_changes(self) -> bool:
        return self.source is not None and self.draft() != self.baseline

    def set_message(self, text: str, *, error: bool = False) -> None:
        self.message.setText(text)
        self.message.setVisible(bool(text))
        set_style_property(self.message, "tone", "error" if error else "ok")

    def refresh_live(self) -> None:
        """Re-read what the engine remembers about this item (memory only)."""

    def _on_engine_answer(self, token, result, error) -> None:
        self._requests.answer(token, result, error)

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("hooksSectionLabel")
        return label

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setObjectName("hooksDivider")
        line.setFixedHeight(1)
        return line


class WebhookEditor(_Editor):
    NOUN = "webhook"
    NAME_PLACEHOLDER = "e.g. Home automation"

    def _build_fields(self, layout):
        self.url_edit = QLineEdit()
        self.url_edit.setMaxLength(models.URL_MAX_CHARS)
        self.url_edit.setPlaceholderText("https://example.com/openwhisper")
        self.url_edit.setAccessibleName("Webhook address")
        self.url_edit.textChanged.connect(self._update_note)
        layout.addSpacing(4)
        layout.addWidget(_field_label("Web address", self.url_edit))
        layout.addWidget(self.url_edit)
        self.url_note = _hint()
        set_style_property(self.url_note, "tone", "warning")
        self.url_note.hide()
        layout.addWidget(self.url_note)
        self.events = EventChooser()
        layout.addSpacing(4)
        layout.addWidget(_field_label("Events to send", self.events))
        layout.addWidget(self.events)

        self.extras = QWidget()
        self.extras.setObjectName("hooksExtras")
        extras = QVBoxLayout(self.extras)
        extras.setContentsMargins(0, 6, 0, 0)
        extras.setSpacing(6)
        extras.addWidget(self._divider())
        extras.addWidget(self._section_label("Signing secret"))
        extras.addWidget(_hint(
            "Each delivery is signed. Your receiver can check the webhook-signature "
            "header with this secret."
        ))
        self.copy_secret = _small_button("Copy secret")
        self.copy_secret.clicked.connect(self._copy_secret)
        self.rotate_secret = _small_button("Make a new one")
        self.rotate_secret.setToolTip("Replace the secret; receivers need the new one")
        self.rotate_secret.clicked.connect(self._rotate_secret)
        self.secret_chip = StatusChip()
        extras.addWidget(_AdaptiveButtons(
            [self.copy_secret, self.rotate_secret, self.secret_chip, None]
        ))
        extras.addSpacing(4)
        extras.addWidget(self._divider())
        extras.addWidget(self._section_label("Test and recent deliveries"))
        self.test_button = _small_button("Send test")
        self.test_button.setToolTip("Send a test event to this address now")
        self.test_button.clicked.connect(self.send_test)
        self.test_chip = StatusChip()
        extras.addWidget(_AdaptiveButtons([self.test_button, self.test_chip, None]))
        self.recent = RecentList("No deliveries yet.")
        extras.addWidget(self.recent)
        layout.addWidget(self.extras)

    def _update_note(self, *_args) -> None:
        note = unencrypted_note(self.url_edit.text())
        self.url_note.setText(note)
        self.url_note.setVisible(bool(note))

    def _show_fields(self, hook) -> None:
        self.url_edit.setText(hook.url)
        self.events.set_events(hook.events)
        self.extras.setVisible(self.saved)
        self._update_note()

    def _show_idle(self) -> None:
        for button in (self.copy_secret, self.rotate_secret, self.test_button):
            button.setEnabled(True)
        self.secret_chip.show_result("", "muted")
        self.test_chip.show_result("", "muted")

    def draft(self) -> models.Webhook:
        return models.Webhook(
            self.source.id,
            self.name_edit.text().strip(),
            self.url_edit.text().strip(),
            self.events.events(),
            self.source.enabled,
        )

    def refresh_live(self) -> None:
        if not self.saved:
            return
        try:
            records = webhook_engine.deliveries(self.source.id)
        except Exception:
            records = []
        rows = []
        for record in records:
            text, ok = delivery_result(record)
            rows.append((clock(record.at), event_label(record.event_type), text, ok))
        self.recent.set_rows(rows)

    def _copy_secret(self) -> None:
        hook_id = self.item_id()
        self.copy_secret.setEnabled(False)
        self.secret_chip.show_result("Getting the secret…", "busy")

        def done(secret, error) -> None:
            self.copy_secret.setEnabled(True)
            if error or not secret:
                self.secret_chip.show_result(error or "No secret yet.", "error")
                return
            QApplication.clipboard().setText(secret)
            self.secret_chip.show_result("Copied", "ok")

        self._requests.in_background(
            lambda: webhook_engine.ensure_secret(hook_id), "settings-webhook-secret", done
        )

    def _rotate_secret(self) -> None:
        answer = QMessageBox.question(
            self,
            "Make a new signing secret?",
            "Your receiver will reject deliveries until you give it the new secret.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        hook_id = self.item_id()
        self.rotate_secret.setEnabled(False)
        self.secret_chip.show_result("Making a new secret…", "busy")

        def done(secret, error) -> None:
            self.rotate_secret.setEnabled(True)
            if error or not secret:
                self.secret_chip.show_result(error or "Couldn't make a new secret.", "error")
                return
            QApplication.clipboard().setText(secret)
            self.secret_chip.show_result("New secret copied", "ok")

        self._requests.in_background(
            lambda: webhook_engine.rotate_secret(hook_id), "settings-webhook-rotate", done
        )

    def send_test(self) -> None:
        if self.has_unsaved_changes() and not (self.save_first and self.save_first()):
            return
        hook_id = self.item_id()
        self.test_button.setEnabled(False)
        self.test_chip.show_result("Sending…", "busy")

        def done(record, error) -> None:
            self.test_button.setEnabled(True)
            if error:
                self.test_chip.show_result(error, "error")
            else:
                text, ok = delivery_result(record)
                self.test_chip.show_result(text, "ok" if ok else "error")
            self.refresh_live()

        self._requests.when_done(lambda: webhook_engine.send_test(hook_id), done)


class _ProgramFields:
    """Program path, a Choose button and arguments, shared by programs and filters."""

    def _build_program_fields(self, layout, noun: str) -> None:
        self.program_edit = QLineEdit()
        self.program_edit.setPlaceholderText("Choose a program on this computer")
        self.program_edit.setAccessibleName(f"{noun} program")
        self.choose_button = _small_button("Choose…")
        self.choose_button.setAccessibleName("Choose a program")
        self.choose_button.clicked.connect(self._choose)
        self.program_label = _field_label("Program", self.program_edit)
        layout.addWidget(self.program_label)
        self.program_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.program_row = _AdaptiveButtons([self.program_edit, self.choose_button])
        layout.addWidget(self.program_row)
        self.args_edit = QPlainTextEdit()
        self.args_edit.setObjectName("hooksArgs")
        self.args_edit.setTabChangesFocus(True)
        self.args_edit.setPlaceholderText("One per line, e.g. --quiet")
        self.args_edit.setAccessibleName("Program arguments, one per line")
        self.args_edit.setFixedHeight(round(64 * current_ui_font_scale()))
        self.args_label = _field_label("Arguments (optional)", self.args_edit)
        layout.addWidget(self.args_label)
        layout.addWidget(self.args_edit)

    def _choose(self) -> None:
        path = _choose_program(self, self.program_edit.text().strip())
        if path:
            self.program_edit.setText(path)

    def _args(self) -> tuple[str, ...]:
        return tuple(line.strip() for line in self.args_edit.toPlainText().splitlines() if line.strip())


class ProgramEditor(_ProgramFields, _Editor):
    NOUN = "program"
    NAME_PLACEHOLDER = "e.g. Log my dictations"

    def _build_fields(self, layout):
        layout.addSpacing(4)
        self._build_program_fields(layout, "Hook")
        self.events = EventChooser()
        layout.addSpacing(4)
        layout.addWidget(_field_label("Events", self.events))
        layout.addWidget(self.events)
        layout.addSpacing(4)
        self.timeout = NoWheelSpinBox()
        self.timeout.setRange(1, models.PROGRAM_TIMEOUT_MAX_S)
        self.timeout.setSuffix(" s")
        self.timeout.setAccessibleName("Time limit in seconds")
        limit = QWidget()
        limit.setObjectName("hooksInlineField")
        row = QHBoxLayout(limit)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(_field_label("Time limit", self.timeout))
        row.addWidget(self.timeout)
        row.addStretch(1)
        layout.addWidget(limit)
        layout.addWidget(_hint(
            "Each event arrives as JSON on the program's input. It runs directly, never "
            "through a shell, and is stopped with anything it started after the time limit."
        ))

        self.extras = QWidget()
        self.extras.setObjectName("hooksExtras")
        extras = QVBoxLayout(self.extras)
        extras.setContentsMargins(0, 6, 0, 0)
        extras.setSpacing(6)
        extras.addWidget(self._divider())
        extras.addWidget(self._section_label("Test and recent runs"))
        self.test_button = _small_button("Run test")
        self.test_button.setToolTip("Run the program now with a test event")
        self.test_button.clicked.connect(self.run_test)
        self.test_chip = StatusChip()
        extras.addWidget(_AdaptiveButtons([self.test_button, self.test_chip, None]))
        self.recent = RecentList("No runs yet.")
        extras.addWidget(self.recent)
        layout.addWidget(self.extras)

    def _show_fields(self, program) -> None:
        self.program_edit.setText(program.program)
        self.args_edit.setPlainText("\n".join(program.args))
        self.events.set_events(program.events)
        self.timeout.setValue(int(program.timeout_s))
        self.extras.setVisible(self.saved)

    def _show_idle(self) -> None:
        self.test_button.setEnabled(True)
        self.test_chip.show_result("", "muted")

    def draft(self) -> models.Program:
        return models.Program(
            self.source.id,
            self.name_edit.text().strip(),
            self.program_edit.text().strip(),
            self._args(),
            self.events.events(),
            self.source.enabled,
            int(self.timeout.value()),
        )

    def refresh_live(self) -> None:
        if not self.saved:
            return
        try:
            records = program_engine.runs(self.source.id)
        except Exception:
            records = []
        rows = []
        for record in records:
            text, ok = run_result(record)
            rows.append((clock(record.at), "Ran" if ok else "Failed", text, ok))
        self.recent.set_rows(rows)

    def run_test(self) -> None:
        if self.has_unsaved_changes() and not (self.save_first and self.save_first()):
            return
        program_id = self.item_id()
        self.test_button.setEnabled(False)
        self.test_chip.show_result("Running…", "busy")

        def done(record, error) -> None:
            self.test_button.setEnabled(True)
            if error:
                self.test_chip.show_result(error, "error")
            else:
                text, ok = run_result(record)
                self.test_chip.show_result(text, "ok" if ok else "error")
            self.refresh_live()

        self._requests.when_done(lambda: program_engine.run_test(program_id), done)


class FilterEditor(_ProgramFields, _Editor):
    NOUN = "filter"
    NAME_PLACEHOLDER = "e.g. Fix product names"

    KINDS = (models.FILTER_HTTP, models.FILTER_PROGRAM)
    STAGES = (models.BEFORE_CLEANUP, models.AFTER_CLEANUP)

    def _build_fields(self, layout):
        layout.addSpacing(4)
        self.kind_bar = SegmentedBar((("Web address", ""), ("Program", "")), compact=True)
        self.kind_bar.setAccessibleName("Filter kind")
        self.kind_bar.currentIndexChanged.connect(self._kind_changed)
        layout.addWidget(_field_label("What changes the words", self.kind_bar))
        layout.addWidget(self.kind_bar)
        self.url_edit = QLineEdit()
        self.url_edit.setMaxLength(models.URL_MAX_CHARS)
        self.url_edit.setPlaceholderText("http://127.0.0.1:8080/filter")
        self.url_edit.setAccessibleName("Filter address")
        self.url_edit.textChanged.connect(self._update_note)
        self.url_label = _field_label("Web address", self.url_edit)
        layout.addWidget(self.url_label)
        layout.addWidget(self.url_edit)
        self.url_note = _hint()
        set_style_property(self.url_note, "tone", "warning")
        self.url_note.hide()
        layout.addWidget(self.url_note)
        self._build_program_fields(layout, "Filter")
        layout.addSpacing(4)
        self.stage_bar = SegmentedBar((
            ("Before AI cleanup", "Words as heard"),
            ("After AI cleanup", "Final words"),
        ))
        self.stage_bar.setAccessibleName("When the filter runs")
        layout.addWidget(_field_label("When it runs", self.stage_bar))
        layout.addWidget(self.stage_bar)
        layout.addSpacing(4)
        self.timeout = NoWheelSpinBox()
        self.timeout.setRange(models.FILTER_TIMEOUT_MIN_MS, models.FILTER_TIMEOUT_MAX_MS)
        self.timeout.setSingleStep(100)
        self.timeout.setSuffix(" ms")
        self.timeout.setAccessibleName("Time limit in milliseconds")
        limit = QWidget()
        limit.setObjectName("hooksInlineField")
        row = QHBoxLayout(limit)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(_field_label("Time limit", self.timeout))
        row.addWidget(self.timeout)
        row.addStretch(1)
        layout.addWidget(limit)
        self.reply_hint = _hint()
        layout.addWidget(self.reply_hint)
        self._kind_changed(0)

    def _kind(self) -> str:
        return self.KINDS[max(0, self.kind_bar.currentIndex())]

    def _kind_changed(self, _index) -> None:
        http = self._kind() == models.FILTER_HTTP
        for widget in (self.url_label, self.url_edit):
            widget.setVisible(http)
        for widget in (self.program_label, self.program_row, self.args_label, self.args_edit):
            widget.setVisible(not http)
        self._update_note()
        self.reply_hint.setText(
            'OpenWhisper posts the words as JSON. Reply {"text": "…"} to change them, '
            '{} to keep them, or {"cancel": true} to paste nothing. A slow or odd reply '
            "keeps your words."
            if http else
            "The words arrive as JSON on the program's input. Print {\"text\": \"…\"} to "
            'change them, {} to keep them, or {"cancel": true} to paste nothing. A slow or '
            "odd reply keeps your words."
        )

    def _update_note(self, *_args) -> None:
        note = unencrypted_note(self.url_edit.text()) if self._kind() == models.FILTER_HTTP else ""
        self.url_note.setText(note)
        self.url_note.setVisible(bool(note))

    def _show_fields(self, text_filter) -> None:
        self.kind_bar.setCurrentIndex(self.KINDS.index(text_filter.kind))
        self._kind_changed(0)
        self.url_edit.setText(text_filter.url)
        self.program_edit.setText(text_filter.program)
        self.args_edit.setPlainText("\n".join(text_filter.args))
        self.stage_bar.setCurrentIndex(self.STAGES.index(text_filter.stage))
        self.timeout.setValue(int(text_filter.timeout_ms))

    def draft(self) -> models.TextFilter:
        http = self._kind() == models.FILTER_HTTP
        return models.TextFilter(
            self.source.id,
            self.name_edit.text().strip(),
            self._kind(),
            self.STAGES[max(0, self.stage_bar.currentIndex())],
            self.url_edit.text().strip() if http else "",
            "" if http else self.program_edit.text().strip(),
            () if http else self._args(),
            int(self.timeout.value()),
            self.source.enabled,
        )


# ---- sections -----------------------------------------------------------------

class _HookRow(QFrame):
    """One saved item: last result, name, what it does, Edit, and an on/off switch."""

    edit_requested = pyqtSignal(str)
    enabled_changed = pyqtSignal(str, bool)

    def __init__(self, item_id: str, *, shows_results: bool = True, parent=None):
        super().__init__(parent)
        self.item_id = item_id
        self.setObjectName("hooksRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = self._grid = QGridLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)
        # The last delivery or run, where there is one to report.
        self.dot = StatusDot(diameter=9)
        self.dot.setVisible(shows_results)
        self._text = QWidget()
        self._text.setObjectName("hooksRowText")
        column = QVBoxLayout(self._text)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(1)
        self.name = ElidingLabel("")
        self.name.setObjectName("hooksRowName")
        column.addWidget(self.name)
        self.detail = ElidingLabel("")
        self.detail.setObjectName("hooksRowDetail")
        column.addWidget(self.detail)
        self.edit_button = _small_button("Edit")
        self.edit_button.clicked.connect(lambda: self.edit_requested.emit(self.item_id))
        self.switch = SettingsSwitch()
        self.switch.toggled.connect(lambda on: self.enabled_changed.emit(self.item_id, on))
        self._controls = QWidget()
        self._controls.setObjectName("hooksRowText")
        controls = QHBoxLayout(self._controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(10)
        controls.addWidget(self.edit_button, 0, Qt.AlignmentFlag.AlignVCenter)
        controls.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignVCenter)
        self._narrow = None
        self._arrange(False)

    def _arrange(self, narrow: bool) -> None:
        """Edit and the switch sit beside the text, or under it in a narrow column."""
        if narrow == self._narrow:
            return
        self._narrow = narrow
        grid = self._grid
        for widget in (self.dot, self._text, self._controls):
            grid.removeWidget(widget)
        center = Qt.AlignmentFlag.AlignVCenter
        grid.addWidget(self.dot, 0, 0, center)
        grid.addWidget(self._text, 0, 1)
        if narrow:
            grid.addWidget(self._controls, 1, 1, Qt.AlignmentFlag.AlignRight)
        else:
            grid.addWidget(self._controls, 0, 2, center)
        grid.setColumnStretch(1, 1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        margins = self._grid.contentsMargins()
        room = self.width() - margins.left() - margins.right()
        needed = (self._controls.sizeHint().width() + self._grid.horizontalSpacing() * 2
                  + self.dot.width() + round(140 * current_ui_font_scale()))
        self._arrange(room < needed)

    def show_item(self, name: str, detail: str, enabled: bool, status: EngineStatus) -> None:
        self.name.setText(name)
        self.detail.setText(detail if enabled else f"Off · {detail}")
        self.edit_button.setAccessibleName(f"Edit {name}")
        self.switch.setAccessibleName(f"{name} on")
        blocked = self.switch.blockSignals(True)
        self.switch.setChecked(enabled)
        self.switch.blockSignals(blocked)
        status = status if enabled else EngineStatus.UNKNOWN
        if self.dot.status() != status:
            self.dot.set_status(status)
        _restyle(self, "off", not enabled)

    def set_selected(self, selected: bool) -> None:
        _restyle(self, "selected", selected)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.edit_requested.emit(self.item_id)
            event.accept()
            return
        super().mousePressEvent(event)


class HookSection(QWidget):
    """A list of one kind of hook with an inline editor.

    An unsaved edit is never dropped silently: opening another item asks to
    save it first, ``refresh`` leaves it in place, and Settings calls
    ``save_draft`` when the page is left.
    """

    changed = pyqtSignal()

    NOUN = "item"
    MAXIMUM = 0
    EMPTY_TEXT = ""
    EDITOR = _Editor
    #: Rows show a dot for the last delivery or run.
    SHOWS_RESULTS = True

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.setObjectName("hooksSection")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.manager = manager
        self._items: list = []
        self._rows: dict[str, _HookRow] = {}
        self._requests = _Requests(self)
        self.count_label = QLabel()
        self.count_label.setObjectName("hooksCount")
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        root = self._root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        self.list_area = QWidget()
        self.list_area.setObjectName("hooksList")
        self._list = QVBoxLayout(self.list_area)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(6)
        root.addWidget(self.list_area)

        self.empty_state = QFrame()
        self.empty_state.setObjectName("hooksEmpty")
        empty = QVBoxLayout(self.empty_state)
        empty.setContentsMargins(14, 12, 14, 12)
        empty_label = WrappedLabel(self.EMPTY_TEXT)
        empty_label.setObjectName("hooksEmptyText")
        empty.addWidget(empty_label)
        root.addWidget(self.empty_state)

        self.editor = self.EDITOR()
        self.editor.hide()
        self.editor.save_requested.connect(self.save)
        self.editor.cancel_requested.connect(self.cancel)
        self.editor.delete_requested.connect(self.delete)
        self.editor.save_first = self.save
        self._list.addWidget(self.editor)

        self.add_button = _small_button(f"Add {self.NOUN}")
        self.add_button.setAccessibleName(f"Add a {self.NOUN}")
        self.add_button.clicked.connect(lambda: self.open_editor(""))
        self.message = WrappedLabel("")
        self.message.setObjectName("hooksMessage")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.hide()
        self.footer = _AdaptiveButtons([self.add_button, None])
        root.addWidget(self.footer)
        root.addWidget(self.message)
        self._build_extra(root)

        self._live = QTimer(self)
        self._live.setInterval(LIVE_INTERVAL_MS)
        self._live.timeout.connect(self.refresh_live)
        self.refresh()

    # ---- what a kind provides ----
    def _build_extra(self, root: QVBoxLayout) -> None:
        """Add anything shown under the list and its Add button."""

    def _load(self, settings) -> list:
        raise NotImplementedError

    def _save_item(self, settings, item) -> None:
        raise NotImplementedError

    def _delete_item(self, settings, item_id) -> None:
        raise NotImplementedError

    def _blank(self):
        raise NotImplementedError

    def _with_enabled(self, item, enabled: bool):
        return replace(item, enabled=enabled)

    def _detail(self, item) -> str:
        raise NotImplementedError

    def _status(self, item) -> EngineStatus:
        return EngineStatus.UNKNOWN

    def _after_delete(self, item) -> None:
        """Forget anything kept outside settings for a deleted item."""

    # ---- state ----
    def _settings(self) -> dict:
        try:
            return self.manager.load_all_settings()
        except Exception:
            return {}

    def items(self) -> list:
        return list(self._items)

    def editing_id(self) -> str:
        return self.editor.item_id() if not self.editor.isHidden() else ""

    def has_unsaved_changes(self) -> bool:
        return not self.editor.isHidden() and self.editor.has_unsaved_changes()

    def refresh(self, select_id: Optional[str] = None) -> None:
        """Re-read the saved items; an open editor with unsaved changes stays as it is."""
        self._items = self._load(self._settings())
        self.count_label.setText(f"{len(self._items)} / {self.MAXIMUM}")
        self.count_label.setVisible(bool(self._items))
        keep = {item.id for item in self._items}
        for item_id, row in list(self._rows.items()):
            self._list.removeWidget(row)
            if item_id not in keep:
                del self._rows[item_id]
                row.hide()
                row.deleteLater()
        self._list.removeWidget(self.editor)
        for item in self._items:
            row = self._rows.get(item.id)
            if row is None:
                row = _HookRow(item.id, shows_results=self.SHOWS_RESULTS)
                row.edit_requested.connect(self.open_editor)
                row.enabled_changed.connect(self.set_enabled)
                self._rows[item.id] = row
            self._list.addWidget(row)
            row.show()
        self.empty_state.setVisible(not self._items and self.editor.isHidden())
        self.add_button.setEnabled(len(self._items) < self.MAXIMUM)
        self.add_button.setToolTip(
            "" if len(self._items) < self.MAXIMUM
            else f"You can keep up to {self.MAXIMUM}. Delete one to add another."
        )
        if select_id is not None:
            item = next((i for i in self._items if i.id == select_id), None)
            if item is not None:
                self._show_editor(item, saved=True)
            else:
                self._close_editor()
        elif not self.editor.isHidden() and not self.editor.has_unsaved_changes():
            item = next((i for i in self._items if i.id == self.editor.item_id()), None)
            if item is not None and item != self.editor.source:
                # Changed elsewhere while open and untouched here.
                self._show_editor(item, saved=True)
            elif item is None and self.editor.saved:
                # Deleted elsewhere while open and untouched.
                self._close_editor()
        self._place_editor()
        self.refresh_live()

    def refresh_live(self) -> None:
        for item in self._items:
            row = self._rows.get(item.id)
            if row is not None:
                row.show_item(item.name, self._detail(item), item.enabled, self._status(item))
        if not self.editor.isHidden():
            self.editor.refresh_live()

    def _place_editor(self) -> None:
        """Put the editor under the row it edits, or after the list for a new item."""
        editing = self.editing_id()
        for item_id, row in self._rows.items():
            row.set_selected(item_id == editing)
        self._list.removeWidget(self.editor)
        row = self._rows.get(editing)
        index = self._list.indexOf(row) + 1 if row is not None else self._list.count()
        self._list.insertWidget(index, self.editor)
        self.list_area.setVisible(bool(self._rows) or not self.editor.isHidden())

    def _show_editor(self, item, *, saved: bool) -> None:
        self.editor.show_item(item, saved=saved)
        self.editor.show()
        self.empty_state.hide()
        self._place_editor()

    def _close_editor(self) -> None:
        self.editor.hide()
        self.editor.clear()
        self.empty_state.setVisible(not self._items)
        self._place_editor()

    def _save_before_switch(self) -> bool:
        if not self.has_unsaved_changes():
            return True
        answer = QMessageBox.question(
            self,
            f"Unsaved {self.NOUN}",
            f"Save your changes to this {self.NOUN}?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        return answer == QMessageBox.StandardButton.Discard

    # ---- actions ----
    def open_editor(self, item_id: str = "") -> bool:
        """Edit a saved item, or start a new one with ``""``."""
        if item_id and item_id == self.editing_id():
            return True
        if not self._save_before_switch():
            return False
        self._set_message("")
        item = next((i for i in self._items if i.id == item_id), None)
        if item is None:
            self._show_editor(self._blank(), saved=False)
        else:
            self._show_editor(item, saved=True)
        self.editor.name_edit.setFocus()
        return True

    def cancel(self) -> None:
        self._close_editor()

    def save(self) -> bool:
        draft = self.editor.draft()
        # The switch on the row decides on/off, even while the editor is open.
        saved = next((i for i in self._items if i.id == draft.id), None)
        if saved is not None:
            draft = self._with_enabled(draft, saved.enabled)
        try:
            self.manager.mutate_settings(lambda settings: self._save_item(settings, draft))
        except ValueError as exc:
            self.editor.set_message(str(exc), error=True)
            return False
        except Exception as exc:
            self.editor.set_message(f"Couldn't save the {self.NOUN}: {_error_text(exc)}", error=True)
            return False
        self.refresh(draft.id)
        self.editor.set_message("Saved.")
        self.changed.emit()
        return True

    def save_draft(self, *, ask: bool = False) -> bool:
        """Save an unsaved edit when Settings leaves the page.

        Returns:
            False when the user chose to keep editing a draft that can't be saved yet.
        """
        if not self.has_unsaved_changes() or self.save() or not ask:
            return True
        answer = QMessageBox.question(
            self,
            f"Unsaved {self.NOUN}",
            f"{self.editor.message.text()} Discard your changes to this {self.NOUN}?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Discard:
            return False
        self._close_editor()
        return True

    def delete(self) -> None:
        item = next((i for i in self._items if i.id == self.editor.item_id()), None)
        if item is None:
            self._close_editor()
            return
        answer = QMessageBox.question(
            self,
            f"Delete {self.NOUN}?",
            self._delete_question(item),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.manager.mutate_settings(lambda settings: self._delete_item(settings, item.id))
        except Exception as exc:
            self.editor.set_message(f"Couldn't delete the {self.NOUN}: {_error_text(exc)}", error=True)
            return
        self._close_editor()
        self.refresh()
        self._after_delete(item)
        self.changed.emit()

    def _delete_question(self, item) -> str:
        return f"Delete “{item.name}”?"

    def set_enabled(self, item_id: str, enabled: bool) -> None:
        item = next((i for i in self._items if i.id == item_id), None)
        if item is None:
            return
        try:
            self.manager.mutate_settings(
                lambda settings: self._save_item(settings, self._with_enabled(item, enabled))
            )
        except Exception as exc:
            self._set_message(_error_text(exc), error=True)
            self.refresh_live()
            return
        self._set_message("")
        self._items = self._load(self._settings())
        if self.editor.item_id() == item_id:
            self.editor.set_enabled(enabled)
        self.refresh_live()
        self.changed.emit()

    def _set_message(self, text: str, *, error: bool = False) -> None:
        self.message.setText(text)
        self.message.setVisible(bool(text))
        set_style_property(self.message, "tone", "error" if error else "ok")

    def _on_engine_answer(self, token, result, error) -> None:
        self._requests.answer(token, result, error)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_live()
        self._live.start()

    def hideEvent(self, event):
        self._live.stop()
        super().hideEvent(event)


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc or url
    except ValueError:
        return url


class WebhookSection(HookSection):
    NOUN = "webhook"
    MAXIMUM = models.MAX_WEBHOOKS
    EMPTY_TEXT = "No webhooks yet. Add one to send finished dictations to a web address."
    EDITOR = WebhookEditor

    def _load(self, settings):
        return models.load_webhooks(settings)

    def _save_item(self, settings, item):
        models.save_webhook(settings, item)

    def _delete_item(self, settings, item_id):
        models.delete_webhook(settings, item_id)

    def _blank(self):
        return models.Webhook(models.new_id(), "", "", DEFAULT_EVENTS)

    def _last(self, item):
        try:
            records = webhook_engine.deliveries(item.id)
        except Exception:
            return None
        return records[0] if records else None

    def _detail(self, item) -> str:
        parts = [_host(item.url), events_summary(item.events)]
        last = self._last(item)
        if last is not None:
            parts.append(f"Last delivery {delivery_result(last)[0]}")
        return " · ".join(parts)

    def _status(self, item) -> EngineStatus:
        last = self._last(item)
        if last is None:
            return EngineStatus.UNKNOWN
        return EngineStatus.READY if last.ok else EngineStatus.ATTENTION

    def _delete_question(self, item) -> str:
        return (f"Delete “{item.name}”? OpenWhisper stops sending to it and "
                "forgets its signing secret.")

    def _after_delete(self, item) -> None:
        def done(_result, error) -> None:
            if error:
                self._set_message(
                    f"Deleted, but its signing secret couldn't be removed: {error}", error=True
                )

        self._requests.in_background(
            lambda: webhook_engine.delete_secret(item.id), "settings-webhook-forget", done
        )


class ProgramSection(HookSection):
    NOUN = "program"
    MAXIMUM = models.MAX_PROGRAMS
    EMPTY_TEXT = "No programs yet. Add one to run your own script when you dictate."
    EDITOR = ProgramEditor

    def _load(self, settings):
        return models.load_programs(settings)

    def _save_item(self, settings, item):
        models.save_program(settings, item)

    def _delete_item(self, settings, item_id):
        models.delete_program(settings, item_id)

    def _blank(self):
        return models.Program(models.new_id(), "", "", (), DEFAULT_EVENTS)

    def _last(self, item):
        try:
            records = program_engine.runs(item.id)
        except Exception:
            return None
        return records[0] if records else None

    def _detail(self, item) -> str:
        parts = [os.path.basename(item.program) or item.program, events_summary(item.events)]
        last = self._last(item)
        if last is not None:
            parts.append(f"Last run {run_result(last)[0]}")
        return " · ".join(parts)

    def _status(self, item) -> EngineStatus:
        last = self._last(item)
        if last is None:
            return EngineStatus.UNKNOWN
        return EngineStatus.READY if last.ok else EngineStatus.ATTENTION

    def _delete_question(self, item) -> str:
        return f"Delete “{item.name}”? OpenWhisper stops running it."


class _LeaseList(QWidget):
    """Filters other programs registered through the live API, with when each ends."""

    open_api_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("hooksLeases")
        self._shown = None
        self._lines = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)
        title = QLabel("Added by your programs")
        title.setObjectName("hooksSectionLabel")
        layout.addWidget(title)
        self.rows = QWidget()
        self.rows.setObjectName("hooksLeaseRows")
        self._rows = QVBoxLayout(self.rows)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(4)
        layout.addWidget(self.rows)
        self.note = _hint(
            "A program on this computer can add a filter for a minute at a time through "
            "the live API, once you allow Change dictated text in API & MCP."
        )
        layout.addWidget(self.note)
        self.open_api = QPushButton("Open API && MCP")
        # Its own style: settingsPageLink's min-height: 0 lets a page laid
        # out at its minimum height squeeze the link to a sliver.
        self.open_api.setObjectName("hooksLink")
        self.open_api.setFlat(True)
        self.open_api.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_api.clicked.connect(self.open_api_requested)
        layout.addWidget(self.open_api, 0, Qt.AlignmentFlag.AlignLeft)

    def refresh(self) -> None:
        try:
            leases = list(filter_engine.leases())
        except Exception:
            leases = []
        now = time.time()
        shown = tuple(
            (lease.id, lease.url, lease.stage, max(0, round(lease.expires_at - now)))
            for lease in leases
            if lease.expires_at > now
        )
        if shown == self._shown:
            return
        self._shown = shown
        # Rows are rebuilt only when the leases change; the countdown updates in place.
        if [row[:3] for row in shown] != [line.lease for line in self._lines]:
            while self._rows.count():
                widget = self._rows.takeAt(0).widget()
                if widget is not None:
                    widget.deleteLater()
            self._lines = []
            for lease_id, url, stage, _seconds in shown:
                line = QFrame()
                line.setObjectName("hooksLeaseRow")
                line.lease = (lease_id, url, stage)
                row = QHBoxLayout(line)
                row.setContentsMargins(10, 6, 10, 6)
                row.setSpacing(10)
                address = ElidingLabel(url)
                address.setObjectName("hooksRowName")
                row.addWidget(address, 1)
                line.detail = QLabel()
                line.detail.setObjectName("hooksRowDetail")
                row.addWidget(line.detail)
                self._rows.addWidget(line)
                self._lines.append(line)
        for line, (_lease_id, _url, stage, seconds) in zip(self._lines, shown):
            line.detail.setText(f"{STAGE_LABELS.get(stage, stage)} · ends in {seconds} s")
        self.rows.setVisible(bool(shown))
        self.note.setVisible(not shown)
        self.open_api.setVisible(not shown)


class FilterSection(HookSection):
    NOUN = "filter"
    MAXIMUM = models.MAX_FILTERS
    SHOWS_RESULTS = False
    EMPTY_TEXT = "No text filters yet. Add one to let your own tool fix words before they're pasted."
    EDITOR = FilterEditor

    def _build_extra(self, root):
        self.leases = _LeaseList()
        root.addWidget(self.leases)

    def _load(self, settings):
        return models.load_filters(settings)

    def _save_item(self, settings, item):
        models.save_filter(settings, item)

    def _delete_item(self, settings, item_id):
        models.delete_filter(settings, item_id)

    def _blank(self):
        return models.TextFilter(models.new_id(), "", models.FILTER_HTTP, models.AFTER_CLEANUP)

    def _detail(self, item) -> str:
        target = _host(item.url) if item.kind == models.FILTER_HTTP else (
            os.path.basename(item.program) or item.program
        )
        return f"{target} · {STAGE_LABELS.get(item.stage, item.stage)} · {item.timeout_ms} ms"

    def _delete_question(self, item) -> str:
        return f"Delete “{item.name}”? Your words will no longer pass through it."

    def refresh_live(self) -> None:
        super().refresh_live()
        self.leases.refresh()
