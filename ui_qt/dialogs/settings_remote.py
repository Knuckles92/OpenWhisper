"""Settings → Remote engine: use a paired computer's engine, or share this one.

The page talks to ``RemoteEngineService`` (services/remote_asr/service.py).
Pairing and the tailnet search block on the network, so they run on worker
threads and report back through signals; host events arrive from server
threads and are re-posted to the UI thread the same way.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Callable, Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from services.remote_asr import protocol
from ui_qt.utils.palette import current_palette
from ui_qt.widgets import (
    Button,
    DangerButton,
    FieldTile,
    InfoTile,
    NoWheelSpinBox,
    PrimaryButton,
    SettingTile,
    WrappedLabel,
)

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


def _engine_phrase(engine: dict) -> str:
    label = str(engine.get("label") or "")
    if not label:
        return ""
    device = str(engine.get("device") or "")
    return f"{label} on {device}" if device else label


class RemoteEngineSection(QObject):
    """Builds the page's tiles and keeps them in step with the service."""

    _service_event = pyqtSignal(str)
    _pair_finished = pyqtSignal(object, str)
    _scan_finished = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._service = None
        self._select_engine: Optional[Callable[[str], None]] = None
        self._set_rail_value: Optional[Callable[[str], None]] = None
        self._pairing_busy = False
        self._built = False
        self._scan = None
        self._scan_busy = False
        self._scan_at = 0.0
        self._listener = lambda kind: self._service_event.emit(kind)
        self._service_event.connect(self._on_service_event)
        self._pair_finished.connect(self._on_pair_finished)
        self._scan_finished.connect(self._on_scan_finished)
        self._countdown = QTimer(self)
        self._countdown.setInterval(1000)
        self._countdown.timeout.connect(self._refresh_pairing_code)

    # ---- construction ----

    def build(self, dialog, layout: QVBoxLayout, icon: Callable[[str], QIcon]) -> None:
        self._set_rail_value = lambda text: dialog.rail.set_value("remote_engine", text)

        # Client: pair with a host, then select the engine.
        self.client_tile = InfoTile(
            "Paired host",
            "",
            icon("server-blue.svg"),
        )
        self.client_tile.setObjectName("remoteClientTile")
        self.pair_row = QWidget()
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
        self.client_tile.add_body(self.pair_row)

        self.paired_row = QWidget()
        paired_layout = QHBoxLayout(self.paired_row)
        paired_layout.setContentsMargins(0, 0, 0, 0)
        paired_layout.setSpacing(8)
        self.use_button = PrimaryButton("Use for dictation")
        self.use_button.setObjectName("remoteUseButton")
        self.use_button.clicked.connect(self._use_engine)
        self.forget_button = Button("Forget host")
        self.forget_button.setObjectName("remoteForgetButton")
        self.forget_button.clicked.connect(self._forget)
        paired_layout.addWidget(self.use_button)
        paired_layout.addWidget(self.forget_button)
        paired_layout.addStretch(1)
        self.client_tile.add_body(self.paired_row)

        self.client_message = WrappedLabel("")
        self.client_message.setObjectName("remoteClientMessage")
        self.client_tile.add_body(self.client_message)

        # Client, over Tailscale: computers on the tailnet that are sharing.
        self.tailnet_tile = InfoTile(
            "Computers on your tailnet",
            "",
            icon("world-blue.svg"),
        )
        self.tailnet_tile.setObjectName("remoteTailnetTile")
        self.tailnet_list = QWidget()
        self.tailnet_list.setObjectName("remoteTailnetList")
        self._tailnet_layout = QVBoxLayout(self.tailnet_list)
        self._tailnet_layout.setContentsMargins(0, 0, 0, 0)
        self._tailnet_layout.setSpacing(6)
        self.tailnet_tile.add_body(self.tailnet_list)
        self.tailnet_search_button = Button("Search again")
        self.tailnet_search_button.setObjectName("remoteTailnetSearchButton")
        self.tailnet_search_button.clicked.connect(lambda: self.scan_tailnet(force=True))
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.addWidget(self.tailnet_search_button)
        search_row.addStretch(1)
        self.tailnet_tile.add_body_layout(search_row)

        dialog._tile_group(
            layout,
            "Use another computer",
            [self.client_tile, self.tailnet_tile],
            columns=1,
            intro=(
                "Dictate here while a faster computer does the transcription, on "
                "your network or anywhere over Tailscale. On that computer, turn "
                "on sharing below; then pick it from your tailnet, or enter its "
                "address and pairing code."
            ),
        )

        # Host: share the engine selected on this computer.
        self.share_tile = SettingTile(
            "Share this computer's engine",
            "Paired computers can dictate with the engine selected on this "
            "computer. The connection is encrypted and only computers you pair "
            "can use it. Windows may ask once whether to allow OpenWhisper on "
            "private networks.",
            icon("server-blue.svg"),
        )
        self.share_tile.setObjectName("remoteShareTile")
        self.share_tile.checkbox.toggled.connect(self._on_share_toggled)
        self.host_status = WrappedLabel("")
        self.host_status.setObjectName("remoteHostStatus")
        self.share_tile.add_body(self.host_status)
        self.host_identity = WrappedLabel("")
        self.host_identity.setObjectName("remoteHostIdentity")
        self.host_identity.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.share_tile.add_body(self.host_identity)

        self.tailscale_tile = SettingTile(
            "Pair my Tailscale computers without a code",
            "",
            icon("world-blue.svg"),
        )
        self.tailscale_tile.setObjectName("remoteTailscaleTile")
        self.tailscale_tile.checkbox.toggled.connect(self._on_tailscale_trust_toggled)

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
            "Computers that can use this engine. Removing one cuts it off at once.",
            icon("key-blue.svg"),
        )
        self.devices_tile.setObjectName("remoteDevicesTile")
        self.pairing_box = QWidget()
        pairing_layout = QHBoxLayout(self.pairing_box)
        pairing_layout.setContentsMargins(0, 0, 0, 0)
        pairing_layout.setSpacing(12)
        self.pairing_code_label = QLabel("")
        self.pairing_code_label.setObjectName("remotePairingCode")
        font = self.pairing_code_label.font()
        font.setPointSizeF(font.pointSizeF() * 1.8)
        font.setBold(True)
        font.setLetterSpacing(font.SpacingType.AbsoluteSpacing, 3)
        self.pairing_code_label.setFont(font)
        self.pairing_expiry_label = WrappedLabel("")
        self.pairing_expiry_label.setObjectName("remotePairingExpiry")
        self.cancel_pairing_button = Button("Cancel")
        self.cancel_pairing_button.setObjectName("remoteCancelPairingButton")
        self.cancel_pairing_button.clicked.connect(self._cancel_pairing)
        pairing_layout.addWidget(self.pairing_code_label)
        pairing_layout.addWidget(self.pairing_expiry_label, stretch=1)
        pairing_layout.addWidget(self.cancel_pairing_button)
        self.devices_tile.add_body(self.pairing_box)
        self.pair_device_button = PrimaryButton("Pair a device")
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

        dialog._tile_group(
            layout,
            "Share this computer",
            [self.share_tile, self.tailscale_tile, self.port_tile, self.devices_tile],
            columns=1,
        )
        self._built = True
        self.refresh()

    def bind(self, service, select_engine: Optional[Callable[[str], None]] = None) -> None:
        """Attach the controller's service (the page is built before one exists)."""
        self._select_engine = select_engine
        if service is not self._service:
            if self._service is not None:
                self._service.remove_listener(self._listener)
            self._service = service
            self._scan = None
            self._scan_at = 0.0
            if service is not None:
                service.add_listener(self._listener)
        self.refresh()
        if self._built and self.client_tile.isVisible():
            self.on_shown()

    def on_shown(self) -> None:
        """The page came into view: look for hosts on the tailnet if unpaired."""
        if self._service is None or self._service.client_pairing() is not None:
            return
        if time.monotonic() - self._scan_at > TAILNET_RESCAN_S:
            self.scan_tailnet()

    # ---- state ----

    def refresh(self) -> None:
        if not self._built:
            return
        service = self._service
        for widget in (self.client_tile, self.tailnet_tile, self.share_tile,
                       self.tailscale_tile, self.port_tile, self.devices_tile):
            widget.setEnabled(service is not None)
        if service is None:
            self.client_tile.set_description("The remote engine isn't available in this window.")
            self.pair_row.hide()
            self.paired_row.hide()
            self.pairing_box.hide()
            self.tailnet_tile.hide()
            self.tailscale_tile.hide()
            return
        self._refresh_client(service)
        self._refresh_tailnet(service)
        self._refresh_host(service)

    def _refresh_client(self, service) -> None:
        pairing = service.client_pairing()
        if pairing is None:
            self.client_tile.set_description(
                "Not paired. Enter the address and code shown on the other "
                "computer under \"Share this computer's engine\"."
            )
            self.pair_row.show()
            self.paired_row.hide()
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
            self.pair_row.hide()
            self.paired_row.show()
        self.pair_button.setEnabled(not self._pairing_busy)

    # ---- tailnet ----

    def scan_tailnet(self, force: bool = False) -> None:
        if self._service is None or self._scan_busy:
            return
        if not force and time.monotonic() - self._scan_at <= TAILNET_RESCAN_S:
            return
        self._scan_busy = True
        service = self._service

        def work():
            try:
                scan = service.scan_tailnet()
            except Exception as exc:
                scan = exc
            self._scan_finished.emit(scan)

        threading.Thread(target=work, name="remote-engine-tailnet-scan", daemon=True).start()
        self.refresh()

    def _on_scan_finished(self, scan) -> None:
        self._scan_busy = False
        self._scan_at = time.monotonic()
        self._scan = None if isinstance(scan, Exception) else scan
        self.refresh()

    def _clear_tailnet_rows(self) -> None:
        while self._tailnet_layout.count():
            item = self._tailnet_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _tailnet_note(self, text: str, link: bool = False) -> None:
        note = WrappedLabel(text)
        note.setObjectName("remoteTailnetNote")
        if link:
            note.setTextFormat(Qt.TextFormat.RichText)
            note.setOpenExternalLinks(True)
        self._tailnet_layout.addWidget(note)

    def _refresh_tailnet(self, service) -> None:
        if service.client_pairing() is not None:
            self.tailnet_tile.hide()
            return
        self.tailnet_tile.show()
        self._clear_tailnet_rows()
        scan = self._scan
        status = scan.status if scan is not None else service.tailscale_status()
        self.tailnet_search_button.setEnabled(not self._scan_busy)
        self.tailnet_search_button.setVisible(status is None or status.running)
        if status is None or (self._scan_busy and scan is None):
            self.tailnet_tile.set_description("Looking for computers on your tailnet...")
            return
        if status.state == "not_installed":
            self.tailnet_tile.set_description(
                "With Tailscale on both computers, this one can use the other's "
                "engine from anywhere, not only at home."
            )
            self._tailnet_note(
                f'<a href="{TAILSCALE_DOWNLOAD_URL}" style="color: '
                f'{current_palette().css("accent")}; text-decoration: underline;">'
                "Get Tailscale</a>, sign in on both computers with the same account, "
                "then come back here.",
                link=True,
            )
            return
        if not status.running:
            reason = {
                "stopped": "Tailscale is installed but turned off.",
                "needs_login": "Tailscale is installed but not signed in.",
                "starting": "Tailscale is still starting.",
            }.get(status.state, "Tailscale isn't answering right now.")
            self.tailnet_tile.set_description(
                f"{reason} Turn it on to find your computers from anywhere."
            )
            self.tailnet_search_button.show()
            return
        who = f" as {status.owner}" if status.owner else ""
        if self._scan_busy:
            self.tailnet_tile.set_description(f"Signed in to Tailscale{who}. Searching...")
        else:
            self.tailnet_tile.set_description(f"Signed in to Tailscale{who}.")
        hosts = scan.hosts if scan is not None else ()
        if scan is not None and not hosts:
            self._tailnet_note(
                "No computer on your tailnet is sharing its engine yet. Turn on "
                "\"Share this computer's engine\" there; OpenWhisper finds it on "
                f"port {protocol.DEFAULT_PORT}."
            )
        for host in hosts:
            self._tailnet_layout.addWidget(self._tailnet_row(host))

    def _tailnet_row(self, host) -> QWidget:
        row = QWidget()
        row.setObjectName("remoteTailnetRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        peer = host.peer
        details = [peer.name]
        engine = _engine_phrase(host.engine)
        if not host.compatible:
            details.append("needs an OpenWhisper update to pair")
        elif engine and host.engine.get("available"):
            details.append(engine)
        else:
            details.append("sharing, but no engine is ready")
        if peer.mine:
            details.append("your computer")
        elif peer.owner:
            details.append(f"shared by {peer.owner}")
        label = WrappedLabel(" · ".join(details))
        label.setObjectName("remoteTailnetLabel")
        row_layout.addWidget(label, stretch=1)
        if host.can_pair_without_code:
            button = PrimaryButton("Connect")
            button.setObjectName("remoteTailnetConnectButton")
            button.clicked.connect(lambda _checked=False, h=host: self._pair_tailscale(h))
        else:
            button = Button("Use a code")
            button.setObjectName("remoteTailnetCodeButton")
            button.clicked.connect(lambda _checked=False, h=host: self._pair_with_code(h))
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

        running = state["running"]
        where = protocol.format_address(state["address"] or state["host_name"], state["port"])
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
                "still needs a pairing code."
            )

        self.pair_device_button.setEnabled(running)
        self._show_pairing(state["pairing"] if running else None)
        self._rebuild_devices(state["devices"], {c["device_id"] for c in state["clients"]})

        pairing = service.client_pairing()
        if running:
            rail = f"Sharing · {len(state['devices'])} paired"
        elif pairing is not None:
            rail = f"Paired · {pairing.host_name}"
        else:
            rail = "Off"
        if self._set_rail_value is not None:
            self._set_rail_value(rail)

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
        self.client_message.setText(text)

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

    def _start_pairing(self, address: str, code: Optional[str], *, tailscale: bool) -> None:
        self._pairing_busy = True
        self.pair_button.setEnabled(False)
        self._say("Pairing...")
        service = self._service

        def work():
            try:
                pairing = service.pair(address, code, tailscale=tailscale)
            except Exception as exc:
                self._pair_finished.emit(None, str(exc) or type(exc).__name__)
            else:
                self._pair_finished.emit(pairing, "")

        threading.Thread(target=work, name="remote-engine-pair", daemon=True).start()
        self.refresh()

    def _pair_tailscale(self, host) -> None:
        if self._service is None or self._pairing_busy:
            return
        self._start_pairing(host.address, None, tailscale=True)

    def _pair_with_code(self, host) -> None:
        self.address_edit.setText(
            host.peer.address if host.port == protocol.DEFAULT_PORT else host.address
        )
        self.code_edit.setFocus()
        self._say(
            f"On {host.peer.name}, click \"Pair a device\", then enter the code it "
            "shows and click Pair."
        )

    def _on_pair_finished(self, pairing, error: str) -> None:
        self._pairing_busy = False
        self.pair_button.setEnabled(True)
        if error:
            self._say(error)
        elif pairing.via == "tailscale":
            self._say(
                f"Paired with {pairing.host_name} through your Tailscale account. "
                "Click \"Use for dictation\" to start using it."
            )
        else:
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

    def _forget(self) -> None:
        if self._service is not None:
            self._service.forget_host()
            self._say("Forgot the host. Pair again to use it.")
            self.scan_tailnet(force=True)

    def _on_share_toggled(self, checked: bool) -> None:
        if self._service is not None:
            self._service.set_host_enabled(checked)

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

    def _remove_device(self, device_id: str) -> None:
        if self._service is not None and device_id:
            self._service.remove_device(device_id)
            self.refresh()

    def _on_service_event(self, _kind: str) -> None:
        self.refresh()
