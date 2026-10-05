"""Settings → Remote engine: use a paired computer's engine, or share this one.

The page talks to ``RemoteEngineService`` (services/remote_asr/service.py).
Pairing and the tailnet search block on the network, so they run on worker
threads and report back through signals; host events arrive from server
threads and are re-posted to the UI thread the same way.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from services.remote_asr import protocol
from ui_qt.utils.palette import current_palette
from ui_qt.widgets import (
    Button,
    DangerButton,
    ElidingComboBox,
    FieldTile,
    InfoTile,
    NoWheelSpinBox,
    PrimaryButton,
    SettingTile,
    WrappedLabel,
)
from ui_qt.widgets.buttons import compact_primary_button, neutral_button
from ui_qt.widgets.segmented_bar import SegmentedBar

logger = logging.getLogger(__name__)

#: The engine's ``config.MODEL_CHOICES`` label.
REMOTE_ENGINE_DISPLAY = "Remote computer"
TAILSCALE_DOWNLOAD_URL = "https://tailscale.com/download"
#: Showing the page again within this long reuses the last tailnet search.
TAILNET_RESCAN_S = 20.0


def _paired_on(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%b %d, %Y").replace(" 0", " ")
    except (TypeError, ValueError):
        return ""


def _size(size: int) -> str:
    from services.format_utils import format_file_size

    return format_file_size(size)


def _stored_phrase(stored: dict) -> str:
    """``12 dictations and 3 meetings (1.2 GB)``, or "" when there are none."""
    parts = []
    total = 0
    for kind, one, many in (("dictation", "dictation", "dictations"), ("meeting", "meeting", "meetings")):
        summary = stored.get(kind) if isinstance(stored, dict) else None
        count = int((summary or {}).get("count") or 0)
        total += int((summary or {}).get("bytes") or 0)
        if count:
            parts.append(f"{count} {one if count == 1 else many}")
    if not parts:
        return ""
    return f"{' and '.join(parts)} ({_size(total)})" if total else " and ".join(parts)


#: The storage choices, in the order the switch shows them.
RECORD_LOCATIONS = ("local", "host", "both")


class _TileSection(QWidget):
    """Tiles that show or hide as one tab; their grids still reflow with the page."""

    def __init__(self, page: QWidget):
        super().__init__()
        self.setObjectName("remoteSection")
        self._page = page
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(0, 0, 0, 0)
        self.column.setSpacing(8)

    def add_tile_grid(self, grid, tiles, columns) -> None:
        self._page.add_tile_grid(grid, tiles, columns)


def _engine_phrase(engine: dict) -> str:
    """``Whisper turbo on cuda (int8_float32)``, as the main window's status says it."""
    label = str(engine.get("label") or "")
    if not label:
        return ""
    device = str(engine.get("device") or "")
    compute = str(engine.get("compute_type") or "")
    if not device:
        return label
    return f"{label} on {device} ({compute})" if compute else f"{label} on {device}"


