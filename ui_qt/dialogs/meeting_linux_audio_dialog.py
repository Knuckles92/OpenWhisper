"""Linux system-audio readiness dialog for Meeting Mode.

Shown when a Linux x86_64/aarch64 machine cannot capture the default output
monitor. Commands are advice only; the dialog never installs packages or
changes the audio server. Probes run off the Qt UI thread on independent
daemon workers so a wedged SoundCard/libpulse call cannot occupy a shared
executor slot or delay process exit.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Final, Optional

from PyQt6.QtCore import QEventLoop, QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QTextEdit,
    QVBoxLayout,
)

from config import bundle_root
from meeting.capture.linux_audio import (
    REASON_MONITOR_OPEN_FAILED,
    REASON_UNKNOWN_FAILURE,
    LinuxAudioCapability,
    probe_linux_audio,
)
from services.linux_deps import (
    LINUX_SYSTEM_AUDIO_GUIDE,
    meeting_audio_remediation,
)
from ui_qt.widgets import Button, PrimaryButton

logger = logging.getLogger(__name__)

#: Hard deadline for one readiness probe as a user-visible operation.
_PROBE_OPERATION_TIMEOUT_S = 8.0


class _ProbeBridge(QObject):
    """Marshal probe results onto the GUI thread via a queued signal."""

    finished = pyqtSignal(object)


def _timeout_capability(
    base: Optional[LinuxAudioCapability] = None,
) -> LinuxAudioCapability:
    return LinuxAudioCapability(
        ready=False,
        reason=REASON_MONITOR_OPEN_FAILED,
        server_kind=base.server_kind if base is not None else "unknown",
        default_sink=base.default_sink if base is not None else "",
        monitor_source=base.monitor_source if base is not None else "",
        package_family=base.package_family if base is not None else "unknown",
        remediation_key=REASON_MONITOR_OPEN_FAILED,
        detail="probe_timeout",
    )


def _spawn_daemon_probe(
    probe: Callable[[], LinuxAudioCapability],
    *,
    on_done: Callable[[LinuxAudioCapability], None],
) -> threading.Thread:
    """Run ``probe`` on a fresh daemon thread; invoke ``on_done`` once."""

    def _worker() -> None:
        capability: LinuxAudioCapability
        try:
            capability = probe()
        except Exception:
            logger.exception("Linux system-audio probe failed")
            capability = LinuxAudioCapability(
                ready=False,
                reason=REASON_UNKNOWN_FAILURE,
                remediation_key=REASON_UNKNOWN_FAILURE,
            )
        try:
            on_done(capability)
        except Exception:
            logger.exception("Linux system-audio probe completion failed")

    thread = threading.Thread(
        target=_worker,
        name="linux-audio-readiness-probe",
        daemon=True,
    )
    thread.start()
    return thread


class MeetingLinuxAudioDialog(QDialog):
    RESULT_CANCEL: Final[str] = "cancel"
    RESULT_MICROPHONE_ONLY: Final[str] = "microphone_only"
    RESULT_READY: Final[str] = "ready"

    def __init__(
        self,
        capability: LinuxAudioCapability,
        parent=None,
        *,
        probe: Optional[Callable[[], LinuxAudioCapability]] = None,
        guide_url: Optional[str] = None,
        probe_timeout_s: float = _PROBE_OPERATION_TIMEOUT_S,
    ):
        super().__init__(parent)
        self.setObjectName("meetingLinuxAudioDialog")
        self.result_action = self.RESULT_CANCEL
        self._probe = probe or (lambda: probe_linux_audio(verify_open=True))
        self._capability = capability
        self._guide_url = guide_url or self._default_guide_url()
        self._probe_timeout_s = float(probe_timeout_s)
        self._probe_pending = False
        # Generation tokens invalidate timed-out or superseded probe attempts.
        self._probe_generation = 0
        self._probe_bridge = _ProbeBridge(self)
        self._probe_bridge.finished.connect(self._on_probe_finished)
        self.setWindowTitle("System audio needs a quick setup")
        self.setMinimumWidth(560)
        self.setModal(True)
        self._setup_ui()
        self._apply_capability(capability)

    @staticmethod
    def _default_guide_url() -> str:
        guide_path = Path(bundle_root()) / LINUX_SYSTEM_AUDIO_GUIDE
        return guide_path.as_uri()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        self.title_label = QLabel("System audio needs a quick setup")
        self.title_label.setObjectName("headerLabel")
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.body_label = QLabel("")
        self.body_label.setObjectName("consentBodyLabel")
        self.body_label.setWordWrap(True)
        layout.addWidget(self.body_label)

        self.meta_label = QLabel("")
        self.meta_label.setObjectName("meetingLinuxAudioMeta")
        self.meta_label.setWordWrap(True)
        layout.addWidget(self.meta_label)

        self.commands_label = QLabel("Setup commands")
        self.commands_label.setObjectName("meetingLinuxAudioCommandsLabel")
        layout.addWidget(self.commands_label)

        self.commands_edit = QTextEdit()
        self.commands_edit.setObjectName("meetingLinuxAudioCommands")
        self.commands_edit.setReadOnly(True)
        self.commands_edit.setMinimumHeight(110)
        self.commands_edit.setAccessibleName("Setup commands")
        self.commands_edit.setAccessibleDescription(
            "Copyable package-manager or diagnostic commands for enabling "
            "Linux system-audio capture. Review before running in a terminal."
        )
        self.commands_label.setBuddy(self.commands_edit)
        layout.addWidget(self.commands_edit)

        self.note_label = QLabel("")
        self.note_label.setObjectName("meetingLinuxAudioNote")
        self.note_label.setWordWrap(True)
        layout.addWidget(self.note_label)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)

        self.copy_btn = Button("Copy command")
        self.copy_btn.setObjectName("meetingLinuxAudioCopyButton")
        self.copy_btn.clicked.connect(self._copy_commands)
        button_row.addWidget(self.copy_btn)

        self.guide_btn = Button("Open setup guide")
        self.guide_btn.setObjectName("meetingLinuxAudioGuideButton")
        self.guide_btn.clicked.connect(self._open_guide)
        button_row.addWidget(self.guide_btn)

        self.retry_btn = Button("Retry detection")
        self.retry_btn.setObjectName("meetingLinuxAudioRetryButton")
        self.retry_btn.clicked.connect(self._retry_detection)
        button_row.addWidget(self.retry_btn)

        button_row.addStretch()

        self.mic_only_btn = Button("Continue microphone only")
        self.mic_only_btn.setObjectName("meetingLinuxAudioMicOnlyButton")
        self.mic_only_btn.clicked.connect(
            lambda: self._finish(self.RESULT_MICROPHONE_ONLY)
        )
        button_row.addWidget(self.mic_only_btn)

        self.go_back_btn = PrimaryButton("Go back")
        self.go_back_btn.setObjectName("meetingLinuxAudioGoBackButton")
        self.go_back_btn.setDefault(True)
        self.go_back_btn.clicked.connect(self.reject)
        button_row.addWidget(self.go_back_btn)

        layout.addLayout(button_row)

    def _apply_capability(self, capability: LinuxAudioCapability) -> None:
        self._capability = capability
        remediation = meeting_audio_remediation(
            capability.reason,
            capability.package_family,
            server_kind=capability.server_kind,
        )
        self.title_label.setText(remediation.title)
        self.body_label.setText(
            f"{remediation.explanation}\n\n"
            "Without system audio, the other side of a call will not appear "
            "in the transcript. You can fix the audio session and retry, or "
            "continue with microphone only for this meeting."
        )
        family = capability.package_family or "unknown"
        server = capability.server_kind or "unknown"
        self.meta_label.setText(
            f"Detected package family: {family}\n"
            f"Audio stack: {server}\n"
            f"Diagnostic key: {capability.reason}"
        )
        commands = "\n".join(remediation.commands) if remediation.commands else (
            "No automatic package command is available for this environment. "
            "See the setup guide for manual steps."
        )
        self.commands_edit.setPlainText(commands)
        notes = []
        if remediation.restart_note:
            notes.append(remediation.restart_note)
        if remediation.rollback_note:
            notes.append(remediation.rollback_note)
        self.note_label.setText("\n".join(notes))
        self.note_label.setVisible(bool(notes))
        self.copy_btn.setEnabled(bool(remediation.commands))

    def _copy_commands(self) -> None:
        text = self.commands_edit.toPlainText().strip()
        if not text:
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)

    def _open_guide(self) -> None:
        QDesktopServices.openUrl(QUrl(self._guide_url))

    def _set_busy(self, busy: bool) -> None:
        self._probe_pending = busy
        # Keep cancellation and microphone-only available while a probe runs so
        # a wedged backend cannot trap the start flow.
        self.retry_btn.setEnabled(not busy)
        self.retry_btn.setText("Checking…" if busy else "Retry detection")
        self.mic_only_btn.setEnabled(True)
        self.go_back_btn.setEnabled(True)
        self.copy_btn.setEnabled(
            (not busy) and bool(self.commands_edit.toPlainText().strip())
        )
        self.guide_btn.setEnabled(True)

    def _retry_detection(self) -> None:
        if self._probe_pending:
            return
        self._set_busy(True)
        generation = self._probe_generation + 1
        self._probe_generation = generation

        def _done(capability: LinuxAudioCapability) -> None:
            # Queued onto the GUI thread; ignored when generation was invalidated.
            self._probe_bridge.finished.emit((generation, capability))

        _spawn_daemon_probe(self._probe, on_done=_done)
        QTimer.singleShot(
            int(max(0.1, self._probe_timeout_s) * 1000),
            lambda: self._on_probe_timeout(generation),
        )

    def _on_probe_timeout(self, generation: int) -> None:
        if generation != self._probe_generation or not self._probe_pending:
            return
        # Invalidate this generation so a late ready result cannot auto-accept.
        self._probe_generation = generation + 1
        self._set_busy(False)
        self._apply_capability(_timeout_capability(self._capability))

    def _on_probe_finished(self, payload) -> None:
        try:
            generation, capability = payload
        except Exception:
            return
        if generation != self._probe_generation:
            # Timed out, superseded, or closed — ignore stale completions.
            return
        if not self._probe_pending:
            return
        self._set_busy(False)
        if capability.ready:
            self._finish(self.RESULT_READY)
            return
        self._apply_capability(capability)

    def _finish(self, action: str) -> None:
        self.result_action = action
        self.accept()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        # Invalidate in-flight generations; daemon workers are abandoned.
        self._probe_generation += 1
        self._probe_pending = False
        super().closeEvent(event)


def _run_bounded_probe(
    probe: Callable[[], LinuxAudioCapability],
    *,
    timeout_s: float = _PROBE_OPERATION_TIMEOUT_S,
) -> LinuxAudioCapability:
    """Run ``probe`` on a daemon thread with a hard deadline; never raises.

    Does not block the Qt event loop when one is available: pumps events (or
    runs a local QEventLoop) until the probe finishes or times out.
    """
    result_box: list[LinuxAudioCapability] = []
    done = threading.Event()

    def _done(capability: LinuxAudioCapability) -> None:
        result_box.append(capability)
        done.set()

    _spawn_daemon_probe(probe, on_done=_done)

    app = QApplication.instance()
    deadline = time.monotonic() + max(0.1, float(timeout_s))
    if app is not None:
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(50)

        def _tick() -> None:
            if done.is_set() or time.monotonic() >= deadline:
                timer.stop()
                loop.quit()

        timer.timeout.connect(_tick)
        timer.start()
        # Also wake immediately if the worker finishes first.
        poll = QTimer()
        poll.setInterval(20)
        poll.timeout.connect(_tick)
        poll.start()
        loop.exec()
        timer.stop()
        poll.stop()
    else:
        remaining = max(0.0, deadline - time.monotonic())
        done.wait(timeout=remaining)

    if result_box:
        return result_box[0]
    logger.warning(
        "Linux system-audio probe exceeded %.1fs deadline", timeout_s
    )
    return _timeout_capability()


def ensure_meeting_linux_system_audio(
    parent=None,
    *,
    probe: Optional[Callable[[], LinuxAudioCapability]] = None,
    probe_timeout_s: float = _PROBE_OPERATION_TIMEOUT_S,
) -> str:
    """Return the Linux readiness decision for a meeting start.

    The initial probe runs off the Qt thread with a hard deadline. Returns:

        ``ready`` when dual-channel capture may proceed,
        ``microphone_only`` when the user accepted mic-only for this meeting,
        or ``cancel`` when the start should abort.
    """
    import sys

    if not sys.platform.startswith("linux"):
        return MeetingLinuxAudioDialog.RESULT_READY

    probe_fn = probe or (lambda: probe_linux_audio(verify_open=True))
    capability = _run_bounded_probe(probe_fn, timeout_s=probe_timeout_s)

    if capability.ready:
        return MeetingLinuxAudioDialog.RESULT_READY

    dialog = MeetingLinuxAudioDialog(
        capability,
        parent=parent,
        probe=probe_fn,
        probe_timeout_s=probe_timeout_s,
    )
    dialog.exec()
    action = getattr(dialog, "result_action", MeetingLinuxAudioDialog.RESULT_CANCEL)
    if action == MeetingLinuxAudioDialog.RESULT_READY:
        return MeetingLinuxAudioDialog.RESULT_READY
    if action == MeetingLinuxAudioDialog.RESULT_MICROPHONE_ONLY:
        return MeetingLinuxAudioDialog.RESULT_MICROPHONE_ONLY
    return MeetingLinuxAudioDialog.RESULT_CANCEL
