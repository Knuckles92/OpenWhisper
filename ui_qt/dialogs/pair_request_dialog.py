"""Ask this computer's owner whether a computer on the network may pair.

A computer that found this one (services/remote_asr/discovery.py) asks to
pair, and the host holds the request (``SpeechHost._pair_request``) until
someone here answers. ``PairRequestPrompter`` watches the service and shows
``PairRequestDialog`` wherever the app is, with Settings open or not.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from services.remote_asr import protocol
from ui_qt.widgets import Button, WrappedLabel
from ui_qt.widgets.buttons import compact_primary_button, neutral_button

logger = logging.getLogger(__name__)


class PairRequestDialog(QDialog):
    """Allow or Deny, beside the number the asking computer shows too.

    Closing it denies, so the other computer isn't left waiting.
    ``answered`` carries the choice once; a request that goes away by
    itself (canceled there, timed out) is closed with ``dismiss``.
    """

    answered = pyqtSignal(bool)

    def __init__(self, request: dict, parent=None, grants=()):
        """``grants`` are what this host lets paired computers do beyond
        transcribing (``RemoteEngineService.pairing_grants``), said before
        the owner allows one."""
        super().__init__(parent)
        self.request_id = request["id"]
        self._done = False
        name = request.get("name") or "A computer"
        self.setObjectName("pairRequestDialog")
        self.setWindowTitle("Pair a computer")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        title = WrappedLabel(f"{name} wants to use this computer's engine")
        title.setObjectName("pairRequestTitle")
        font = title.font()
        font.setPointSizeF(font.pointSizeF() * 1.25)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        layout.addWidget(WrappedLabel(
            f"It's asking from {request.get('address') or 'this network'}. Allow it only "
            f"if {name} shows this same number:"
        ))
        self.code_label = QLabel(protocol.format_sas(str(request.get("sas") or "")))
        self.code_label.setObjectName("pairRequestCode")
        self.code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code_label.setAccessibleName("Pairing number")
        font = self.code_label.font()
        font.setPointSizeF(font.pointSizeF() * 2.4)
        font.setBold(True)
        font.setLetterSpacing(font.SpacingType.AbsoluteSpacing, 4)
        self.code_label.setFont(font)
        layout.addWidget(self.code_label)
        self.expiry_label = WrappedLabel("")
        self.expiry_label.setObjectName("pairRequestExpiry")
        layout.addWidget(self.expiry_label)
        grants = list(grants)
        also = ""
        if grants:
            listed = grants[0] if len(grants) == 1 else (
                ", ".join(grants[:-1]) + (", and " if len(grants) > 2 else " and ") + grants[-1]
            )
            also = f". It can also {listed}"
        self.grants_label = WrappedLabel(
            f"Once allowed, it can dictate and transcribe with the engine selected here{also}. "
            "You can remove it any time under Settings → Remote engine."
        )
        self.grants_label.setObjectName("pairRequestGrants")
        layout.addWidget(self.grants_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.deny_button = neutral_button(Button("Deny"))
        self.deny_button.setObjectName("pairRequestDenyButton")
        self.deny_button.clicked.connect(lambda: self._answer(False))
        # A plain Button: PrimaryButton's own large padding outranks the
        # compact tone outside Settings, and 34 px then cut its label off.
        self.allow_button = compact_primary_button(Button("Allow"))
        self.allow_button.setObjectName("pairRequestAllowButton")
        self.allow_button.clicked.connect(lambda: self._answer(True))
        # Deny is the safe key to fall on.
        self.deny_button.setDefault(True)
        buttons.addWidget(self.deny_button)
        buttons.addWidget(self.allow_button)
        layout.addLayout(buttons)

        self._deadline = time.monotonic() + float(request.get("seconds_left") or 0)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Opened a few pixels shorter than its own hint at larger font sizes,
        # which squeezed the buttons and cut their labels off.
        height = self.sizeHint().height()
        if height > self.height():
            self.resize(self.width(), height)

    def _tick(self) -> None:
        left = max(0, int(self._deadline - time.monotonic()))
        minutes, seconds = divmod(left, 60)
        self.expiry_label.setText(f"No answer by {minutes}:{seconds:02d} turns it down.")
        if left <= 0:
            self.dismiss()

    def _answer(self, allow: bool) -> None:
        if self._done:
            return
        self._done = True
        self._timer.stop()
        self.answered.emit(allow)
        self.close()

    def dismiss(self) -> None:
        """Close without answering: the request is gone already."""
        self._done = True
        self._timer.stop()
        self.close()

    def closeEvent(self, event) -> None:
        if not self._done:
            self._done = True
            self._timer.stop()
            self.answered.emit(False)
        super().closeEvent(event)


class PairRequestPrompter(QObject):
    """Shows a dialog for each pairing request the host gets, and passes the answer on."""

    _event = pyqtSignal(str)

    def __init__(self, service, parent_window=None):
        super().__init__()
        self._service = service
        self._window = parent_window if isinstance(parent_window, QWidget) else None
        self.dialog: Optional[PairRequestDialog] = None
        self._listener = lambda kind: self._event.emit(kind)
        self._event.connect(self._on_event)
        if hasattr(service, "add_listener"):
            service.add_listener(self._listener)

    def detach(self) -> None:
        if hasattr(self._service, "remove_listener"):
            self._service.remove_listener(self._listener)
        if self.dialog is not None:
            self.dialog.dismiss()

    def _on_event(self, kind: str) -> None:
        if kind in ("pair_request", "state"):
            self.sync()

    def sync(self) -> None:
        try:
            request = self._service.pending_pair_request()
        except Exception:
            logger.debug("Could not read the pending pairing request", exc_info=True)
            request = None
        dialog = self.dialog
        if dialog is not None and (request is None or request["id"] != dialog.request_id):
            self.dialog = None
            dialog.dismiss()
            dialog.deleteLater()
        if request is None or self.dialog is not None:
            return
        try:
            grants = self._service.pairing_grants() if hasattr(self._service, "pairing_grants") else []
        except Exception:
            logger.debug("Could not read what paired computers may do", exc_info=True)
            grants = []
        dialog = PairRequestDialog(request, self._window, grants)
        dialog.answered.connect(
            lambda allow, request_id=request["id"]: self._service.answer_pair_request(request_id, allow)
        )
        self.dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        QApplication.alert(dialog)