class RemoteEngineSection(QObject):
    """Builds the page's tiles and keeps them in step with the service."""

    _service_event = pyqtSignal(str)
    #: (pairing or None, the exception or None, the NearbyHost it was for or None)
    _pair_finished = pyqtSignal(object, object, object)
    #: The six digits a pairing request shows while the host's owner decides.
    _pair_code = pyqtSignal(str)
    _scan_finished = pyqtSignal(object)
    _records_event = pyqtSignal(str)
    _records_job_done = pyqtSignal(str)
    _recovery_job_done = pyqtSignal(str)

    def __init__(self, parent=None, records=None):
        super().__init__(parent)
        self._service = None
        # The record sync (services/remote_records/sync.py); tests pass their own.
        if records is None:
            from services.remote_records.sync import record_sync

            records = record_sync
        self._records = records
        self._records_busy = ""
        self._records_note = ""
        self._records_listener = lambda kind: self._records_event.emit(kind)
        self._records_event.connect(self._on_records_event)
        self._records_job_done.connect(self._on_records_job_done)
        self._recovery_job_done.connect(self._on_recovery_job_done)
        self._recovery_busy = False
        self._select_engine: Optional[Callable[[str], None]] = None
        self._set_rail_value: Optional[Callable[[str], None]] = None
        self._pairing_busy = False
        self._built = False
        self._scan = None
        self._scan_busy = False
        self._scan_at = 0.0
        self._sweeping = False
        #: The address-and-code row is open (asked for, or "Use a code").
        self._manual = False
        #: While a pairing request waits on the host: {"name", "sas", "canceled"}.
        self._request = None
        self._listener = lambda kind: self._service_event.emit(kind)
        self._service_event.connect(self._on_service_event)
        self._pair_finished.connect(self._on_pair_finished)
        self._pair_code.connect(self._on_pair_code)
        self._scan_finished.connect(self._on_scan_finished)
        self._countdown = QTimer(self)
        self._countdown.setInterval(1000)
        self._countdown.timeout.connect(self._refresh_pairing_code)

    # ---- construction ----

    def build(self, dialog, layout: QVBoxLayout, icon: Callable[[str], QIcon]) -> None:
        self._set_rail_value = lambda text: dialog.rail.set_value("remote_engine", text)

        # Two audiences share this page: someone using another computer's
        # engine, and someone sharing this one. Tabs keep each to one screen.
        page = layout.parentWidget()
        self.tabs = SegmentedBar(
            (("Use another computer", "Not paired"), ("Share this computer", "Off"))
        )
        self.tabs.setAccessibleName("Remote engine role")
        self.client_container = _TileSection(page)
        self.host_container = _TileSection(page)
        self.host_container.hide()
        self._tab_chosen = False
        self.tabs.activated.connect(lambda _index: setattr(self, "_tab_chosen", True))
        self.tabs.currentIndexChanged.connect(self._show_section)
        layout.addWidget(self.tabs)
        layout.addWidget(self.client_container)
        layout.addWidget(self.host_container)

        # Client, before pairing: the computers nearby that share their
        # engine, found on this network and the tailnet; or an address and
        # code typed in.
        self.find_tile = InfoTile(
            "Find a computer",
            "",
            icon("world-blue.svg"),
        )
        self.find_tile.setProperty("tileId", "remoteFindTile")  # not setObjectName: the theme styles tiles by "settingsTile"
        self.find_list = QWidget()
        self.find_list.setObjectName("remoteFindList")
        self._find_layout = QVBoxLayout(self.find_list)
        self._find_layout.setContentsMargins(0, 0, 0, 0)
        self._find_layout.setSpacing(6)
        self.find_tile.add_body(self.find_list)

        # While the host's owner decides on a pairing request: the number
        # both screens show, and a way to stop waiting.
        self.wait_box = QWidget()
        self.wait_box.setObjectName("remoteWaitBox")
        wait_layout = QHBoxLayout(self.wait_box)
        wait_layout.setContentsMargins(0, 0, 0, 0)
        wait_layout.setSpacing(12)
        self.wait_code_label = QLabel("")
        self.wait_code_label.setObjectName("remoteWaitCode")
        self.wait_code_label.setAccessibleName("Pairing number")
        self.wait_label = WrappedLabel("")
        self.wait_label.setObjectName("remoteWaitLabel")
        self.wait_cancel_button = Button("Cancel")
        self.wait_cancel_button.setObjectName("remoteWaitCancelButton")
        self.wait_cancel_button.clicked.connect(self._cancel_request)
        wait_layout.addWidget(self.wait_code_label)
        wait_layout.addWidget(self.wait_label, stretch=1)
        wait_layout.addWidget(self.wait_cancel_button)
        self.wait_box.hide()
        self.find_tile.add_body(self.wait_box)

        find_actions = QHBoxLayout()
        find_actions.setContentsMargins(0, 0, 0, 0)
        self.search_button = Button("Search again")
        self.search_button.setObjectName("remoteSearchButton")
        self.search_button.clicked.connect(lambda: self.scan_nearby(force=True))
        self.sweep_button = Button("Check every address")
        self.sweep_button.setObjectName("remoteSweepButton")
        self.sweep_button.setToolTip(
            "Try each address on this network, for routers that hide computers from a search"
        )
        self.sweep_button.clicked.connect(lambda: self.scan_nearby(force=True, sweep=True))
        self.manual_button = Button("Enter an address")
        self.manual_button.setObjectName("remoteManualButton")
        self.manual_button.clicked.connect(self._show_manual)
        find_actions.addWidget(self.search_button)
        find_actions.addWidget(self.sweep_button)
        find_actions.addWidget(self.manual_button)
        find_actions.addStretch(1)
        self.find_tile.add_body_layout(find_actions)

        # Client: pair with a host, then select the engine.
        self.client_tile = InfoTile(
            "Paired host",
            "",
            icon("server-blue.svg"),
        )
        self.client_tile.setProperty("tileId", "remoteClientTile")  # not setObjectName: the theme styles tiles by "settingsTile"
        self.pair_row = QWidget()
        self.pair_row.setObjectName("remotePairRow")
        pair_layout = QHBoxLayout(self.pair_row)
        pair_layout.setContentsMargins(0, 0, 0, 0)
        pair_layout.setSpacing(8)
        self.address_edit = QLineEdit()
        self.address_edit.setObjectName("remoteAddressEdit")
        self.address_edit.setPlaceholderText("Host address, e.g. 192.168.1.20 or devbox.local")
        self.address_edit.setMinimumHeight(36)
        self.code_edit = QLineEdit()
        self.code_edit.setObjectName("remoteCodeEdit")
        self.code_edit.setPlaceholderText("Pairing code")
        self.code_edit.setMaxLength(12)
        self.code_edit.setMinimumHeight(36)
        self.code_edit.setMaximumWidth(150)
        self.code_edit.returnPressed.connect(self._pair)
        self.pair_button = PrimaryButton("Pair")
        self.pair_button.setObjectName("remotePairButton")
        self.pair_button.clicked.connect(self._pair)
        pair_layout.addWidget(self.address_edit, stretch=1)
        pair_layout.addWidget(self.code_edit)
        pair_layout.addWidget(self.pair_button)
        self.pair_row.hide()
        self.find_tile.add_body(self.pair_row)
        self.find_message = WrappedLabel("")
        self.find_message.setObjectName("remoteFindMessage")
        self.find_tile.add_body(self.find_message)

        self.paired_row = QWidget()
        self.paired_row.setObjectName("remotePairedRow")
        paired_layout = QVBoxLayout(self.paired_row)
        paired_layout.setContentsMargins(0, 0, 0, 0)
        paired_layout.setSpacing(8)
        self.use_button = PrimaryButton("Use for dictation")
        self.use_button.setObjectName("remoteUseButton")
        self.use_button.clicked.connect(self._use_engine)
        self.manage_button = Button("Manage host models")
        self.manage_button.setObjectName("remoteManageModelsButton")
        self.manage_button.clicked.connect(self._manage_models)
        self.forget_button = Button("Forget host")
        self.forget_button.setObjectName("remoteForgetButton")
        self.forget_button.clicked.connect(self._forget)
        use_row = QHBoxLayout()
        use_row.addWidget(self.use_button)
        self.meeting_use_button = Button("Use for meetings")
        self.meeting_use_button.setObjectName("remoteUseForMeetingsButton")
        def use_for_meetings():
            from ui_qt.dialogs.settings_destinations import MEETING_VOICE
            combo = dialog.models.meeting_source_combo
            combo.setCurrentIndex(combo.findData("remote"))
            dialog.select_destination(MEETING_VOICE)
        self.meeting_use_button.clicked.connect(use_for_meetings)
        use_row.addWidget(self.meeting_use_button)
        use_row.addStretch(1)
        paired_layout.addLayout(use_row)
        manage_row = QHBoxLayout()
        manage_row.addWidget(self.manage_button)
        manage_row.addWidget(self.forget_button)
        manage_row.addStretch(1)
        paired_layout.addLayout(manage_row)
        self.client_tile.add_body(self.paired_row)

        self.client_message = WrappedLabel("")
        self.client_message.setObjectName("remoteClientMessage")
        self.client_tile.add_body(self.client_message)
        # Paired on this network only: how to keep using it away from home.
        self.tailscale_hint = WrappedLabel("")
        self.tailscale_hint.setObjectName("remoteTailscaleHint")
        self.tailscale_hint.setTextFormat(Qt.TextFormat.RichText)
        self.tailscale_hint.setOpenExternalLinks(True)
        self.tailscale_hint.hide()
        self.client_tile.add_body(self.tailscale_hint)

        self.share_history_tile = SettingTile(
            "Allow the paired host to query this computer's history",
            "Agents connected to the paired host can search this computer's saved dictations "
            "and meetings and read their transcripts and insights while this app is running. "
            "Choose Both below to keep a copy available when this computer is offline.",
            icon("server-blue.svg"),
        )
        self.share_history_tile.setProperty("tileId", "remoteShareHistoryTile")  # not setObjectName: the theme styles tiles by "settingsTile"
        self.share_history_tile.checkbox.toggled.connect(self._on_share_history_toggled)

        # Client: where this computer's history, recordings and meetings go.
        self.storage_tile = InfoTile(
            "Where records are kept",
            "",
            icon("box-blue.svg"),
        )
        self.storage_tile.setProperty("tileId", "remoteStorageTile")  # not setObjectName: the theme styles tiles by "settingsTile"
        switch = QFrame()
        switch.setObjectName("recordsLocationSwitch")
        switch_layout = QHBoxLayout(switch)
        switch_layout.setContentsMargins(2, 2, 2, 2)
        switch_layout.setSpacing(2)
        self._location_group = QButtonGroup(self)
        self.location_buttons = {}
        for location, label in zip(RECORD_LOCATIONS, ("This computer", "Host", "Both")):
            button = QPushButton(label)
            button.setObjectName("recordsLocationBtn")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(30)
            button.clicked.connect(lambda _checked=False, value=location: self._choose_location(value))
            self._location_group.addButton(button)
            switch_layout.addWidget(button)
            self.location_buttons[location] = button
        switch_row = QHBoxLayout()
        switch_row.setContentsMargins(0, 0, 0, 0)
        switch_row.addWidget(switch)
        switch_row.addStretch(1)
        self.storage_tile.add_body_layout(switch_row)
        self.storage_status = WrappedLabel("")
        self.storage_status.setObjectName("remoteStorageStatus")
        self.storage_tile.add_body(self.storage_status)
        storage_actions = QHBoxLayout()
        storage_actions.setContentsMargins(0, 0, 0, 0)
        self.send_existing_button = Button("Move existing records")
        self.send_existing_button.setObjectName("remoteSendExistingButton")
        self.send_existing_button.clicked.connect(self._send_existing)
        self.bring_back_button = Button("Bring records back")
        self.bring_back_button.setObjectName("remoteBringBackButton")
        self.bring_back_button.clicked.connect(self._bring_back)
        self.retry_records_button = Button("Try again")
        self.retry_records_button.setObjectName("remoteRetryRecordsButton")
        self.retry_records_button.clicked.connect(lambda: self._records.wake(now=True))
        storage_actions.addWidget(self.send_existing_button)
        storage_actions.addWidget(self.bring_back_button)
        storage_actions.addWidget(self.retry_records_button)
        storage_actions.addStretch(1)
        self.storage_tile.add_body_layout(storage_actions)

        dialog._tile_group(
            self.client_container.column,
            "",
            [self.find_tile, self.client_tile, self.share_history_tile, self.storage_tile],
            columns=1,
            intro=(
                "Dictate or record meetings here while a faster computer does the transcription. "
                "On that computer, open Share this computer and turn on sharing; it then shows "
                "up below."
            ),
        )

        # Host: share the engine selected on this computer.
        self.share_tile = SettingTile(
            "Share this computer's engine",
            "Paired computers can dictate or transcribe meetings with the engine selected here, "
            "and switch it to any model downloaded here. The connection is encrypted and only "
            "computers you pair can use it. Windows may ask once to allow private networks.",
            icon("server-blue.svg"),
        )
        self.share_tile.setProperty("tileId", "remoteShareTile")  # not setObjectName: the theme styles tiles by "settingsTile"
        self.share_tile.checkbox.toggled.connect(self._on_share_toggled)
        self.host_status = WrappedLabel("")
        self.host_status.setObjectName("remoteHostStatus")
        self.share_tile.add_body(self.host_status)
        self.host_identity = WrappedLabel("")
        self.host_identity.setObjectName("remoteHostIdentity")
        self.host_identity.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.share_tile.add_body(self.host_identity)
        # What may keep other computers out (services/remote_asr/reachability.py).
        self.host_notes = QWidget()
        self.host_notes.setObjectName("remoteHostNotes")
        self._host_notes_layout = QVBoxLayout(self.host_notes)
        self._host_notes_layout.setContentsMargins(0, 0, 0, 0)
        self._host_notes_layout.setSpacing(6)
        self._host_notes_shown: tuple = ()
        self.host_notes.hide()
        self.share_tile.add_body(self.host_notes)

        self.tailscale_tile = SettingTile(
            "Pair my Tailscale computers without a code",
            "",
            icon("world-blue.svg"),
        )
        self.tailscale_tile.setProperty("tileId", "remoteTailscaleTile")  # not setObjectName: the theme styles tiles by "settingsTile"
        self.tailscale_tile.checkbox.toggled.connect(self._on_tailscale_trust_toggled)

        self.management_tile = SettingTile(
            "Allow paired computers to manage models",
            "Paired computers can browse the model catalog and download models and verified "
            "speech runtimes here, using this computer's network and storage. Its Hugging Face "
            "download policy still applies. Turning this off blocks new requests; downloads "
            "already started continue. Choosing an already-downloaded model never needs it.",
            icon("server-blue.svg"),
        )
        self.management_tile.setProperty("tileId", "remoteModelManagementTile")  # not setObjectName: the theme styles tiles by "settingsTile"
        self.management_tile.checkbox.toggled.connect(self._on_model_management_toggled)

        self.keep_records_tile = SettingTile(
            "Keep records for paired computers",
            "Paired computers can keep their dictation history, recordings and meetings here. "
            "They show in History and Past Meetings, marked with the computer they came from, "
            "and each computer can see and delete only its own. Turning this off stops new "
            "ones; what's kept stays until that computer brings it back or you remove it below.",
            icon("box-blue.svg"),
        )
        self.keep_records_tile.setProperty("tileId", "remoteKeepRecordsTile")  # not setObjectName: the theme styles tiles by "settingsTile"
        self.keep_records_tile.checkbox.toggled.connect(self._on_keep_records_toggled)

        self.manage_mcp_tile = SettingTile(
            "Allow paired computers to manage MCP",
            "Paired computers can turn this computer's MCP server on or off, change what "
            "assistants may do, and copy its access token. That token can read every saved "
            "dictation and meeting here, including other computers' records. Turning this off "
            "blocks new requests at once.",
            icon("key-blue.svg"),
        )
        self.manage_mcp_tile.setProperty("tileId", "remoteManageMcpTile")  # not setObjectName: the theme styles tiles by "settingsTile"
        self.manage_mcp_tile.checkbox.toggled.connect(self._on_manage_mcp_toggled)

        self.port_spin = NoWheelSpinBox()
        self.port_spin.setObjectName("remotePortSpin")
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(protocol.DEFAULT_PORT)
        self.port_spin.setMinimumHeight(40)
        self.port_spin.setMinimumWidth(120)
        self.port_spin.setKeyboardTracking(False)
        self.port_spin.valueChanged.connect(self._on_port_changed)
        self.port_tile = FieldTile(
            "Port",
            f"Clients connect to this port. {protocol.DEFAULT_PORT} unless "
            "something else already uses it.",
            self.port_spin,
            icon("bolt-green.svg"),
            compact=True,
        )

        self.devices_tile = InfoTile(
            "Paired computers",
            "Computers that can use this engine. One that finds this computer can ask to pair, "
            "and you're asked here to allow it. Removing one cuts it off at once.",
            icon("key-blue.svg"),
        )
        self.devices_tile.setProperty("tileId", "remoteDevicesTile")  # not setObjectName: the theme styles tiles by "settingsTile"
        self.pairing_box = QWidget()
        self.pairing_box.setObjectName("remotePairingBox")
        pairing_layout = QHBoxLayout(self.pairing_box)
        pairing_layout.setContentsMargins(0, 0, 0, 0)
        pairing_layout.setSpacing(12)
        self.pairing_code_label = QLabel("")
        self.pairing_code_label.setObjectName("remotePairingCode")
        for label in (self.pairing_code_label, self.wait_code_label):
            font = label.font()
            font.setPointSizeF(font.pointSizeF() * 1.8)
            font.setBold(True)
            font.setLetterSpacing(font.SpacingType.AbsoluteSpacing, 3)
            label.setFont(font)
        self.pairing_expiry_label = WrappedLabel("")
        self.pairing_expiry_label.setObjectName("remotePairingExpiry")
        self.cancel_pairing_button = Button("Cancel")
        self.cancel_pairing_button.setObjectName("remoteCancelPairingButton")
        self.cancel_pairing_button.clicked.connect(self._cancel_pairing)
        pairing_layout.addWidget(self.pairing_code_label)
        pairing_layout.addWidget(self.pairing_expiry_label, stretch=1)
        pairing_layout.addWidget(self.cancel_pairing_button)
        self.devices_tile.add_body(self.pairing_box)
        self.pair_device_button = PrimaryButton("Show a pairing code")
        self.pair_device_button.setToolTip(
            "For a computer that enters this one's address, or runs an older OpenWhisper"
        )
        self.pair_device_button.setObjectName("remotePairDeviceButton")
        self.pair_device_button.clicked.connect(self._open_pairing)
        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.addWidget(self.pair_device_button)
        button_row.addStretch(1)
        self.devices_tile.add_body_layout(button_row)
        self.devices_list = QWidget()
        self.devices_list.setObjectName("remoteDevicesList")
        self._devices_layout = QVBoxLayout(self.devices_list)
        self._devices_layout.setContentsMargins(0, 0, 0, 0)
        self._devices_layout.setSpacing(6)
        self.devices_tile.add_body(self.devices_list)
        self.recover_records_button = Button("Recover stored records…")
        self.recover_records_button.setObjectName("remoteRecoverRecordsButton")
        self.recover_records_button.setToolTip("Reconnect records from an old pairing to a paired computer")
        self.recover_records_button.clicked.connect(self._recover_records)
        recover_row = QHBoxLayout()
        recover_row.setContentsMargins(0, 0, 0, 0)
        recover_row.addWidget(self.recover_records_button)
        recover_row.addStretch(1)
        self.devices_tile.add_body_layout(recover_row)
        self.recovery_message = WrappedLabel("")
        self.recovery_message.setObjectName("infoLabel")
        self.recovery_message.setTextFormat(Qt.TextFormat.PlainText)
        self.recovery_message.hide()
        self.devices_tile.add_body(self.recovery_message)

        dialog._tile_group(
            self.host_container.column,
            "",
            [self.share_tile, self.management_tile, self.keep_records_tile,
             self.manage_mcp_tile, self.tailscale_tile, self.port_tile, self.devices_tile],
            columns=1,
        )
        for button in (self.pair_button, self.use_button, self.pair_device_button):
            compact_primary_button(button)
        for button in (
            self.manage_button, self.forget_button, self.meeting_use_button,
            self.send_existing_button, self.bring_back_button, self.retry_records_button,
            self.search_button, self.sweep_button, self.manual_button, self.wait_cancel_button,
            self.cancel_pairing_button, self.recover_records_button,
        ):
            neutral_button(button)
        self._built = True
        self.refresh()

    _UI_ATTRIBUTES = frozenset({'_set_rail_value', 'storage_status', 'find_list', 'cancel_pairing_button', 'manage_button', 'meeting_use_button', 'client_tile', 'paired_row', 'pair_row', 'share_tile', 'code_edit', 'devices_list', 'search_button', 'sweep_button', 'manual_button', 'find_message', 'wait_box', 'wait_code_label', 'wait_label', 'wait_cancel_button', 'tailscale_hint', 'host_notes', '_host_notes_layout', '_host_notes_shown', 'send_existing_button', 'pairing_code_label', 'client_message', 'tailscale_tile', 'pairing_expiry_label', 'retry_records_button', 'use_button', '_find_layout', 'devices_tile', 'forget_button', 'pair_device_button', 'bring_back_button', 'storage_tile', 'host_status', 'share_history_tile', 'keep_records_tile', 'manage_mcp_tile', 'tabs', 'client_container', 'host_container', 'find_tile', 'host_identity', 'management_tile', 'address_edit', '_built', '_location_group', '_devices_layout', 'pairing_box', 'pair_button', 'port_tile', 'location_buttons', 'port_spin'})

    def __getattr__(self, name):
        if name in self._UI_ATTRIBUTES and not self.__dict__.get("_built", False):
            host = self.parent()
            if host is not None and hasattr(host, "ensure_page"):
                from ui_qt.dialogs.settings_destinations import REMOTE_ENGINE
                host.ensure_page(REMOTE_ENGINE)
                if name in self.__dict__:
                    return self.__dict__[name]
        raise AttributeError(name)

    def bind(self, service, select_engine: Optional[Callable[[str], None]] = None) -> None:
        """Attach the controller's service (the page is built before one exists)."""
        self._select_engine = select_engine
        if service is not self._service:
            if self._service is not None:
                self._service.remove_listener(self._listener)
                self._records.remove_listener(self._records_listener)
            self._service = service
            self._scan = None
            self._scan_at = 0.0
            if service is not None:
                service.add_listener(self._listener)
                self._records.add_listener(self._records_listener)
        self.refresh()
        if self._built and self.client_container.isVisible():
            self.on_shown()

    def on_shown(self) -> None:
        """The page came into view: ask the host what it keeps, or look for hosts."""
        if self._service is None:
            return
        if self._service.client_pairing() is not None:
            threading.Thread(target=self._records.refresh_summary,
                             name="remote-records-summary", daemon=True).start()
            return
        if time.monotonic() - self._scan_at > TAILNET_RESCAN_S:
            self.scan_nearby()

    # ---- state ----

    def refresh(self) -> None:
        if not self._built:
            return
        service = self._service
        for widget in (self.client_tile, self.find_tile, self.share_tile,
                       self.management_tile, self.keep_records_tile, self.manage_mcp_tile,
                       self.tailscale_tile, self.port_tile, self.devices_tile):
            widget.setEnabled(service is not None)
        if service is None:
            self.client_tile.show()
            self.client_tile.set_description("The remote engine isn't available in this window.")
            self.paired_row.hide()
            self.pairing_box.hide()
            self.find_tile.hide()
            self.tailscale_tile.hide()
            self.storage_tile.hide()
            self.share_history_tile.hide()
            return
        self._refresh_client(service)
        from services.remote_asr.settings import client_shares_history

        self.share_history_tile.setVisible(service.client_pairing() is not None)
        checkbox = self.share_history_tile.checkbox
        blocked = checkbox.blockSignals(True)
        checkbox.setChecked(client_shares_history())
        checkbox.blockSignals(blocked)
        self._refresh_records()
        self._refresh_find(service)
        self._refresh_host(service)

    def _refresh_records(self) -> None:
        """The storage tile: the choice, what's on the host, what's under way."""
        if not self._built:
            return
        try:
            status = self._records.status()
        except Exception as exc:
            self.storage_tile.hide()
            logger.warning("Record sync status failed: %s", exc)
            return
        self.storage_tile.setVisible(self._service is not None and status.paired)
        if not status.paired:
            return
        host = status.host_name or "the host"
        host_button = self.location_buttons["host"]
        host_button.setText(host_button.fontMetrics().elidedText(
            host, Qt.TextElideMode.ElideMiddle, 160))
        host_button.setToolTip(f"Keep records on {host}")
        for location, button in self.location_buttons.items():
            button.setChecked(location == status.location)
            button.setEnabled(not self._records_busy)
        self.storage_tile.set_description({
            "local": "History, recordings and meetings stay on this computer.",
            "host": (f"New dictations and meetings move to {host} once it has checked "
                     "them. History and Past Meetings list them from there."),
            "both": (f"New dictations and meetings stay here and are copied to {host}. "
                     "Deleting one here deletes both copies."),
        }[status.location])

        stored = _stored_phrase(status.stored)
        lines = []
        if self._records_busy:
            lines.append(self._records_busy)
        elif status.active:
            lines.append(status.active)
        if status.host_supports is False and status.location != "local":
            lines.append(f"Update OpenWhisper on {host} to keep records there.")
        elif status.host_keeps is False and status.location != "local":
            lines.append(
                f"{host} isn't keeping records for other computers yet. On {host}, turn on "
                "\"Keep records for paired computers\" under Settings → Remote engine."
            )
        elif status.waiting and not status.active:
            lines.append(status.waiting)
        if status.pending and not status.active and not self._records_busy:
            lines.append(f"{status.pending} waiting to be sent.")
        if status.error and status.pending and not status.active:
            lines.append(status.error)
        if stored:
            lines.append(f"On {host}: {stored}.")
        if self._records_note:
            lines.append(self._records_note)
        self.storage_status.setText(" ".join(lines))
        self.storage_status.setVisible(bool(lines))

        blocked = bool(self._records_busy)
        self.send_existing_button.setVisible(status.location != "local")
        self.send_existing_button.setText(
            "Move existing records" if status.location == "host" else "Copy existing records"
        )
        self.send_existing_button.setToolTip(
            f"Send the records already on this computer to {host} too"
        )
        self.send_existing_button.setEnabled(not blocked and status.host_keeps is not False)
        has_stored = bool(stored)
        self.bring_back_button.setVisible(has_stored)
        self.bring_back_button.setToolTip(f"Move everything {host} keeps for this computer back here")
        self.bring_back_button.setEnabled(not blocked)
        self.retry_records_button.setVisible(bool(status.pending and status.error and not status.active))

    def _refresh_client(self, service) -> None:
        pairing = service.client_pairing()
        self.client_tile.setVisible(pairing is not None)
        self.find_tile.setVisible(pairing is None)
        if pairing is None:
            self.paired_row.hide()
            self.tailscale_hint.hide()
        else:
            identity = protocol.short_fingerprint(pairing.fingerprint)
            if pairing.over_tailscale:
                where = f"over Tailscale at {pairing.address}"
            else:
                where = f"at {pairing.address}"
                if pairing.tailscale_fallback:
                    where += f", or over Tailscale at {pairing.tailscale_fallback} when away"
            self.client_tile.set_description(
                f"Paired with {pairing.host_name} {where}. Its identity is {identity}."
            )
            self.paired_row.show()
            # Paired on this network only. Away from home is Tailscale's job;
            # say so once it's clear the home network works.
            home_only = (pairing.via != "tailscale" and not pairing.over_tailscale
                         and not pairing.tailscale_fallback)
            self.tailscale_hint.setVisible(home_only)
            if home_only:
                self.tailscale_hint.setText(
                    f"Works while both computers are on the same network. To use "
                    f"{pairing.host_name} away from home too, {self._tailscale_link('get Tailscale')} "
                    "on both computers and sign in with the same account."
                )
        self.pair_button.setEnabled(not self._pairing_busy)

    @staticmethod
    def _tailscale_link(text: str) -> str:
        return (f'<a href="{TAILSCALE_DOWNLOAD_URL}" style="color: '
                f'{current_palette().css("accent")}; text-decoration: underline;">{text}</a>')

    # ---- finding a computer ----

    def scan_nearby(self, force: bool = False, sweep: bool = False) -> None:
        """Look for computers sharing their engine, here and on the tailnet."""
        if self._service is None or self._scan_busy:
            return
        if not force and time.monotonic() - self._scan_at <= TAILNET_RESCAN_S:
            return
        self._scan_busy = True
        self._sweeping = sweep
        service = self._service

        def work():
            try:
                scan = service.scan_nearby(sweep=sweep)
            except Exception as exc:
                logger.warning("Looking for computers to pair with failed: %s", exc)
                scan = exc
            self._scan_finished.emit(scan)

        threading.Thread(target=work, name="remote-engine-nearby-scan", daemon=True).start()
        self.refresh()

    def _on_scan_finished(self, scan) -> None:
        self._scan_busy = False
        self._sweeping = False
        self._scan_at = time.monotonic()
        self._scan = None if isinstance(scan, Exception) else scan
        self.refresh()

    def _clear_find_rows(self) -> None:
        while self._find_layout.count():
            item = self._find_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Hidden now: deleteLater waits for the event loop, and a row
                # taken out of the layout would paint over the new ones until then.
                widget.hide()
                widget.deleteLater()

    def _find_note(self, text: str, link: bool = False) -> None:
        note = WrappedLabel(text)
        note.setObjectName("remoteFindNote")
        if link:
            note.setTextFormat(Qt.TextFormat.RichText)
            note.setOpenExternalLinks(True)
        self._find_layout.addWidget(note)

    def _refresh_find(self, service) -> None:
        if service.client_pairing() is not None:
            return
        self._clear_find_rows()
        waiting = self._request is not None
        self.wait_box.setVisible(waiting and bool(self._request.get("sas")))
        self.pair_row.setVisible(self._manual and not waiting)
        self.manual_button.setVisible(not self._manual and not waiting)
        self.search_button.setEnabled(not self._scan_busy and not self._pairing_busy)
        self.sweep_button.setEnabled(not self._scan_busy and not self._pairing_busy)
        scan = self._scan
        status = scan.status if scan is not None else service.tailscale_status()
        tailnet = status is not None and status.running
        where = "on this network and your tailnet" if tailnet else "on this network"
        if self._scan_busy:
            self.find_tile.set_description(
                "Checking every address on this network..." if self._sweeping
                else f"Looking for computers {where}..."
            )
        else:
            description = f"Computers {where} that share their engine."
            if tailnet and status.owner:
                description += f" Signed in to Tailscale as {status.owner}."
            self.find_tile.set_description(description)
        hosts = scan.hosts if scan is not None else ()
        self.sweep_button.setVisible(scan is not None and not hosts and not scan.swept)
        for host in hosts:
            self._find_layout.addWidget(self._find_row(host))
        if scan is None or hosts or self._scan_busy:
            return
        self._find_note(
            "None found. On the computer you want to use, open Settings → Remote engine → "
            "Share this computer and turn on sharing."
            + (" Checked every address on this network as well." if scan.swept else
               " If it's on, your router may be hiding it from the search: try Check every "
               "address, or enter its address.")
        )
        if status is not None and not tailnet:
            reason = {
                "stopped": "Tailscale is installed but turned off; turn it on to find your "
                           "computers on other networks too.",
                "needs_login": "Tailscale is installed but not signed in; sign in to find your "
                               "computers on other networks too.",
                "starting": "Tailscale is still starting.",
            }.get(status.state)
            if reason is None and status.state == "not_installed":
                self._find_note(
                    f"On a different network? {self._tailscale_link('Get Tailscale')} on both "
                    "computers and sign in with the same account.",
                    link=True,
                )
            elif reason is not None:
                self._find_note(reason)

    def _find_row(self, host) -> QWidget:
        row = QWidget()
        row.setObjectName("remoteFindRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        details = [host.name]
        engine = _engine_phrase(host.engine)
        if not host.compatible:
            details.append("needs an OpenWhisper update to pair")
        elif engine and host.engine.get("available"):
            details.append(engine)
        else:
            details.append("sharing, but no engine is ready")
        peer = host.tailnet.peer if host.tailnet is not None else None
        if peer is not None and peer.mine:
            details.append("your computer")
        elif peer is not None and peer.owner:
            details.append(f"shared by {peer.owner}")
        elif host.lan is not None:
            details.append("on this network")
        label = WrappedLabel(" · ".join(details))
        label.setObjectName("remoteFindLabel")
        label.setToolTip(host.address)
        row_layout.addWidget(label, stretch=1)
        if host.compatible and (host.can_pair_without_code or host.approval):
            button = compact_primary_button(PrimaryButton("Connect"))
            button.setObjectName("remoteFindConnectButton")
            button.clicked.connect(lambda _checked=False, h=host: self._connect(h))
        else:
            button = neutral_button(Button("Use a code"))
            button.setObjectName("remoteFindCodeButton")
            button.clicked.connect(lambda _checked=False, h=host: self._use_code(h))
        button.setEnabled(host.compatible and not self._pairing_busy)
        row_layout.addWidget(button)
        return row

    def _refresh_host(self, service) -> None:
        state = service.host_state()
        checkbox = self.share_tile.checkbox
        blocked = checkbox.blockSignals(True)
        checkbox.setChecked(bool(state["enabled"]))
        self.share_tile._sync_checked_property(bool(state["enabled"]))
        checkbox.blockSignals(blocked)
        blocked = self.port_spin.blockSignals(True)
        self.port_spin.setValue(int(state["port"] or protocol.DEFAULT_PORT))
        self.port_spin.blockSignals(blocked)

        for tile, key in ((self.management_tile, "model_management"),
                          (self.keep_records_tile, "keep_records"),
                          (self.manage_mcp_tile, "manage_mcp")):
            checkbox = tile.checkbox
            blocked = checkbox.blockSignals(True)
            checkbox.setChecked(state.get(key) is True)
            tile._sync_checked_property(checkbox.isChecked())
            checkbox.blockSignals(blocked)

        running = state["running"]
        where = protocol.format_address(state["address"] or state["host_name"], state["port"])
        if state.get("address_kind") == "vpn":
            where += " (a VPN address; computers on your network may not reach it)"
        tailnet = state.get("tailscale")
        on_tailnet = tailnet is not None and tailnet.running and bool(tailnet.address)
        if on_tailnet:
            where += f", and on Tailscale as {tailnet.name or tailnet.address} ({tailnet.address})"
        if state["error"]:
            status = state["error"]
        elif running:
            engine = state["engine"] or {}
            if engine.get("available"):
                status = f"Sharing {_engine_phrase(engine)} at {where}."
            else:
                status = (
                    f"Sharing is on at {where}, but no engine is ready to serve: "
                    f"{engine.get('status') or 'select a local engine'}."
                )
        elif state["enabled"]:
            status = "Starting..."
        else:
            status = ""
        self.host_status.setText(status)
        self.host_status.setVisible(bool(status))
        if running and state["fingerprint"]:
            self.host_identity.setText(
                f"This computer's identity: {protocol.short_fingerprint(state['fingerprint'])}. "
                "A computer that pairs should show the same."
            )
            self.host_identity.show()
        else:
            self.host_identity.hide()
        self._show_host_notes(state.get("notes") or [] if running else [])

        # Only offered where it can work: Tailscale running under a person's
        # account (tagged servers have no owner to compare against).
        self.tailscale_tile.setVisible(on_tailnet and bool(tailnet.owner))
        if on_tailnet and tailnet.owner:
            checkbox = self.tailscale_tile.checkbox
            blocked = checkbox.blockSignals(True)
            checkbox.setChecked(bool(state["tailscale_trust"]))
            self.tailscale_tile._sync_checked_property(bool(state["tailscale_trust"]))
            checkbox.blockSignals(blocked)
            self.tailscale_tile.set_description(
                f"Computers signed in to Tailscale as {tailnet.owner} can pair by "
                "picking this one from their tailnet. Anyone else on your tailnet "
                "still asks first, and you allow it here."
            )

        self.pair_device_button.setEnabled(running)
        self._show_pairing(state["pairing"] if running else None)
        self._rebuild_devices(state["devices"], {c["device_id"] for c in state["clients"]})
        self.recover_records_button.setEnabled(
            not self._recovery_busy and hasattr(service, "recover_device_records")
        )

        pairing = service.client_pairing()
        if running:
            rail = f"Sharing · {len(state['devices'])} paired"
        elif pairing is not None:
            rail = f"Paired · {pairing.host_name}"
        else:
            rail = "Off"
        if self._set_rail_value is not None:
            self._set_rail_value(rail)
        self._refresh_tabs(pairing, state, running)

    def _refresh_tabs(self, pairing, state, running: bool) -> None:
        """Say on each tab what is going on there, and open on the one in use."""
        self.tabs.set_detail(0, f"Paired · {pairing.host_name}" if pairing else "Not paired")
        self.tabs.set_detail(1, (
            f"Sharing · {len(state['devices'])} paired" if running
            else "Starting…" if state["enabled"] else "Off"
        ))
        if not self._tab_chosen and pairing is None and (running or state["enabled"]):
            # Sharing and not using another computer: the host side is the one in use.
            self.tabs.setCurrentIndex(1)

    def _show_section(self, index: int) -> None:
        self.client_container.setVisible(index == 0)
        self.host_container.setVisible(index == 1)
        if index == 0 and self._service is not None and self._service.client_pairing() is None:
            # The page may have opened on the sharing tab; search on first look here.
            self.scan_nearby()

    def _show_host_notes(self, notes: list) -> None:
        """What may keep other computers out, each with its fix (rebuilt only on change)."""
        shown = tuple((note.kind, note.message, note.action, note.url) for note in notes)
        if shown == self._host_notes_shown:
            return
        self._host_notes_shown = shown
        while self._host_notes_layout.count():
            item = self._host_notes_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        for note in notes:
            row = QWidget()
            row.setObjectName("remoteHostNoteRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            label = WrappedLabel(note.message)
            label.setObjectName("remoteHostNote")
            row_layout.addWidget(label, stretch=1)
            if note.action and note.url:
                button = neutral_button(Button(note.action))
                button.setObjectName("remoteHostNoteButton")
                button.clicked.connect(lambda _checked=False, url=note.url: self._open_url(url))
                row_layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignTop)
            self._host_notes_layout.addWidget(row)
        self.host_notes.setVisible(bool(notes))

    @staticmethod
    def _open_url(url: str) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl(url))

    def _show_pairing(self, pairing) -> None:
        # The code replaces the button while it is valid.
        self.pair_device_button.setVisible(pairing is None)
        if pairing is None:
            self.pairing_box.hide()
            self._countdown.stop()
            return
        code, left = pairing
        self.pairing_code_label.setText(f"{code[:3]} {code[3:]}")
        minutes, seconds = divmod(int(left), 60)
        self.pairing_expiry_label.setText(
            f"Enter this code on the other computer. Expires in {minutes}:{seconds:02d}."
        )
        self.pairing_box.show()
        if not self._countdown.isActive():
            self._countdown.start()

    def _refresh_pairing_code(self) -> None:
        if self._service is None:
            self._countdown.stop()
            return
        state = self._service.host_state()
        self._show_pairing(state["pairing"] if state["running"] else None)

    def _rebuild_devices(self, devices: list, connected: set) -> None:
        while self._devices_layout.count():
            item = self._devices_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not devices:
            empty = WrappedLabel("No computers are paired yet.")
            empty.setObjectName("infoLabel")
            self._devices_layout.addWidget(empty)
            return
        for device in devices:
            row = QWidget()
            row.setObjectName("remoteDeviceRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            details = [device.get("name") or "Unnamed computer"]
            if device.get("id") in connected:
                details.append("connected now")
            paired = _paired_on(device.get("paired_at", ""))
            if paired:
                via = " over Tailscale" if device.get("via") == "tailscale" else ""
                details.append(f"paired {paired}{via}")
            stored = _stored_phrase(self._device_records(device.get("id")))
            if stored:
                details.append(f"keeps {stored} here")
            label = WrappedLabel(" · ".join(details))
            label.setObjectName("remoteDeviceLabel")
            remove = DangerButton("Remove")
            remove.setObjectName("remoteRemoveDeviceButton")
            device_id = device.get("id")
            remove.clicked.connect(lambda _checked=False, d=device_id: self._remove_device(d))
            row_layout.addWidget(label, stretch=1)
            row_layout.addWidget(remove)
            self._devices_layout.addWidget(row)

    # ---- actions ----

    def _say(self, text: str) -> None:
        # One line in each state's tile, so a message survives the switch
        # between finding a computer and being paired with one.
        self.client_message.setText(text)
        self.find_message.setText(text)

    def _show_manual(self) -> None:
        self._manual = True
        self.refresh()
        self.address_edit.setFocus()

    def _pair(self) -> None:
        if self._service is None or self._pairing_busy:
            return
        address = self.address_edit.text()
        code = self.code_edit.text()
        try:
            protocol.parse_address(address)
        except ValueError as exc:
            self._say(str(exc))
            return
        if not "".join(ch for ch in code if ch.isdigit()):
            self._say("Enter the pairing code shown on the host.")
            return
        self._start_pairing(address, code, tailscale=False)

    def _start_pairing(self, address: str, code: Optional[str], *, tailscale: bool,
                       host=None) -> None:
        self._pairing_busy = True
        self.pair_button.setEnabled(False)
        self._say("Pairing...")
        service = self._service

        def work():
            try:
                pairing = service.pair(address, code, tailscale=tailscale)
            except Exception as exc:
                self._pair_finished.emit(None, exc, host)
            else:
                self._pair_finished.emit(pairing, None, host)

        threading.Thread(target=work, name="remote-engine-pair", daemon=True).start()
        self.refresh()

    def _connect(self, host) -> None:
        """Pair with a computer from the list: through Tailscale, or by asking it."""
        if self._service is None or self._pairing_busy:
            return
        if host.can_pair_without_code:
            self._start_pairing(host.address, None, tailscale=True, host=host)
        else:
            self._request_pairing(host)

    def _request_pairing(self, host) -> None:
        """Ask the host's owner to allow this computer; both screens show a number."""
        canceled = threading.Event()
        self._pairing_busy = True
        self._request = {"name": host.name, "sas": "", "canceled": canceled}
        self.pair_button.setEnabled(False)
        self._say(f"Asking {host.name}...")
        service = self._service

        def work():
            try:
                pairing = service.request_pairing(
                    host.address, on_code=self._pair_code.emit, canceled=canceled
                )
            except Exception as exc:
                self._pair_finished.emit(None, exc, host)
            else:
                self._pair_finished.emit(pairing, None, host)

        threading.Thread(target=work, name="remote-engine-pair-request", daemon=True).start()
        self.refresh()

    def _on_pair_code(self, sas: str) -> None:
        if self._request is None:
            return
        self._request["sas"] = sas
        name = self._request["name"]
        self.wait_code_label.setText(protocol.format_sas(sas))
        self.wait_label.setText(
            f"On {name}, click Allow if it shows this number. If it shows a different "
            "one, click Deny there: something else is answering for it."
        )
        self._say(f"Waiting for {name} to allow this computer...")
        self.refresh()

    def _cancel_request(self) -> None:
        if self._request is not None:
            self._request["canceled"].set()
            self._say("Canceling...")

    def _use_code(self, host) -> None:
        """A host that can't take requests (an older OpenWhisper): type its code."""
        self._manual = True
        self.address_edit.setText(host.address)
        self.refresh()
        self.code_edit.setFocus()
        self._say(
            f"On {host.name}, click \"Show a pairing code\" under Settings → Remote engine → "
            "Share this computer (\"Pair a device\" in older versions), then enter the code "
            "here and click Pair."
        )

    def _pair_error(self, exc, host) -> str:
        from services.remote_asr.client import PairingCanceled, RemoteUnreachable

        if isinstance(exc, PairingCanceled):
            return "Pairing canceled."
        if isinstance(exc, RemoteUnreachable) and host is not None and host.lan is not None \
                and not exc.refused:
            # It answered the search, so it is on this network; the
            # connection itself is what's being stopped.
            return (
                f"{host.name} answered the search but didn't accept the connection. Its "
                f"firewall may be blocking OpenWhisper: on {host.name}, open Settings → Remote "
                "engine → Share this computer to see what to change."
            )
        return str(exc) or type(exc).__name__

    def _on_pair_finished(self, pairing, error, host) -> None:
        self._pairing_busy = False
        self._request = None
        self.wait_box.hide()
        self.pair_button.setEnabled(True)
        if error is not None:
            self._say(self._pair_error(error, host))
        elif pairing.via == "tailscale":
            self._say(
                f"Paired with {pairing.host_name} through your Tailscale account. "
                "Click \"Use for dictation\" to start using it."
            )
        elif pairing.via == "approval":
            self._manual = False
            self._say(
                f"Paired with {pairing.host_name}. Click \"Use for dictation\" to start using it."
            )
        else:
            self._manual = False
            self.code_edit.clear()
            self._say(
                f"Paired with {pairing.host_name}. Check that its identity, "
                f"{protocol.short_fingerprint(pairing.fingerprint)}, matches the one "
                "shown there."
            )
        self.refresh()

    def _use_engine(self) -> None:
        if self._select_engine is not None:
            self._select_engine(REMOTE_ENGINE_DISPLAY)
            pairing = self._service.client_pairing() if self._service else None
            if pairing is not None:
                self._say(f"Dictation now uses {pairing.host_name}'s engine.")

    def _manage_models(self) -> None:
        if self._service is None:
            return
        pairing = self._service.client_pairing()
        if pairing is None:
            return
        from ui_qt.dialogs.remote_models import RemoteModelsDialog

        dialog = RemoteModelsDialog(self._service, pairing.host_name, self.client_tile.window())
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        self._models_dialog = dialog

    def _forget(self) -> None:
        if self._service is not None:
            status = self._records.status()
            stored = _stored_phrase(status.stored)
            if stored:
                host = status.host_name or "the host"
                answer = QMessageBox.question(
                    self.client_tile.window(),
                    "Forget host",
                    f"{host} keeps {stored} of this computer's. Once you forget it, "
                    f"this computer can't list or bring them back; they stay on {host}. "
                    "Forget it anyway?",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self._service.forget_host()
            self._say("Forgot the host. Pair again to use it.")
            self.scan_nearby(force=True)

    def _on_share_toggled(self, checked: bool) -> None:
        if self._service is None:
            return
        self._service.set_host_enabled(checked)
        state = self._service.host_state() if checked else None
        if state is not None and state["running"] and not state["devices"] and not state["pairing"]:
            # Someone turning sharing on is about to pair a computer; have a
            # code ready for one that types it, as well as for its request.
            self._service.open_pairing()
            self.refresh()

    def _on_model_management_toggled(self, checked: bool) -> None:
        if self._service is not None:
            self._service.set_model_management(checked)

    def _on_keep_records_toggled(self, checked: bool) -> None:
        if self._service is not None:
            self._service.set_keep_records(checked)

    def _on_manage_mcp_toggled(self, checked: bool) -> None:
        if self._service is not None:
            self._service.set_manage_mcp(checked)

    def _on_share_history_toggled(self, checked: bool) -> None:
        if self._service is not None:
            self._service.set_share_history(checked)

    # ---- where records are kept ----

    def _choose_location(self, location: str) -> None:
        try:
            current = self._records.location()
            if location == current:
                return
            self._records.set_location(location)
        except Exception as exc:
            self._records_note = f"Couldn't change where records are kept: {exc}"
        else:
            status = self._records.status()
            host = status.host_name or "the host"
            self._records_note = {
                "local": (f"New records stay here. Those already on {host} stay there "
                          "until you bring them back."),
                "host": f"New records will move to {host}.",
                "both": f"New records will also be copied to {host}.",
            }[location]
        self._refresh_records()

    def _run_records_job(self, busy: str, work: Callable[[], str]) -> None:
        if self._records_busy:
            return
        self._records_busy = busy
        self._records_note = ""
        self._refresh_records()

        def run():
            try:
                message = work()
            except Exception as exc:
                message = str(exc) or type(exc).__name__
            self._records_job_done.emit(message)

        threading.Thread(target=run, name="remote-records-job", daemon=True).start()

    def _on_records_event(self, kind: str) -> None:
        if kind.startswith("progress:") and self._records_busy:
            self._records_busy = kind[len("progress:"):]
        self._refresh_records()

    def _on_records_job_done(self, message: str) -> None:
        self._records_busy = ""
        self._records_note = message
        self._refresh_records()

    def _send_existing(self) -> None:
        records = self._records

        def work() -> str:
            queued = records.send_existing()
            if not queued:
                return "Everything here is already on the host."
            return f"Queued {queued} record{'s' if queued != 1 else ''} to send."

        self._run_records_job("Queuing records…", work)

    def _bring_back(self) -> None:
        records = self._records
        status = records.status()
        host = status.host_name or "the host"
        if status.location != "local":
            answer = QMessageBox.question(
                self.storage_tile.window(),
                "Bring records back",
                f"Bring every record on {host} back to this computer, and keep new ones "
                "here from now on?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            records.set_location("local")

        def work() -> str:
            moved = records.bring_back(
                lambda text: self._records_event.emit("progress:" + text)
            )
            return f"Brought {moved} record{'s' if moved != 1 else ''} back from {host}."

        self._run_records_job(f"Bringing records back from {host}…", work)

    def _on_tailscale_trust_toggled(self, checked: bool) -> None:
        if self._service is not None:
            self._service.set_tailscale_trust(checked)

    def _on_port_changed(self, port: int) -> None:
        if self._service is not None:
            self._service.set_host_port(port)

    def _open_pairing(self) -> None:
        if self._service is not None:
            self._service.open_pairing()
            self.refresh()

    def _cancel_pairing(self) -> None:
        if self._service is not None:
            self._service.cancel_pairing()
            self.refresh()

    def _device_records(self, device_id) -> dict:
        if self._service is None or not device_id:
            return {}
        try:
            return self._service.records_summary(device_id)
        except Exception:
            logger.debug("Could not count a paired computer's records", exc_info=True)
            return {}

    def _remove_device(self, device_id: str) -> None:
        if self._service is None or not device_id:
            return
        stored = _stored_phrase(self._device_records(device_id))
        delete_records = False
        if stored:
            name = next((d.get("name") for d in self._service.host_state()["devices"]
                         if d.get("id") == device_id), None) or "This computer"
            box = QMessageBox(self.devices_tile.window())
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Remove paired computer")
            box.setText(f"{name} keeps {stored} here.")
            box.setInformativeText(
                "Keep them in this computer's History and Past Meetings, or delete them "
                "for good? Deleted records can't be recovered, and that computer can't "
                "bring them back."
            )
            keep = box.addButton("Keep its records", QMessageBox.ButtonRole.AcceptRole)
            delete = box.addButton("Delete its records", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(keep)
            box.exec()
            clicked = box.clickedButton()
            if clicked not in (keep, delete):
                return
            delete_records = clicked is delete
        try:
            self._service.remove_device(device_id, delete_records=delete_records)
        except Exception as exc:
            QMessageBox.warning(self.devices_tile.window(), "Could not remove computer", str(exc))
        self.refresh()

    def _recover_records(self) -> None:
        service = self._service
        if service is None or self._recovery_busy:
            return
        try:
            owners = service.recoverable_record_owners()
            devices = service.host_state()["devices"]
        except Exception as exc:
            QMessageBox.warning(self.devices_tile.window(), "Could not load stored records", str(exc))
            return
        if not owners or not devices:
            QMessageBox.information(
                self.devices_tile.window(), "Recover stored records",
                "Pair the original computer again, then assign its old records here."
                if not devices else "No records from unpaired computers need recovery.",
            )
            return
        dialog = QDialog(self.devices_tile.window())
        dialog.setObjectName("remoteRecordRecoveryDialog")
        dialog.setWindowTitle("Recover stored records")
        dialog.setMinimumWidth(300)
        dialog.resize(460, 280)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        layout.addWidget(WrappedLabel(
            "Choose the old computer's records and its new pairing. "
            "The selected paired computer will be able to read, edit, and delete these records."
        ))
        source = ElidingComboBox()
        source.setObjectName("remoteRecoveryOwnerCombo")
        source.setAccessibleName("Stored records from old computer")
        for owner in owners:
            counts = {kind: {"count": count} for kind, count in owner["counts"].items()}
            source.addItem(f"{owner['name']} · {owner['id'][:8]} · {_stored_phrase(counts)}", owner["id"])
        layout.addWidget(QLabel("Stored records from"))
        layout.addWidget(source)
        target = ElidingComboBox()
        target.setObjectName("remoteRecoveryDeviceCombo")
        target.setAccessibleName("Paired computer to receive access")
        for device in devices:
            target.addItem(f"{device['name']} · {device['id'][:8]}", device["id"])
        layout.addWidget(QLabel("Assign to paired computer"))
        layout.addWidget(target)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        recover = buttons.addButton("Recover records", QDialogButtonBox.ButtonRole.AcceptRole)
        recover.setObjectName("primaryButton")
        recover.setDefault(False)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        owner_id, device_id = source.currentData(), target.currentData()
        self._recovery_busy = True
        self.recovery_message.setText("Recovering stored records…")
        self.recovery_message.show()
        self.refresh()

        def work():
            try:
                service.recover_device_records(owner_id, device_id)
                message = "Stored records are available to the paired computer. Refresh its history to see them."
            except Exception as exc:
                message = f"Could not recover records: {exc}"
            self._recovery_job_done.emit(message)

        threading.Thread(target=work, name="remote-record-recovery", daemon=True).start()

    def _on_recovery_job_done(self, message: str) -> None:
        self._recovery_busy = False
        self.recovery_message.setText(message)
        self.recovery_message.show()
        self.refresh()

    def _on_service_event(self, kind: str) -> None:
        # "activity" fires around every request a paired computer makes, and
        # nothing on this page shows it.
        if kind != "activity":
            self.refresh()
