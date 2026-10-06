"""Settings → Recording → Microphone: the preferred microphone and its backups.

``audio_input_priority`` is the single source of truth. Its first entry is the
preferred microphone in the combo (the Basic view mirrors that combo); the
rest are the backups, listed in order above a fixed System default row.
Combo items carry an entry token rather than a device index, because indexes
shift whenever PortAudio re-reads its devices.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Callable, List, Optional

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import QBoxLayout, QFrame, QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from services import audio_devices
from services.audio_devices import InputDevice, display_name, hostapi_label, match_entry, short_name
from services.settings import SettingsKey
from ui_qt.dialogs.settings_fields import settings_caption
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.icons import design_icon
from ui_qt.utils.list_reconcile import HistoryDelivery
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets.buttons import Button, neutral_button
from ui_qt.widgets.eliding_label import ElidingLabel
from ui_qt.widgets.no_wheel import ElidingComboBox
from ui_qt.widgets.setting_tile import FieldTile, InfoTile
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)

SYSTEM_DEFAULT = ""
OUTCOME_REFRESHED = "refreshed"
OUTCOME_BUSY = "busy"
IN_USE = "Microphones are in use. Refresh when recording or the meeting ends."


def entry_token(entry: dict) -> str:
    return json.dumps([entry["hostapi"], entry["name"]])


def token_entry(token) -> Optional[dict]:
    try:
        hostapi, name = json.loads(token)
    except (TypeError, ValueError):
        return None
    if not isinstance(hostapi, str) or not isinstance(name, str) or not name:
        return None
    return {"name": name, "hostapi": hostapi}


def saved_priority(settings: dict) -> List[dict]:
    return audio_devices.normalize_priority(settings.get(SettingsKey.AUDIO_INPUT_PRIORITY))


def preferred_token(settings: dict) -> str:
    priority = saved_priority(settings)
    return entry_token(priority[0]) if priority else SYSTEM_DEFAULT


def rail_value(settings: dict) -> str:
    names = [short_name(entry["name"]) for entry in saved_priority(settings)]
    if not names:
        return "System default"
    value = " → ".join(names[:2])
    return value + (f" +{len(names) - 2}" if len(names) > 2 else "")


def preferred_name(settings: dict) -> str:
    priority = saved_priority(settings)
    return short_name(priority[0]["name"]) if priority else "Default microphone"


class _PreferredRow(QWidget):
    """The microphone combo with Refresh beside it, or under it when narrow."""

    # Room for a typical "Microphone (USB Audio Device)" plus the button.
    NARROW_BELOW = 400

    def __init__(self, combo, button):
        super().__init__()
        self.setObjectName("micPreferredRow")
        self._button = button
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self._layout.addWidget(combo, 1)
        self._layout.addWidget(button)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = self.width() < round(self.NARROW_BELOW * current_ui_font_scale())
        direction = QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight
        if self._layout.direction() != direction:
            self._layout.setDirection(direction)
            self._layout.setAlignment(
                self._button, Qt.AlignmentFlag.AlignLeft if narrow else Qt.AlignmentFlag.AlignVCenter,
            )


def _listed(device: InputDevice) -> bool:
    # One row per microphone: Windows lists each under MME, DirectSound,
    # WASAPI and WDM-KS, and the default host API opens all of them.
    return device.default_api and not audio_devices.is_system_alias(device)


class MicrophoneSection:
    """Builds the Microphone group and keeps it in step with the saved order."""

    def __init__(self, dialog, list_devices: Callable[[], list]):
        self.dialog = dialog
        self._list_devices = list_devices
        # None until the first enumeration arrives: until then nothing can be
        # called "Not connected".
        self.devices: Optional[List[InputDevice]] = None
        self.priority: List[dict] = []
        self.refresh_button: Optional[QPushButton] = None

    def build(self, layout) -> None:
        dialog = self.dialog
        combo = ElidingComboBox()
        combo.setObjectName("settingsAudioDeviceCombo")
        combo.setMinimumHeight(40)
        combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        dialog.audio_device_combo = combo
        self.refresh_button = neutral_button(Button("Refresh"))
        self.refresh_button.setToolTip("Look for microphones plugged in since OpenWhisper started")
        self.refresh_button.clicked.connect(self.refresh_devices)
        row = _PreferredRow(combo, self.refresh_button)
        dialog.audio_device_tile = FieldTile(
            "Preferred microphone",
            "Used for dictation and meetings.",
            row,
            design_icon("microphone-blue.svg"),
        )
        self.status = settings_caption("")
        self.status.hide()
        dialog.audio_device_tile.add_body(self.status)

        dialog.microphone_backup_tile = InfoTile(
            "If it's unplugged, use",
            "OpenWhisper moves down this list, even in the middle of a dictation.",
            design_icon("stack-slate.svg"),
        )
        self.rows = QWidget()
        self.rows.setObjectName("micBackupList")
        self.rows_layout = QVBoxLayout(self.rows)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(6)
        dialog.microphone_backup_tile.add_body(self.rows)
        self.add_combo = ElidingComboBox()
        self.add_combo.setMinimumHeight(36)
        self.add_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.add_combo.setAccessibleName("Add a backup microphone")
        self.add_combo.activated.connect(self._on_add_chosen)
        dialog.microphone_backup_tile.add_body(self.add_combo)
        dialog._tile_group(
            layout, "Microphone", [dialog.audio_device_tile, dialog.microphone_backup_tile], columns=1,
        )
        self.load(dialog._settings_snapshot())
        self.enumerate()

    def load(self, settings: dict) -> None:
        self.priority = saved_priority(settings)
        self._render()

    def enumerate(self, *, refresh: bool = False) -> None:
        """List devices off the Qt thread; the driver can take seconds to answer."""
        dialog = self.dialog
        dialog._audio_device_generation += 1
        generation = dialog._audio_device_generation
        delivery = HistoryDelivery()
        delivery.loaded.connect(dialog._apply_audio_devices)

        def load():
            outcome = ""
            try:
                if refresh:
                    outcome = OUTCOME_REFRESHED if audio_devices.refresh_portaudio() else OUTCOME_BUSY
                devices = self._list_devices()
            except Exception:
                logger.warning("Couldn't discover audio inputs", exc_info=True)
                devices = []
            delivery.loaded.emit(generation, outcome, devices, "")

        threading.Thread(target=load, name="settings-audio-devices", daemon=True).start()

    def apply_devices(self, devices: list, outcome: str) -> None:
        self.devices = [device for device in devices if isinstance(device, InputDevice)]
        if self.refresh_button is not None:
            self.refresh_button.setEnabled(True)
        self._say(IN_USE if outcome == OUTCOME_BUSY else "")
        self._render()

    def refresh_devices(self) -> None:
        dialog = self.dialog
        meeting_active = getattr(dialog, "get_meeting_active", None)
        busy = audio_devices.open_stream_count() > 0 or bool(meeting_active and meeting_active())
        if not busy:
            from services import audio_player

            busy = audio_player.player().is_playing
        if busy:
            self._say(IN_USE)
            return
        self._say("")
        self.refresh_button.setEnabled(False)
        self.enumerate(refresh=True)

    def choose_preferred(self, token) -> None:
        if token == SYSTEM_DEFAULT:
            priority = []
        else:
            entry = token_entry(token)
            if entry is None:
                return
            priority = [entry] + [saved for saved in self.priority if saved != entry]
        updates = {SettingsKey.AUDIO_INPUT_PRIORITY: priority}
        drops = ()
        # The old index key is what an older version reads after a
        # downgrade. Keep it on the preferred microphone where that version
        # could open it (it knows no WASAPI conversion), else the default.
        device = self._match(priority[0]) if priority else None
        if device is not None and device.default_api:
            updates[SettingsKey.AUDIO_INPUT_DEVICE] = device.index
        else:
            drops = (SettingsKey.AUDIO_INPUT_DEVICE,)
        self._save(priority, updates, drops)

    def _move(self, index: int, offset: int) -> None:
        target = index + offset
        if not (1 <= index < len(self.priority) and 1 <= target < len(self.priority)):
            return
        priority = list(self.priority)
        priority[index], priority[target] = priority[target], priority[index]
        self._save(priority)

    def _remove(self, index: int) -> None:
        if not 1 <= index < len(self.priority):
            return
        self._save(self.priority[:index] + self.priority[index + 1:])

    def _on_add_chosen(self, _index: int) -> None:
        entry = token_entry(self.add_combo.currentData())
        self.add_combo.setCurrentIndex(0)
        if entry is None or entry in self.priority or not self.priority:
            return
        self._save(self.priority + [entry])

    def _save(self, priority: List[dict], updates: Optional[dict] = None, drops: tuple = ()) -> None:
        dialog = self.dialog
        updates = updates or {SettingsKey.AUDIO_INPUT_PRIORITY: priority}
        if not dialog._persist_many(updates, drops=drops):
            self._render()
            return
        self.priority = priority
        self._render()
        if dialog.on_audio_device_changed:
            dialog.on_audio_device_changed(priority)

    def _match(self, entry: dict) -> Optional[InputDevice]:
        return match_entry(entry, self.devices) if self.devices else None

    def _say(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _label(self, entry: dict) -> tuple[str, bool]:
        """What to call a saved entry, and whether it may be connected."""
        device = self._match(entry)
        if device is None:
            return display_name(entry["name"]), self.devices is None
        label = device.display
        if not device.default_api:
            label = f"{label} · {hostapi_label(device.hostapi)}"
        return label, True

    def _render(self) -> None:
        self.dialog.audio_device_tile.set_description(
            "Used for dictation and meetings."
            if self.priority else
            "Used for dictation and meetings. Choose one to set backups for when it's unplugged."
        )
        self._fill_preferred()
        self._fill_rows()
        self._fill_add_combo()

    def _fill_preferred(self) -> None:
        combo = self.dialog.audio_device_combo
        blocker = combo.blockSignals(True)
        combo.clear()
        combo.addItem("System default", SYSTEM_DEFAULT)
        tokens = {SYSTEM_DEFAULT}
        for device in self.devices or ():
            if not _listed(device):
                continue
            saved = next((entry for entry in self.priority if self._match(entry) == device), None)
            token = entry_token(saved or device.key)
            if token not in tokens:
                combo.addItem(device.display, token)
                tokens.add(token)
        for entry in self.priority:
            token = entry_token(entry)
            if token not in tokens:
                label, connected = self._label(entry)
                combo.addItem(label if connected else f"{label} · Not connected", token)
                tokens.add(token)
        wanted = entry_token(self.priority[0]) if self.priority else SYSTEM_DEFAULT
        combo.setCurrentIndex(max(0, combo.findData(wanted)))
        combo.blockSignals(blocker)

    def _fill_rows(self) -> None:
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        backups = self.priority[1:]
        self.dialog.microphone_backup_tile.setVisible(bool(self.priority))
        for offset, entry in enumerate(backups):
            index = offset + 1
            label, connected = self._label(entry)
            self.rows_layout.addWidget(self._row(
                label,
                "" if connected else "Not connected",
                state="" if connected else "missing",
                up=(lambda _=False, i=index: self._move(i, -1)) if offset > 0 else None,
                down=(lambda _=False, i=index: self._move(i, 1)) if offset < len(backups) - 1 else None,
                remove=lambda _=False, i=index: self._remove(i),
            ))
        self.rows_layout.addWidget(self._row("System default", "Always last", state="fixed"))

    def _row(
        self,
        label: str,
        note: str,
        *,
        state: str,
        up: Optional[Callable] = None,
        down: Optional[Callable] = None,
        remove: Optional[Callable] = None,
    ) -> QFrame:
        row = QFrame()
        row.setObjectName("micBackupRow")
        set_style_property(row, "state", state)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(12, 6, 6, 6)
        layout.setSpacing(4)
        text = QWidget()
        text.setObjectName("micBackupText")
        column = QVBoxLayout(text)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(1)
        # Wrapped, not elided: a narrow column would cut every name to
        # "Microphone (…" and hide the part that tells them apart.
        name = WrappedLabel(label)
        name.setObjectName("micBackupName")
        column.addWidget(name)
        if note:
            hint = ElidingLabel(note)
            hint.setObjectName("micBackupNote")
            column.addWidget(hint)
        layout.addWidget(text, 1)
        if state != "fixed":
            for kind, action, tip in (("up", up, "Move up"), ("down", down, "Move down"),
                                      ("remove", remove, "Remove")):
                button = QPushButton()
                button.setObjectName("micBackupButton")
                button.setProperty("kind", kind)
                button.setFixedSize(28, 28)
                button.setIconSize(QSize(14, 14))
                button.setFlat(True)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setToolTip(tip)
                button.setAccessibleName(f"{tip}: {label}")
                if action is not None:
                    button.clicked.connect(action)
                else:
                    # The top row has no Up and the bottom no Down; keeping
                    # the space lines every row's buttons up.
                    policy = button.sizePolicy()
                    policy.setRetainSizeWhenHidden(True)
                    button.setSizePolicy(policy)
                    button.setEnabled(False)
                    button.hide()
                layout.addWidget(button)
        return row

    def _fill_add_combo(self) -> None:
        combo = self.add_combo
        blocker = combo.blockSignals(True)
        combo.clear()
        if self.devices is None:
            combo.addItem("Looking for microphones…", None)
        else:
            taken = {self._match(entry) for entry in self.priority} - {None}
            choices = [device for device in self.devices if _listed(device) and device not in taken]
            combo.addItem("Add a microphone…" if choices else "No other microphones found", None)
            for device in choices:
                combo.addItem(device.display, entry_token(device.key))
        combo.setEnabled(combo.count() > 1)
        combo.setCurrentIndex(0)
        combo.blockSignals(blocker)
