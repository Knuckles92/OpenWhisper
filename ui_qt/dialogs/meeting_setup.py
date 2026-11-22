"""First-run Meeting Mode setup shared by supported capture platforms."""

from __future__ import annotations

import logging
import math
import sys
import threading
from concurrent.futures import Future
from queue import Empty, SimpleQueue

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QDialog, QLabel, QProgressBar, QVBoxLayout, QWidget

from meeting.platform import (
    SCREEN_RECORDING_SETTINGS_URL,
    request_system_audio_permission,
    system_audio_permission_granted,
    system_audio_permission_required,
)
from services.meeting_setup import check_audio, inspect_models, prepare_models
from services.settings import SettingsKey, settings_manager
from ui_qt.widgets.buttons import Button, SuccessButton
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)


class MeetingSetupDialog(QDialog):
    def __init__(self, parent=None, *, starting: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Set up Meeting Mode")
        self.setMinimumWidth(420)
        self._models_ready = False
        self._audio_ready = False
        self._checked_audio = False
        self._permission_missing = False
        self._system_audio_verified = False
        self._future = None
        self._checking_audio = False
        self._pending_result = None
        self._cancel = threading.Event()
        self._progress = SimpleQueue()
        self._audio_progress = SimpleQueue()
        self._timer = QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._poll)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        title = self._label("Set up Meeting Mode")
        title.setObjectName("headerLabel")
        layout.addWidget(title)
        layout.addWidget(self._label(
            "Prepare your meeting speech and speaker models. Then speak and play "
            "sound through the output your calls use while the audio check runs. "
            "The test audio is discarded."
        ))
        self.model_status = self._label("Checking meeting models…")
        layout.addWidget(self.model_status)
        self.prepare_button = Button("Download missing files")
        self.prepare_button.clicked.connect(self._prepare)
        layout.addWidget(self.prepare_button)

        self.audio_status = self._label("Microphone and system audio have not been checked.")
        layout.addWidget(self.audio_status)
        self.audio_levels = QWidget()
        levels_layout = QVBoxLayout(self.audio_levels)
        levels_layout.setContentsMargins(0, 0, 0, 0)
        self.audio_meters = []
        for source in ("Microphone", "System audio"):
            meters_label = QLabel(source)
            levels_layout.addWidget(meters_label)
            meter = QProgressBar()
            meter.setAccessibleName(f"{source} input level")
            meter.setTextVisible(False)
            levels_layout.addWidget(meter)
            self.audio_meters.append(meter)
        self.audio_levels.hide()
        layout.addWidget(self.audio_levels)
        self.audio_button = Button("Check audio")
        self.audio_button.clicked.connect(self._check_audio)
        layout.addWidget(self.audio_button)
        self.audio_settings = Button("Open System Settings")
        self.audio_settings.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(SCREEN_RECORDING_SETTINGS_URL))
        )
        self.audio_settings.hide()
        layout.addWidget(self.audio_settings)

        # Stack actions so enlarged fonts and the microphone-only action fit
        # without squeezing button labels in a narrow dialog.
        footer = QVBoxLayout()
        cancel = Button("Back" if starting else "Close")
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        self.skip_button = Button("Start anyway")
        self.skip_button.setVisible(starting)
        self.skip_button.clicked.connect(self.accept)
        footer.addWidget(self.skip_button)
        self.start_hint = self._label("")
        footer.addWidget(self.start_hint)
        self.finish_button = SuccessButton("Start Meeting" if starting else "Done")
        self.finish_button.clicked.connect(self._finish)
        footer.addWidget(self.finish_button)
        layout.addLayout(footer)
        self._run(inspect_models, self._show_models, self.model_status)

    @property
    def system_audio_policy(self) -> str:
        if self._permission_missing:
            return "disabled"
        if sys.platform.startswith("linux") and self._system_audio_verified:
            return "required"
        return "auto"

    @staticmethod
    def _label(message):
        label = WrappedLabel(message, break_long_words=True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        return label

    def _run(self, work, done, status_label):
        # The worker only holds plain Python objects, so it can safely finish
        # after the dialog has closed. Qt is touched only by the polling timer.
        future = Future()

        def run():
            try:
                future.set_result(work())
            except Exception as exc:
                future.set_exception(exc)

        self._future = future
        self._done = done
        self._status_label = status_label
        self._update_buttons()
        threading.Thread(target=run, name="meeting-setup", daemon=True).start()
        self._timer.start()

    def _poll(self):
        try:
            while True:
                self._status_label.setText(self._progress.get_nowait())
        except Empty:
            pass
        if self._checking_audio and self._pending_result is None:
            self._show_audio_progress()
        if not self._future.done():
            return
        self._timer.stop()
        future, self._future = self._future, None
        self._checking_audio = False
        self.audio_levels.hide()
        if self._pending_result is not None:
            self.done(self._pending_result)
            return
        try:
            self._done(future.result())
        except Exception as exc:
            logger.warning("Meeting setup step failed", exc_info=True)
            self._status_label.setText(f"Couldn't complete this step. {exc}")
        self._update_buttons()

    def _prepare(self):
        cancel, report = self._cancel, self._progress.put
        self.model_status.setText("Preparing meeting models…")
        self._run(lambda: prepare_models(cancel, report), self._show_models, self.model_status)

    def _show_models(self, models):
        missing = models.missing
        self._models_ready = not missing
        text = f"Speech engine: {models.label}\n"
        if missing:
            text += "Still needed: " + ", ".join(missing)
        else:
            text += "Required local files are ready."
        self.model_status.setText(text)

    def _check_audio(self):
        self._checked_audio = False
        self._audio_ready = False
        self._system_audio_verified = False
        allowed = True
        if system_audio_permission_required() and not system_audio_permission_granted():
            allowed = request_system_audio_permission()
        self._permission_missing = not allowed
        self.audio_settings.setVisible(self._permission_missing)
        self.skip_button.setText(
            "Start with microphone only" if self._permission_missing else "Start anyway"
        )
        self.audio_status.setText("Opening audio devices…")
        for meter in self.audio_meters:
            meter.setValue(0)
        self.audio_levels.show()
        cancel = self._cancel
        self._audio_progress = SimpleQueue()
        report = self._audio_progress.put
        self._checking_audio = True
        self._run(
            lambda: check_audio(cancel, system_audio_allowed=allowed,
                                progress=lambda *sample: report(sample)),
            self._show_audio, self.audio_status,
        )

    def _show_audio_progress(self) -> None:
        sample = None
        try:
            while True:
                sample = self._audio_progress.get_nowait()
        except Empty:
            pass
        if sample is None:
            return
        remaining, *levels = sample
        self.audio_status.setText(
            f"Listening… {math.ceil(remaining)} s left. Speak and play audio through your call output."
        )
        for meter, rms in zip(self.audio_meters, levels, strict=True):
            dbfs = 20 * math.log10(max(1.0, rms) / 32768.0)
            meter.setValue(round(max(0, min(100, 100 * (1 + dbfs / 65)))))

    def _show_audio(self, audio):
        self._checked_audio = True
        self._audio_ready = audio.ready
        self._system_audio_verified = audio.system_audio_verified
        message = f"Microphone: {audio.microphone}\nSystem audio: {audio.system_audio}"
        if self._permission_missing:
            message += "\nGrant access in System Settings. You may need to relaunch OpenWhisper before retrying."
        elif sys.platform.startswith("linux") and not self._system_audio_verified:
            message += "\nCheck the default output in Sound settings and make sure PulseAudio or PipeWire-Pulse is running."
        if audio.microphone == "No signal detected":
            message += "\nSpeak close to the microphone or turn up its input level, then retry."
        self.audio_status.setText(message)

    def _update_buttons(self):
        idle = self._future is None
        self.prepare_button.setEnabled(idle and not self._models_ready)
        self.audio_button.setEnabled(idle)
        self.skip_button.setEnabled(idle and self._checked_audio
                                    and not (self._models_ready and self._audio_ready))
        ready = idle and self._models_ready and self._audio_ready
        self.finish_button.set_active(ready)
        self.finish_button.setEnabled(ready)
        if not idle:
            hint = "Wait for the audio check." if self._checking_audio else "Wait for model preparation."
        elif not self._models_ready:
            hint = "Prepare the required meeting models to continue."
        elif not self._checked_audio:
            hint = "Run the audio check to continue."
        elif not self._audio_ready:
            hint = "Retry the audio check after resolving the issues above."
        else:
            hint = ""
        self.start_hint.setText(hint)
        self.start_hint.setVisible(bool(hint))

    def _finish(self):
        try:
            settings_manager.save_setting(SettingsKey.MEETING_SETUP_COMPLETE, True)
        except Exception:
            logger.warning("Could not save Meeting Mode setup completion", exc_info=True)
        self.accept()

    def done(self, result):
        self._cancel.set()
        # Keep the modal guard until the worker has released capture devices;
        # otherwise Quick Record or a meeting could race the closing check.
        if self._checking_audio and self._future is not None:
            self._pending_result = result
            self.audio_status.setText("Closing the audio check…")
            self.setEnabled(False)
            return
        self._timer.stop()
        super().done(result)
