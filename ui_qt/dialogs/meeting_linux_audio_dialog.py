"""Linux system-audio readiness dialog for Meeting Mode.

Shown when a Linux x86_64/aarch64 machine cannot capture the default output
monitor. Commands are advice only; the dialog never installs packages or
changes the audio server. Probes run off the Qt UI thread on independent
daemon workers so a wedged SoundCard/libpulse call cannot occupy a shared
executor slot or delay process exit.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Final, Optional

from PyQt6.QtCore import QEventLoop, QObject, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
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
from ui_qt.widgets import Button
from ui_qt.widgets.button_row import ButtonRow
from ui_qt.widgets.buttons import compact_primary_button, neutral_button

logger = logging.getLogger(__name__)

#: Hard deadline for one readiness probe as a user-visible operation.
_PROBE_OPERATION_TIMEOUT_S = 8.0

#: Preferred (not minimum) dialog width in pixels.
_PREFERRED_WIDTH = 560

#: The explanation may scroll, but never shrinks below this on a short screen.
_MIN_CONTENT_HEIGHT = 140


class _ProbeRelay(QObject):
    """Brings probe results to the GUI thread for whichever dialogs exist then.

    It lives for the session, so a probe worker never holds or emits on a
    dialog. Holding one let the worker drop the last reference and delete the
    dialog off the GUI thread, and emitting on its child raced the dialog
    closing; both crashed the process. Qt drops a deleted dialog's connection.
    """

    finished = pyqtSignal(int, object)


_relay: Optional[_ProbeRelay] = None
#: Probe generations, unique across dialogs because they share the relay.
_tokens = itertools.count(1)


def _probe_relay() -> _ProbeRelay:
    global _relay
    if _relay is None:
        _relay = _ProbeRelay()
    return _relay


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
        _probe_relay().finished.connect(self._on_probe_finished)
        # Owned, so a dialog deleted mid-probe takes its deadline with it.
        self._probe_deadline = QTimer(self)
        self._probe_deadline.setSingleShot(True)
        self._probe_deadline.timeout.connect(self._on_probe_timeout)
        self.setWindowTitle("System audio needs a quick setup")
        self.setAccessibleName("Set up Linux system audio")
        self.setAccessibleDescription(
            "Review detected audio issues and setup commands, retry detection, "
            "continue microphone only, or go back."
        )
        self.setModal(True)
        self._fitted = False
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

        # The explanation scrolls on a short screen; the title and both button
        # rows stay put, so the choices are always reachable.
        self._content = QWidget()
        self._content.setObjectName("meetingLinuxAudioContent")
        content = QVBoxLayout(self._content)
        content.setContentsMargins(0, 0, 6, 0)
        content.setSpacing(12)
        self._content_scroll = QScrollArea()
        self._content_scroll.setObjectName("meetingLinuxAudioScroll")
        self._content_scroll.setWidgetResizable(True)
        self._content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._content_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._content_scroll.setMinimumHeight(_MIN_CONTENT_HEIGHT)
        self._content_scroll.setWidget(self._content)
        layout.addWidget(self._content_scroll, 1)

        self.body_label = QLabel("")
        self.body_label.setObjectName("consentBodyLabel")
        self.body_label.setWordWrap(True)
        content.addWidget(self.body_label)

        self.meta_label = QLabel("")
        self.meta_label.setObjectName("meetingLinuxAudioMeta")
        self.meta_label.setWordWrap(True)
        content.addWidget(self.meta_label)

        # Diagnostic-only failures list several status commands; they start
        # folded so the explanation and actions fit a small display.
        self._commands_foldable = False
        self.commands_toggle = neutral_button(Button("Show diagnostic commands"))
        self.commands_toggle.setObjectName("meetingLinuxAudioCommandsToggle")
        self.commands_toggle.setCheckable(True)
        self.commands_toggle.setAutoDefault(False)
        self.commands_toggle.toggled.connect(self._on_commands_toggled)
        self.commands_toggle.hide()
        content.addWidget(self.commands_toggle, 0, Qt.AlignmentFlag.AlignLeft)

        self.commands_label = QLabel("Setup commands")
        self.commands_label.setObjectName("meetingLinuxAudioCommandsLabel")
        content.addWidget(self.commands_label)

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
        content.addWidget(self.commands_edit)

        self.note_label = QLabel("")
        self.note_label.setObjectName("meetingLinuxAudioNote")
        self.note_label.setWordWrap(True)
        content.addWidget(self.note_label)
        content.addStretch(1)

        # Fix-it tools wrap onto more rows when the dialog is narrow instead
        # of squeezing their labels.
        self.copy_btn = neutral_button(Button("Copy command"))
        self.copy_btn.setObjectName("meetingLinuxAudioCopyButton")
        self.copy_btn.setAutoDefault(False)
        self.copy_btn.clicked.connect(self._copy_commands)

        self.guide_btn = neutral_button(Button("Open setup guide"))
        self.guide_btn.setObjectName("meetingLinuxAudioGuideButton")
        self.guide_btn.setAutoDefault(False)
        self.guide_btn.clicked.connect(self._open_guide)

        self.retry_btn = neutral_button(Button("Retry detection"))
        self.retry_btn.setObjectName("meetingLinuxAudioRetryButton")
        self.retry_btn.setAutoDefault(False)
        self.retry_btn.clicked.connect(self._retry_detection)

        self.tool_row = ButtonRow([self.copy_btn, self.guide_btn, self.retry_btn])
        layout.addWidget(self.tool_row)

        # The decision gets its own row, so its labels are never squeezed.
        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        action_row.addStretch()

        self.go_back_btn = neutral_button(Button("Go back"))
        self.go_back_btn.setObjectName("meetingLinuxAudioGoBackButton")
        self.go_back_btn.setAutoDefault(False)
        self.go_back_btn.clicked.connect(self.reject)
        action_row.addWidget(self.go_back_btn)

        # A plain Button with the primary tone: renaming a PrimaryButton for
        # lookup would strip the look it takes from its object name.
        self.mic_only_btn = compact_primary_button(
            Button("Continue microphone only")
        )
        self.mic_only_btn.setObjectName("meetingLinuxAudioMicOnlyButton")
        self.mic_only_btn.setDefault(True)
        self.mic_only_btn.clicked.connect(
            lambda: self._finish(self.RESULT_MICROPHONE_ONLY)
        )
        action_row.addWidget(self.mic_only_btn)

        layout.addLayout(action_row)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        # A comfortable reading width; the layout alone sets the minimum, so
        # the dialog never gets narrower than its button rows need.
        return super().sizeHint().expandedTo(QSize(_PREFERRED_WIDTH, 0))

    def setVisible(self, visible: bool) -> None:  # noqa: N802 - Qt API
        if visible and not self._fitted:
            # Before QDialog centers itself, so it centers the final size.
            self._fitted = True
            self.adjustSize()
            self._fit_height()
        super().setVisible(visible)

    def _fit_height(self) -> None:
        """Show all of the explanation when the screen has room for it.

        ``adjustSize`` caps a window at two thirds of the screen and sizes a
        scroll area by a rough hint, so measure the content at its real width
        and grow (or shrink) to that, never past the screen.
        """
        layout = self.layout()
        layout.activate()
        if not self.isVisible():
            # A window that has not been shown yet has not placed its
            # children; place them so their sizes are this width's.
            layout.setGeometry(self.rect())
            self.tool_row.refresh()
            layout.invalidate()
            layout.setGeometry(self.rect())
        width = self._content_scroll.width()
        if width <= 0:
            return
        content = self._content
        if content.hasHeightForWidth():
            needed = content.heightForWidth(width)
        else:
            needed = content.sizeHint().height()
        target = self.height() - self._content_scroll.height() + needed
        screen = self.screen()
        if screen is not None:
            # Leave room for the window frame and a desktop panel.
            target = min(target, screen.availableGeometry().height() - 64)
        self.resize(self.width(), max(target, self.minimumHeight()))

    def _on_commands_toggled(self, expanded: bool) -> None:
        self._show_commands(expanded)
        if self.isVisible():
            self._fit_height()

    def _show_commands(self, expanded: bool) -> None:
        shown = expanded or not self._commands_foldable
        self.commands_label.setVisible(shown)
        self.commands_edit.setVisible(shown)
        self.copy_btn.setVisible(shown)
        self.commands_toggle.setText(
            "Hide diagnostic commands" if expanded else "Show diagnostic commands"
        )
        self.tool_row.refresh()

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
        # Status checks only, no fix to paste: fold them behind a toggle.
        self._commands_foldable = (
            remediation.reason == "audio_server_unavailable"
            and bool(remediation.commands)
        )
        self.commands_toggle.setVisible(self._commands_foldable)
        self.commands_label.setText(
            "Diagnostic commands" if self._commands_foldable else "Setup commands"
        )
        self._show_commands(self.commands_toggle.isChecked())
        if self.isVisible():
            self._fit_height()

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
        self._probe_generation = generation = next(_tokens)
        # Bound here: the worker must not hold or reach the dialog.
        finished = _probe_relay().finished

        def _done(capability: LinuxAudioCapability) -> None:
            # Queued onto the GUI thread; ignored when generation was invalidated.
            finished.emit(generation, capability)

        _spawn_daemon_probe(self._probe, on_done=_done)
        # Restarted per attempt, so it always times out the one now running.
        self._probe_deadline.start(int(max(0.1, self._probe_timeout_s) * 1000))

    def _on_probe_timeout(self) -> None:
        if not self._probe_pending:
            return
        # Invalidate this generation so a late ready result cannot auto-accept.
        self._probe_generation = next(_tokens)
        self._set_busy(False)
        self._apply_capability(_timeout_capability(self._capability))

    def _on_probe_finished(self, generation: int, capability) -> None:
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
        self._probe_generation = next(_tokens)
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
