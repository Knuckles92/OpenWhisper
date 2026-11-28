"""Optional first-run setup: a welcome, Record → Clean up → Output, then a try-it page.

The Settings window remains the owner of save handlers, confirmations and
runtime callbacks. These controls project its saved values; opening,
stepping and illustrating cleanup never applies a recommendation. Settings
is only built once the user leaves the welcome page, so offering setup at
startup costs nothing.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable

from PyQt6.QtCore import QByteArray, QEvent, QPoint, QSignalBlocker, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from services import cleanup_prompts, dictation_language, guided_setup
from services.credentials import resolve_credential
from services.hotkey_manager import format_hotkey_display
from services.remote_asr import invite as remote_invite
from services.remote_asr import protocol as remote_protocol
from services.remote_asr.qr import render_invite_qr_svg_bytes
from services.settings import (
    RecordingTriggerMode,
    SettingsKey,
    resolve_bool_setting,
    resolve_recording_hands_free_latch,
    resolve_recording_trigger_mode,
    resolve_transcript_cleanup_level,
    resolve_transcript_cleanup_provider,
    settings_manager,
)
from services.text_llm import get_profile
from ui_qt.dialogs import cleanup_levels, settings_microphones
from ui_qt.dialogs.settings_destinations import (
    API_KEYS,
    CLEANUP,
    GENERAL,
    HOTKEYS,
    RECORDING,
    REMOTE_ENGINE,
    VOICE_MODEL,
)
from ui_qt.utils.app_icon import app_icon
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.icons import design_icon
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets import Button
from ui_qt.widgets.buttons import compact_primary_button, neutral_button
from ui_qt.widgets.hotkey_capture import mouse_shortcut_note
from ui_qt.widgets.microphone_check import MicrophoneCheck
from ui_qt.widgets.no_wheel import ElidingComboBox
from ui_qt.widgets.profile_hotkey_input import ProfileHotkeyInput
from ui_qt.widgets.segmented_bar import SegmentedBar
from ui_qt.widgets.settings_switch import SettingsSwitch
from ui_qt.widgets.wrapped_label import WrappedLabel
from ui_qt.utils.error_messages import format_action_error

logger = logging.getLogger(__name__)

WELCOME, RECORD, CLEAN_UP, OUTPUT, READY = range(5)
CONNECT_HOST, HOST_AI, HOST_SETUP = 5, 6, 7

ROLE_LOCAL = "local"
ROLE_CLIENT = "client"
ROLE_HOST = "host"

STEPS = ("Record", "Clean up", "Output")
CLIENT_STEPS = (
    (CONNECT_HOST, "Connect"),
    (HOST_AI, "Host AI"),
    (RECORD, "Record"),
    (OUTPUT, "Output"),
)
HOST_STEPS = (
    (HOST_SETUP, "Host setup"),
    (RECORD, "Record"),
    (OUTPUT, "Output"),
)
LOCAL_STEPS = (
    (RECORD, "Record"),
    (CLEAN_UP, "Clean up"),
    (OUTPUT, "Output"),
)

_STYLE = """
QDialog#guidedSetupDialog { background: @slate-bg; }
QWidget#guidedPage, QWidget#guidedCanvas, QWidget#guidedGroup { background: transparent; }
QFrame#guidedHeader { background: @slate-field; border: none; border-bottom: 1px solid @slate-border; }
QFrame#guidedFooter { background: transparent; border: none; border-top: 1px solid @slate-border-subtle; }
QFrame#guidedCard, QFrame#guidedPreview {
    background: @slate-field; border: 1px solid @slate-border; border-radius: 12px;
}
QFrame#guidedRoleCard {
    background: @slate-field; border: 1px solid @slate-border; border-radius: 12px;
}
QFrame#guidedRoleCard:hover {
    border-color: @accent-hover;
}
QFrame#guidedRoleCard[selected="true"] {
    background: @slate-raised; border: 2px solid @accent;
}
QLabel#guidedRoleBadge {
    color: @accent; font-size: 11px; font-weight: 600; border: none; background: transparent;
}
QFrame#guidedQrFrame {
    background: #ffffff; border: 1px solid @slate-border; border-radius: 10px;
}
QLineEdit#guidedInput {
    color: @slate-text; background: @slate-raised; border: 1px solid @slate-border;
    border-radius: 8px; padding: 6px 10px; font-size: 13px;
}
QLineEdit#guidedInput:focus { border-color: @accent; }
QLabel#guidedCapBadge {
    color: @slate-text-3; background: @slate-raised; border: 1px solid @slate-border;
    border-radius: 9px; padding: 2px 8px; font-size: 11px; font-weight: 600;
}
QLabel#guidedCapBadge[enabled="true"] {
    color: @success-text; border-color: @success-text;
}
QLabel#guidedBrand { color: @slate-text; font-size: 15px; font-weight: 600; border: none; background: transparent; }
QLabel#guidedHeroTitle { color: @slate-text; font-size: 28px; font-weight: 600; border: none; background: transparent; }
QLabel#guidedTitle { color: @slate-text; font-size: 22px; font-weight: 600; border: none; background: transparent; }
QLabel#guidedLede { color: @slate-text-2; font-size: 14px; border: none; background: transparent; }
QLabel#guidedRowTitle { color: @slate-text; font-size: 14px; font-weight: 600; border: none; background: transparent; }
QLabel#guidedDetail { color: @slate-text-3; font-size: 13px; border: none; background: transparent; }
QLabel#guidedDetail[tone="ready"] { color: @success-text; }
QLabel#guidedDetail[tone="attention"] { color: @warning-text; }
QLabel#guidedSample { color: @slate-text; font-size: 17px; border: none; background: transparent; }
QLabel#guidedPrivacy {
    color: @slate-text-2; background: @slate-raised; border: 1px solid @slate-border;
    border-radius: 8px; padding: 10px 12px; font-size: 13px;
}
QLabel#guidedPrivacy[remote="true"] {
    color: @warning-text; background: @warning-tint; border-color: @warning-tint-border;
}
QLabel#guidedError { color: @danger-text; font-size: 13px; background: transparent; border: none; }
QPushButton#guidedStep {
    color: @slate-text-3; padding: 8px 6px; border: none; border-bottom: 2px solid @slate-border;
    border-radius: 0; font-size: 13px; background: transparent;
}
QPushButton#guidedStep:hover { color: @slate-text; }
QPushButton#guidedStep:focus { color: @slate-text; border-bottom-color: @accent-hover; }
QPushButton#guidedStep[active="true"] { color: @accent; border-color: @accent; font-weight: 600; }
QPushButton#guidedStep[done="true"] { color: @slate-text-2; border-color: @accent; }
QPushButton#guidedStep[done="true"]:hover { color: @slate-text; }
QProgressBar#microphoneCheckBar, QProgressBar#guidedProgress {
    background: @slate-raised; border: none; border-radius: 4px;
}
QProgressBar#microphoneCheckBar::chunk, QProgressBar#guidedProgress::chunk {
    background: @accent; border-radius: 4px;
}
QLabel#guidedKeycap {
    color: @slate-text; background: @slate-raised; border: 1px solid @slate-border-strong;
    border-bottom-width: 3px; border-radius: 8px; padding: 6px 16px; font-size: 22px; font-weight: 600;
}
QPlainTextEdit#guidedTry {
    color: @slate-text; background: @slate-field; border: 1px solid @slate-border;
    border-radius: 10px; padding: 10px 12px; font-size: 15px;
}
QPlainTextEdit#guidedTry:focus { border-color: @accent; }
QPushButton#guidedLink { color: @accent; background: transparent; border: none; padding: 6px 8px; font-size: 13px; }
QPushButton#guidedLink:hover { color: @accent-hover; text-decoration: underline; }
QPushButton#guidedLink:focus { border: 1px solid @accent; border-radius: 5px; }
QScrollArea#guidedScroll { background: transparent; border: none; }
"""


def _svg_pixmap(svg_bytes: bytes, target_px: int = 168) -> QPixmap:
    """Decode SVG bytes into a crisp ``QPixmap`` using Qt's bundled SVG plugin."""
    image = QImage()
    if image.loadFromData(QByteArray(svg_bytes), "SVG"):
        pix = QPixmap.fromImage(image)
        if not pix.isNull():
            return pix.scaled(
                target_px,
                target_px,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
    return QPixmap()


class _RoleCard(QFrame):
    """Clickable setup path card on the Welcome page."""

    clicked = pyqtSignal(str)

    def __init__(self, role_id: str, icon_name: str, title: str, detail: str, badge: str = "", parent=None):
        super().__init__(parent)
        self.role_id = role_id
        self.setObjectName("guidedRoleCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 12, 16, 12)
        row.setSpacing(14)
        glyph = QLabel()
        glyph.setPixmap(design_icon(icon_name).pixmap(24, 24))
        glyph.setAlignment(Qt.AlignmentFlag.AlignTop)
        glyph.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(glyph)
        copy = QVBoxLayout()
        copy.setSpacing(2)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        title_lbl = _label(title, "guidedRowTitle")
        title_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        header.addWidget(title_lbl, 1)
        if badge:
            badge_lbl = QLabel(badge)
            badge_lbl.setObjectName("guidedRoleBadge")
            badge_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            header.addWidget(badge_lbl, 0, Qt.AlignmentFlag.AlignRight)
        copy.addLayout(header)
        detail_lbl = _label(detail)
        detail_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        copy.addWidget(detail_lbl)
        row.addLayout(copy, 1)

    def set_selected(self, selected: bool) -> None:
        set_style_property(self, "selected", selected)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.role_id)
        super().mousePressEvent(event)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clicked.emit(self.role_id)
            return
        super().keyPressEvent(event)


def provider_summary(settings: dict) -> tuple[str, str, str, bool, bool]:
    """``(name, status, privacy, remote, needs_key)`` from saved configuration.

    A saved key is reported as configured, never as a tested connection.
    """
    provider = resolve_transcript_cleanup_provider(settings)
    if provider == remote_invite.REMOTE_HOST_PROVIDER:
        from services.remote_asr.settings import load_client_pairing

        pairing = load_client_pairing(settings)
        host_name = pairing.host_name if pairing is not None else "your paired host"
        return (
            f"Paired host ({host_name})",
            "Uses your host's AI provider · No API key needed here",
            f"Transcript text is sent to {host_name} over your encrypted host link when cleanup is on. No audio is sent for cleanup.",
            True,
            False,
        )
    from services.settings import TranscriptCleanupProvider
    if provider in TranscriptCleanupProvider.INSTALLED:
        from services.installed_agents import AGENT_SPECS, cached_scan

        name = AGENT_SPECS[provider].name
        scan = cached_scan()
        agent = (scan or {}).get(provider)
        status = "Uses your agent's sign-in."
        if scan is not None and (agent is None or not agent.usable):
            status = "Install or sign in to this agent in All settings."
        return (
            name,
            status,
            f"Transcript text is sent to {name}'s model when cleanup is on. No audio is sent for cleanup.",
            True, False,
        )
    profile = get_profile(provider, settings)
    if profile is None:
        return (
            "Provider unavailable",
            "Choose a provider in All settings.",
            "The saved cleanup provider is unavailable. Choose a provider before using AI cleanup.",
            True,
            False,
        )
    local = profile.is_local
    privacy = (
        f"Transcript text is sent to {profile.name} on this computer when cleanup is on."
        if local
        else f"Transcript text is sent to {profile.name} when cleanup is on."
    ) + " No audio is sent for cleanup."
    needs_key = False
    if profile.api_key_env:
        if resolve_credential(profile.api_key_env):
            status = "API key saved · Connection not tested"
        else:
            status, needs_key = "API key needed", True
    else:
        status = "No API key needed · Connection not tested"
    return profile.name, status, privacy, not local, needs_key


def _on_number_pad(hotkey: str) -> bool:
    return hotkey.startswith("kp ") or "+kp " in hotkey


def shortcut_help(hotkey: str, settings: dict) -> str:
    """How the recording shortcut is used, in the user's trigger mode."""
    if resolve_recording_trigger_mode(settings) == "push_hold":
        text = "Hold it while you speak, then let go."
        if resolve_recording_hands_free_latch(settings):
            text += " Double-tap to keep recording hands-free."
    else:
        text = "Press it to start dictating, and again to stop."
    if _on_number_pad(hotkey):
        text += " It's on the number pad."
    note = mouse_shortcut_note(hotkey)
    return " ".join(filter(None, (text, note)))


def _label(text, name="guidedDetail"):
    label = WrappedLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setObjectName(name)
    return label


def _button(text, callback, name="guidedButton"):
    """A link, or a footer-style button in the shared Settings and dialog look."""
    if name == "guidedLink":
        button = QPushButton(text)
    else:
        styled = compact_primary_button if name == "guidedPrimary" else neutral_button
        button = styled(Button(text))
    button.setObjectName(name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    # Enter presses the focused button, and Continue from anywhere else.
    button.setAutoDefault(True)
    button.clicked.connect(callback)
    return button


def _group(layout_type=QVBoxLayout, spacing=12):
    widget = QWidget()
    widget.setObjectName("guidedGroup")
    layout = layout_type(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    return widget, layout


class CleanupChoices(SegmentedBar):
    """Three preset tiles, with no selected preset while a custom prompt wins."""

    def setCurrentIndex(self, index):
        if not getattr(self, "_custom", False):
            super().setCurrentIndex(index)

    def set_choice(self, level, custom):
        self._custom = custom
        if custom:
            self._group.setExclusive(False)
            for button in self.buttons:
                button.setChecked(False)
            self._group.setExclusive(True)
            self._index = -1
        else:
            self.setCurrentIndex(cleanup_levels.segment_index(level))


class GuidedSetupDialog(QDialog):
    """Focused projections of existing Settings controls; choices save immediately.

    Args:
        get_settings: Returns the configured Settings window. Called once,
            when setup first needs it (never for the welcome page alone).
        open_destination: Opens Settings on a destination key.
        open_all_settings: Opens Settings on its home page.
        download_model: Starts the app's own download of a model by name,
            consent included. None sends the user to the model page instead.
        remote_service: Optional ``RemoteEngineService`` for LAN/Tailscale
            discovery, invite pairing, and host invite setup.
    """

    _scan_done = pyqtSignal(object)
    _pair_done = pyqtSignal(object, object)
    _caps_done = pyqtSignal(object)

    def __init__(
        self,
        get_settings: Callable,
        parent=None,
        *,
        open_destination: Callable,
        open_all_settings: Callable,
        download_model: Callable | None = None,
        manager=None,
        remote_service=None,
    ):
        super().__init__(parent)
        self._get_settings = get_settings
        self._settings = None
        self._remote_service = remote_service
        self._host_invite_server: remote_invite.HostInviteServer | None = None
        self.manager = settings_manager if manager is None else manager
        self.open_destination = open_destination
        self.open_all_settings = open_all_settings
        self._download_model = download_model
        # The speech download this window started: the model name while it
        # downloads, whether its runtime is installing, and (text, done, total).
        self.expecting_download = False
        self._requested_model = None
        self._model_busy = None
        self._runtime_busy = False
        self._download_progress = None
        self._download_error = ""
        self.role = ROLE_CLIENT if remote_invite.is_client_only_edition() else ROLE_LOCAL
        self._pending_invite: remote_invite.InviteLink | None = None
        self._scan_busy = False
        self._scan_result = None
        self._pairing_busy = False
        self._host_capabilities: dict = {}
        self._host_ai_meta: dict = {}
        self._host_invite_url = ""
        self.page = WELCOME
        self._finished = False
        self._speech_ready = None
        self._provider_needs_key = False
        # (tone, text) once a dictation has been tried on the last page.
        self._try_result = None
        self._tried_text = ""
        self._rows = []
        self.role_cards: dict[str, _RoleCard] = {}
        self.host_cap_switches: dict[str, SettingsSwitch] = {}
        self.host_ai_badges: dict[str, QLabel] = {}
        self.setObjectName("guidedSetupDialog")
        self.setWindowTitle(
            "Set up OpenWhisper Client"
            if remote_invite.is_client_only_edition()
            else "Set up OpenWhisper"
        )
        self.setWindowIcon(app_icon())
        self.setModal(False)
        # Dictation and the in-app shortcut fallback treat it as OpenWhisper's
        # own window, so the try-it box on the last page takes dictation.
        self.setProperty("openwhisperHotkeyWindow", True)
        self.setMinimumSize(540, 460)
        self.resize(720, 690)
        self.setSizeGripEnabled(True)
        self.setStyleSheet(_STYLE)
        self._scan_done.connect(self._on_scan_done)
        self._pair_done.connect(self._on_pair_done)
        self._caps_done.connect(self._on_caps_done)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh)
        self._privacy_reveal_timer = QTimer(self)
        self._privacy_reveal_timer.setSingleShot(True)
        self._privacy_reveal_timer.timeout.connect(self._reveal_cleanup_privacy)
        self._try_fill_timer = QTimer(self)
        self._try_fill_timer.setSingleShot(True)
        self._try_fill_timer.timeout.connect(self._fill_try_box)
        self._setup_ui()
        self.show_page(CONNECT_HOST if remote_invite.is_client_only_edition() else WELCOME)

    def _resolve_remote_service(self):
        if self._remote_service is not None:
            return self._remote_service
        if self._settings is not None:
            svc = getattr(self._settings, "_remote_service", None) or getattr(
                getattr(self._settings, "remote_section", None), "_service", None
            )
            if svc is not None:
                return svc
        return None

    # ---- settings binding ----

    @property
    def settings(self):
        return self._ensure_settings()

    def _ensure_settings(self):
        if self._settings is None:
            self._settings = self._get_settings()
            self._settings.ensure_page(RECORDING)
            # The existing microphone worker fills this model asynchronously.
            model = self._settings.audio_device_combo.model()
            for signal in (
                model.modelReset,
                model.rowsInserted,
                model.rowsRemoved,
                model.dataChanged,
            ):
                signal.connect(self._queue_refresh)
        return self._settings

    # ---- construction ----

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("guidedHeader")
        row = QHBoxLayout(header)
        row.setContentsMargins(20, 12, 16, 12)
        row.setSpacing(10)
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(28, 28))
        row.addWidget(icon)
        row.addWidget(_label("Set up OpenWhisper", "guidedBrand"))
        row.addStretch()
        self.all_settings_button = _button("All settings", self._all_settings, "guidedLink")
        row.addWidget(self.all_settings_button)
        root.addWidget(header)

        body = QWidget()
        body.setObjectName("guidedCanvas")
        column = QVBoxLayout(body)
        column.setContentsMargins(28, 16, 28, 0)
        column.setSpacing(12)
        self.step_row, self._steps_layout = _group(QHBoxLayout, 8)
        self.step_buttons = []
        self._rebuild_step_buttons(LOCAL_STEPS)
        column.addWidget(self.step_row)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("guidedScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pages = QStackedWidget()
        self.pages.setObjectName("guidedCanvas")
        self.pages.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._welcome_page()
        self._record_page()
        self._clean_up_page()
        self._output_page()
        self._ready_page()
        self._connect_host_page()
        self._host_ai_page()
        self._host_setup_page()
        self.scroll.setWidget(self.pages)
        column.addWidget(self.scroll, 1)
        self.error = _label("", "guidedError")
        self.error.hide()
        column.addWidget(self.error)
        root.addWidget(body, 1)

        footer = QFrame()
        footer.setObjectName("guidedFooter")
        actions = QHBoxLayout(footer)
        actions.setContentsMargins(20, 12, 20, 14)
        actions.setSpacing(10)
        self.skip_button = _button("Skip setup", self.reject, "guidedLink")
        actions.addWidget(self.skip_button)
        actions.addStretch()
        self.back_button = _button("Back", self._back)
        actions.addWidget(self.back_button)
        self.next_button = _button("Continue", self._continue, "guidedPrimary")
        self.next_button.setDefault(True)
        actions.addWidget(self.next_button)
        root.addWidget(footer)

    def _page(self, title, lede, name="guidedTitle"):
        page = QWidget()
        page.setObjectName("guidedPage")
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 8, 0, 16)
        column.setSpacing(14)
        heading, text = _group(spacing=6)
        heading_label = _label(title, name)
        text.addWidget(heading_label)
        lede_label = _label(lede, "guidedLede")
        text.addWidget(lede_label)
        column.addWidget(heading)
        self.pages.addWidget(page)
        return column, heading_label, lede_label

    def _card(self, column):
        card = QFrame()
        card.setObjectName("guidedCard")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 16, 20, 16)
        inner.setSpacing(16)
        column.addWidget(card)
        return inner

    def _row(self, column, title, detail, control):
        row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        row.setSpacing(18)
        text = QVBoxLayout()
        text.setSpacing(3)
        text.addWidget(_label(title, "guidedRowTitle"))
        description = _label(detail)
        text.addWidget(description)
        row.addLayout(text, 1)
        row.addWidget(control)
        control.setAccessibleName(title)
        column.addLayout(row)
        self._rows.append((row, control))
        return description

    def _progress_bar(self):
        bar = QProgressBar()
        bar.setObjectName("guidedProgress")
        bar.setTextVisible(False)
        bar.setFixedHeight(8)
        bar.setAccessibleName("Speech model download")
        bar.hide()
        return bar

    def _link_row(self, column, text, callback):
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        link = _button(text, callback, "guidedLink")
        row.addWidget(link)
        row.addStretch()
        column.addLayout(row)
        return link

    def _rebuild_step_buttons(self, spec: tuple[tuple[int, str], ...]) -> None:
        while self._steps_layout.count():
            item = self._steps_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.step_buttons = []
        self._step_spec = spec
        for index, (page_id, title) in enumerate(spec):
            step = QPushButton(title)
            step.setObjectName("guidedStep")
            step.setCursor(Qt.CursorShape.PointingHandCursor)
            step.setAccessibleName(f"Step {index + 1} of {len(spec)}: {title}")
            step.clicked.connect(lambda _=False, p=page_id: self.show_page(p))
            self._steps_layout.addWidget(step, 1)
            self.step_buttons.append(step)

    def _active_steps(self) -> tuple[tuple[int, str], ...]:
        if remote_invite.is_client_only_edition() or self.role == ROLE_CLIENT:
            return CLIENT_STEPS
        if self.role == ROLE_HOST:
            return HOST_STEPS
        return LOCAL_STEPS

    def select_role(self, role: str) -> None:
        """Select a setup path on Welcome (``'local'``, ``'client'``, or ``'host'``)."""
        if role not in (ROLE_LOCAL, ROLE_CLIENT, ROLE_HOST):
            return
        self.role = role
        for rid, card in self.role_cards.items():
            card.set_selected(rid == role)

    def _welcome_page(self):
        column, _, _ = self._page(
            "Welcome to OpenWhisper",
            "Speak, and your words appear wherever you're typing. "
            "Choose how you want to use OpenWhisper on this computer.",
            "guidedHeroTitle",
        )
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(56, 56))
        column.insertWidget(0, icon)

        for role_id, svg, title, detail, badge in (
            (
                ROLE_LOCAL,
                "microphone-blue.svg",
                "Use on this computer",
                "Run speech models locally on this PC. Record, optionally clean up with AI, and paste anywhere.",
                "Default",
            ),
            (
                ROLE_CLIENT,
                "world-blue.svg",
                "Connect to an OpenWhisper Host",
                "Use another computer's GPU, speech engine, and AI services over LAN or Tailscale — skip local model downloads.",
                "Lightweight",
            ),
            (
                ROLE_HOST,
                "server-blue.svg",
                "Set up as a Host",
                "Share this computer's speech engine and opt-in AI services with your other devices and generate a client invite link.",
                "Share",
            ),
        ):
            card = _RoleCard(role_id, svg, title, detail, badge)
            card.clicked.connect(self.select_role)
            self.role_cards[role_id] = card
            column.addWidget(card)
        self.select_role(self.role)

        column.addWidget(
            _label(
                "Takes about a minute. Every choice saves as you go, and you can "
                "come back any time from File → Guided setup."
            )
        )
        column.addStretch()

    def _record_page(self):
        column, _, _ = self._page(
            "How will you record?",
            "Pick a microphone and the shortcut that starts dictation.",
        )
        card = self._card(column)
        self.microphone = ElidingComboBox()
        self.microphone.setObjectName("basicSettingsCombo")
        self.microphone.setMinimumHeight(38)
        self.microphone.setMinimumWidth(200)
        self.microphone.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        holder, microphone_row = _group(QHBoxLayout, 8)
        microphone_row.addWidget(self.microphone, 1)
        self.mic_test_button = _button("Test", self._toggle_mic_check)
        self.mic_test_button.setAccessibleName("Test microphone")
        microphone_row.addWidget(self.mic_test_button)
        self.microphone_detail = self._row(card, "Microphone", "", holder)
        self.microphone.setAccessibleName("Microphone")
        self.microphone.currentIndexChanged.connect(self._choose_microphone)
        self.mic_check = MicrophoneCheck(lambda: self.settings._recording_look_recorder())
        self.mic_check.running_changed.connect(self._mic_check_changed)
        self.mic_check.hide()
        card.addWidget(self.mic_check)
        holder, shortcut_row = _group(QHBoxLayout, 8)
        self.shortcut = ProfileHotkeyInput()
        self.shortcut.setAccessibleName("Recording shortcut")
        self.shortcut.setToolTip("Click Change, then press a shortcut. Escape cancels.")
        self.shortcut.capture_changed.connect(lambda capturing: self.settings._on_profile_capture(capturing))
        self.shortcut.captured.connect(self._shortcut_captured)
        shortcut_row.addWidget(self.shortcut)
        shortcut_row.addWidget(_button("Change", self._capture_shortcut))
        self.shortcut_detail = self._row(card, "Shortcut", "", holder)
        self.trigger = ElidingComboBox()
        self.trigger.setObjectName("basicSettingsCombo")
        self.trigger.setMinimumHeight(38)
        self.trigger.setMinimumWidth(200)
        self.trigger.addItem("Press to start and stop", RecordingTriggerMode.TOGGLE)
        self.trigger.addItem("Hold to talk", RecordingTriggerMode.PUSH_HOLD)
        self._row(
            card,
            "Press or hold",
            "Press once and talk for as long as you like, or hold the shortcut like a walkie-talkie.",
            self.trigger,
        )
        self.trigger.currentIndexChanged.connect(self._choose_trigger)
        holder, speech_row = _group(QHBoxLayout, 8)
        self.speech_download_button = _button("Download", self._download_speech)
        self.speech_download_button.setAccessibleName("Download the speech model")
        speech_row.addWidget(self.speech_download_button)
        self.speech_button = _button("Change", lambda: self._open(VOICE_MODEL), "guidedLink")
        speech_row.addWidget(self.speech_button)
        self.speech_summary = self._row(card, "Speech engine", "", holder)
        self.speech_progress = self._progress_bar()
        card.addWidget(self.speech_progress)
        self.speech_multilingual_menu = QMenu(self)
        self.speech_multilingual_row, multilingual_layout = _group(QHBoxLayout, 8)
        self.speech_multilingual_row.setObjectName("guidedMultilingualRow")
        self.speech_multilingual_note = _label(
            "English only \u2014 dictating in another language?"
        )
        self.speech_multilingual_button = _button(
            "Other languages", self._open_multilingual_menu, "guidedLink"
        )
        self.speech_multilingual_button.setAccessibleName(
            "Switch to a multilingual speech engine"
        )
        multilingual_layout.addWidget(self.speech_multilingual_note, 1)
        multilingual_layout.addWidget(self.speech_multilingual_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.speech_multilingual_row.hide()
        card.addWidget(self.speech_multilingual_row)
        self._link_row(column, "More recording options", lambda: self._open(RECORDING))
        column.addStretch()

    def _clean_up_page(self):
        column, _, _ = self._page(
            "Tidy up what you say?",
            "Optional. After each dictation, an AI model fixes punctuation and drops filler words.",
        )
        card = self._card(column)
        self.cleanup_switch = SettingsSwitch()
        self.cleanup_detail = self._row(card, "Use AI cleanup", "", self.cleanup_switch)
        self.cleanup_switch.toggled.connect(
            lambda value: self._apply_switch(
                CLEANUP, "transcript_cleanup_check", SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, value
            )
        )
        self.cleanup_options, options = _group(spacing=14)
        self.privacy = _label("", "guidedPrivacy")
        options.addWidget(self.privacy)
        self.provider_button = _button("Change", self._provider_action, "guidedLink")
        self.provider_detail = self._row(options, "Cleanup provider", "", self.provider_button)
        self.levels = CleanupChoices(
            [(title, detail) for _, title, detail in cleanup_levels.SEGMENTS[:3]]
        )
        self.levels.setAccessibleName("Cleanup level")
        self.levels.activated.connect(self._choose_level)
        options.addWidget(self.levels)
        self.level_note = _label("")
        options.addWidget(self.level_note)
        self.preview = QFrame()
        self.preview.setObjectName("guidedPreview")
        preview = QBoxLayout(QBoxLayout.Direction.LeftToRight, self.preview)
        preview.setContentsMargins(16, 12, 16, 12)
        preview.setSpacing(18)
        self._rows.append((preview, None))
        for title, text in (("What you say", "Meet at two, actually three."), ("What you get", "")):
            copy = QVBoxLayout()
            copy.setSpacing(4)
            copy.addWidget(_label(title))
            sample = _label(text, "guidedSample")
            copy.addWidget(sample)
            preview.addLayout(copy, 1)
        self.sample_result = sample
        options.addWidget(self.preview)
        self.sample_note = _label(
            "An illustration. Real results depend on your model and instructions."
        )
        options.addWidget(self.sample_note)
        card.addWidget(self.cleanup_options)
        column.addStretch()

    def _output_page(self):
        column, _, _ = self._page(
            "Where should your words go?",
            "Choose what happens when you finish speaking.",
        )
        card = self._card(column)
        self.paste_switch = SettingsSwitch()
        self.paste_detail = self._row(
            card, "Paste automatically", "Insert the text into the app you're typing in.", self.paste_switch
        )
        self.paste_switch.toggled.connect(
            lambda value: self._apply_switch(GENERAL, "auto_paste_check", SettingsKey.AUTO_PASTE, value)
        )
        self.copy_switch = SettingsSwitch()
        self._row(
            card,
            "Copy to clipboard",
            "Keep it ready to paste yourself when auto-paste is off or can't reach an app.",
            self.copy_switch,
        )
        self.copy_switch.toggled.connect(
            lambda value: self._apply_switch(GENERAL, "copy_clipboard_check", SettingsKey.COPY_CLIPBOARD, value)
        )
        self.live_switch = SettingsSwitch()
        self._row(
            card,
            "Show live transcript",
            "Watch words appear while you speak. Not every speech engine supports it.",
            self.live_switch,
        )
        self.live_switch.toggled.connect(
            lambda value: self._apply_switch(
                RECORDING, "streaming_enabled_check", SettingsKey.STREAMING_ENABLED, value
            )
        )
        self._link_row(column, "More output options", lambda: self._open(GENERAL))
        column.addStretch()

    def _ready_page(self):
        column, self.ready_title, self.ready_lede = self._page("You're ready", "")
        self.ready_speech, speech = _group(QHBoxLayout, 8)
        self.ready_speech_note = _label("", "guidedDetail")
        set_style_property(self.ready_speech_note, "tone", "attention")
        speech.addWidget(self.ready_speech_note, 1)
        self.ready_download_button = _button("Download", self._download_speech, "guidedLink")
        speech.addWidget(self.ready_download_button)
        column.addWidget(self.ready_speech)
        self.ready_progress = self._progress_bar()
        column.addWidget(self.ready_progress)
        self.ready_cleanup, cleanup = _group(QHBoxLayout, 8)
        self.ready_cleanup_note = _label("", "guidedDetail")
        set_style_property(self.ready_cleanup_note, "tone", "attention")
        cleanup.addWidget(self.ready_cleanup_note, 1)
        cleanup.addWidget(_button("Add key", lambda: self._open(API_KEYS), "guidedLink"))
        column.addWidget(self.ready_cleanup)
        card = self._card(column)
        key_row = QHBoxLayout()
        key_row.setSpacing(16)
        self.keycap = QLabel()
        self.keycap.setObjectName("guidedKeycap")
        self.keycap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.keycap.setMinimumWidth(64)
        key_row.addWidget(self.keycap, 0, Qt.AlignmentFlag.AlignVCenter)
        self.keycap_help = _label("", "guidedLede")
        key_row.addWidget(self.keycap_help, 1)
        card.addLayout(key_row)
        self.try_box = QPlainTextEdit()
        self.try_box.setObjectName("guidedTry")
        self.try_box.setAccessibleName("Try dictation here")
        self.try_box.setTabChangesFocus(True)
        self.try_box.setMinimumHeight(96)
        self.try_box.setMaximumHeight(140)
        card.addWidget(self.try_box)
        self.try_note = _label("")
        card.addWidget(self.try_note)
        column.addWidget(
            _label("Change any of this later in Settings, or run setup again from File → Guided setup.")
        )
        column.addStretch()

    def _connect_host_page(self):
        column, self.connect_title, self.connect_lede = self._page(
            "Connect to an OpenWhisper Host",
            "Find a host on your network or Tailscale, paste an invite link, or enter an address and code.",
        )

        # Card 1: Nearby hosts (auto-scans LAN + Tailscale)
        nearby_card = self._card(column)
        nearby_header = QHBoxLayout()
        nearby_header.setContentsMargins(0, 0, 0, 0)
        nearby_header.addWidget(_label("Nearby hosts (LAN & Tailscale)", "guidedRowTitle"), 1)
        self.scan_hosts_button = _button("Search again", self._scan_nearby_hosts)
        self.scan_hosts_button.setObjectName("guidedScanHostsButton")
        nearby_header.addWidget(self.scan_hosts_button)
        nearby_card.addLayout(nearby_header)
        self.nearby_hosts_box = QWidget()
        self.nearby_hosts_box.setObjectName("guidedNearbyHostsBox")
        self._nearby_hosts_layout = QVBoxLayout(self.nearby_hosts_box)
        self._nearby_hosts_layout.setContentsMargins(0, 0, 0, 0)
        self._nearby_hosts_layout.setSpacing(8)
        nearby_card.addWidget(self.nearby_hosts_box)

        # Card 2: Paste invite link (openwhisper://pair?... or .openwhisper-invite JSON)
        invite_card = self._card(column)
        invite_card.addWidget(_label("Paste invite link", "guidedRowTitle"))
        invite_card.addWidget(
            _label("Paste an openwhisper://pair link from your host's invite page or Settings.")
        )
        invite_row, invite_layout = _group(QHBoxLayout, 8)
        self.invite_input = QLineEdit()
        self.invite_input.setObjectName("guidedInput")
        self.invite_input.setPlaceholderText("openwhisper://pair?host=...&port=...&fp=...&code=...")
        self.invite_input.setAccessibleName("Host invite link")
        self.invite_input.setMinimumHeight(36)
        self.invite_input.textChanged.connect(self._on_invite_text_changed)
        self.invite_pair_button = _button("Connect with link", self._pair_from_invite_input, "guidedPrimary")
        self.invite_pair_button.setObjectName("guidedInvitePairButton")
        invite_layout.addWidget(self.invite_input, 1)
        invite_layout.addWidget(self.invite_pair_button)
        invite_card.addWidget(invite_row)
        self.invite_preview = _label("")
        self.invite_preview.setObjectName("guidedDetail")
        self.invite_preview.hide()
        invite_card.addWidget(self.invite_preview)

        # Card 3: Manual address + code fallback
        manual_card = self._card(column)
        manual_card.addWidget(_label("Or enter address & pairing code", "guidedRowTitle"))
        manual_row, manual_layout = _group(QHBoxLayout, 8)
        self.manual_host_edit = QLineEdit()
        self.manual_host_edit.setObjectName("guidedInput")
        self.manual_host_edit.setPlaceholderText("192.168.1.20 or studio-pc.local")
        self.manual_host_edit.setAccessibleName("Host address")
        self.manual_host_edit.setMinimumHeight(36)
        self.manual_code_edit = QLineEdit()
        self.manual_code_edit.setObjectName("guidedInput")
        self.manual_code_edit.setPlaceholderText("6-digit code")
        self.manual_code_edit.setAccessibleName("Pairing code")
        self.manual_code_edit.setMaxLength(12)
        self.manual_code_edit.setMinimumHeight(36)
        self.manual_code_edit.setMaximumWidth(130)
        self.manual_pair_button = _button("Pair", self._pair_manual)
        self.manual_pair_button.setObjectName("guidedManualPairButton")
        manual_layout.addWidget(self.manual_host_edit, 1)
        manual_layout.addWidget(self.manual_code_edit)
        manual_layout.addWidget(self.manual_pair_button)
        manual_card.addWidget(manual_row)

        self.connect_status = _label("")
        self.connect_status.setObjectName("guidedDetail")
        column.addWidget(self.connect_status)
        self._link_row(column, "Open Remote engine settings", lambda: self._open(REMOTE_ENGINE))
        column.addStretch()

    def _host_ai_page(self):
        column, self.host_ai_title, self.host_ai_lede = self._page(
            "Host AI & Storage",
            "Use your paired host's speech, AI cleanup, walkthrough write-ups, and storage — no API keys or local models needed here.",
        )
        summary_card = self._card(column)
        self.host_ai_summary = _label("Checking host capabilities…", "guidedRowTitle")
        summary_card.addWidget(self.host_ai_summary)

        for cap_id, label_text, detail_text in (
            ("speech", "Speech Transcription", "Dictation & meetings run on your paired host"),
            ("text_cleanup", "AI Cleanup & Rewrites", "Clean up punctuation, filler words, and styles on the host"),
            ("fast_judgments", "Fast Judgments", "Quick classification and routing on the host"),
            ("walkthrough_writeup", "Walkthrough Write-Ups", "Polished walkthrough documents built on the host"),
            ("meeting_ai", "Meeting AI & Speaker ID", "Speaker identification and live meeting insights on the host"),
            ("records", "Record Storage", "Keep dictations, meetings, and walkthroughs on the host"),
        ):
            badge = QLabel("Checking…")
            badge.setObjectName("guidedCapBadge")
            self.host_ai_badges[cap_id] = badge
            self._row(summary_card, label_text, detail_text, badge)

        action_card = self._card(column)
        self.use_host_ai_switch = SettingsSwitch()
        self.use_host_ai_switch.setObjectName("guidedUseHostAiSwitch")
        self.use_host_ai_switch.setChecked(True)
        self._row(
            action_card,
            "Use host for AI cleanup & processing",
            "Automatically routes supported AI features to your paired host with zero API keys on this computer.",
            self.use_host_ai_switch,
        )
        self.use_host_records_switch = SettingsSwitch()
        self.use_host_records_switch.setObjectName("guidedUseHostRecordsSwitch")
        self.use_host_records_switch.setChecked(False)
        self._row(
            action_card,
            "Store records on host",
            "Save dictations and meetings on your paired host when the host allows record storage.",
            self.use_host_records_switch,
        )
        self.apply_host_ai_button = _button(
            "Apply host services now", self._apply_host_ai_choices, "guidedPrimary"
        )
        self.apply_host_ai_button.setObjectName("guidedApplyHostAiButton")
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.apply_host_ai_button)
        btn_row.addStretch()
        action_card.addLayout(btn_row)
        self.host_ai_applied_note = _label("")
        action_card.addWidget(self.host_ai_applied_note)

        column.addWidget(
            _label("Your host's API keys never leave the host. You can also configure cloud API fallback anytime in Settings.")
        )
        column.addStretch()

    def _host_setup_page(self):
        column, _, _ = self._page(
            "Set up this computer as a Host",
            "Enable sharing, choose which AI and storage services paired devices may use, and share your invite link.",
        )

        # Card 1: Sharing & Capabilities
        caps_card = self._card(column)
        self.host_enable_switch = SettingsSwitch()
        self.host_enable_switch.setObjectName("guidedHostEnableSwitch")
        self.host_enable_switch.toggled.connect(self._on_guided_host_enable_toggled)
        self.host_enable_detail = self._row(
            caps_card,
            "Share this computer's speech engine",
            "Turns on encrypted LAN & Tailscale hosting so your other devices can connect.",
            self.host_enable_switch,
        )

        for cap_id, attr_name, fallback_key, title, desc in remote_invite.HOST_CAPABILITY_SPECS:
            sw = SettingsSwitch()
            sw.setObjectName(f"guidedHostCap_{cap_id}")
            sw.toggled.connect(
                lambda checked, c=cap_id, a=attr_name, fk=fallback_key: self._on_guided_host_cap_toggled(
                    c, a, fk, checked
                )
            )
            self.host_cap_switches[cap_id] = sw
            self._row(caps_card, title, desc, sw)

        records_sw = SettingsSwitch()
        records_sw.setObjectName("guidedHostCap_records")
        records_sw.toggled.connect(self._on_guided_host_records_toggled)
        self.host_cap_switches["records"] = records_sw
        self._row(
            caps_card,
            "Record Storage",
            "Let paired computers store their dictation history, meetings, and walkthroughs here.",
            records_sw,
        )

        # Card 2: Client Invite Link & QR preview
        invite_card = self._card(column)
        invite_card.addWidget(_label("Invite another computer", "guidedRowTitle"))
        invite_card.addWidget(
            _label(
                "Generate a temporary 15-minute link that downloads OpenWhisper Client and pairs "
                "with this computer's verified certificate in one click."
            )
        )
        invite_body = QHBoxLayout()
        invite_body.setSpacing(16)
        left_col = QVBoxLayout()
        left_col.setSpacing(8)
        self.host_invite_url_label = _label("Click Generate & Copy Client Invite Link to create an invite.")
        self.host_invite_url_label.setObjectName("guidedDetail")
        self.host_invite_url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        left_col.addWidget(self.host_invite_url_label)
        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)
        self.host_copy_invite_button = _button(
            "Copy Client Invite Link", self._generate_and_copy_host_invite, "guidedPrimary"
        )
        self.host_copy_invite_button.setObjectName("guidedCopyInviteButton")
        self.host_revoke_invite_button = _button("Revoke link", self._revoke_guided_host_invite)
        self.host_revoke_invite_button.setObjectName("guidedRevokeInviteButton")
        self.host_revoke_invite_button.hide()
        actions_row.addWidget(self.host_copy_invite_button)
        actions_row.addWidget(self.host_revoke_invite_button)
        actions_row.addStretch()
        left_col.addLayout(actions_row)
        left_col.addStretch()
        invite_body.addLayout(left_col, 1)

        self.host_qr_frame = QFrame()
        self.host_qr_frame.setObjectName("guidedQrFrame")
        qr_layout = QVBoxLayout(self.host_qr_frame)
        qr_layout.setContentsMargins(8, 8, 8, 8)
        self.host_qr_label = QLabel()
        self.host_qr_label.setObjectName("guidedQrLabel")
        self.host_qr_label.setFixedSize(148, 148)
        self.host_qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        qr_layout.addWidget(self.host_qr_label)
        self.host_qr_frame.hide()
        invite_body.addWidget(self.host_qr_frame, 0, Qt.AlignmentFlag.AlignTop)
        invite_card.addLayout(invite_body)

        self._link_row(column, "More host & invite options", lambda: self._open(REMOTE_ENGINE))
        column.addStretch()

    # ---- projection ----

    def _queue_refresh(self, *_):
        self._refresh_timer.start(0)

    def refresh(self):
        if self._settings is None:
            return
        settings = self.manager.load_all_settings()
        source = self._settings.audio_device_combo
        items = [(source.itemText(i), source.itemData(i)) for i in range(source.count())]
        old = [
            (self.microphone.itemText(i), self.microphone.itemData(i))
            for i in range(self.microphone.count())
        ]
        with QSignalBlocker(self.microphone):
            if items != old:
                self.microphone.clear()
                for name, token in items:
                    self.microphone.addItem(name, token)
            self.microphone.setCurrentIndex(
                max(0, self.microphone.findData(settings_microphones.preferred_token(settings)))
            )
        backups = len(settings_microphones.saved_priority(settings)) > 1
        self.microphone_detail.setText(
            "The one OpenWhisper listens to. Test it to see that it hears you."
            + (" Your backup microphones stay as they are." if backups else "")
        )
        with QSignalBlocker(self.trigger):
            self.trigger.setCurrentIndex(
                max(0, self.trigger.findData(resolve_recording_trigger_mode(settings)))
            )
        hotkey = self.manager.load_hotkey_settings().get("record_toggle", "")
        if not self.shortcut._capturing:
            self.shortcut.set_hotkey(hotkey)
        self.shortcut_detail.setText(
            shortcut_help(self.shortcut.hotkey, settings)
            # The default is a number-pad key, which most laptops lack.
            + (" No number pad? Choose Change." if _on_number_pad(self.shortcut.hotkey) else "")
        )
        for switch, key in (
            (self.cleanup_switch, SettingsKey.TRANSCRIPT_CLEANUP_ENABLED),
            (self.paste_switch, SettingsKey.AUTO_PASTE),
            (self.copy_switch, SettingsKey.COPY_CLIPBOARD),
            (self.live_switch, SettingsKey.STREAMING_ENABLED),
        ):
            with QSignalBlocker(switch):
                switch.setChecked(resolve_bool_setting(key, settings))
        self._refresh_cleanup(settings)
        self._refresh_speech()
        self._refresh_output()
        self._refresh_ready(settings, hotkey)
        self._refresh_connect_host()
        self._refresh_host_ai()
        self._refresh_host_setup(settings)
        # Rows that come and go change this page's height.
        self.pages.adjustSize()

    def _refresh_cleanup(self, settings):
        enabled = self.cleanup_switch.isChecked()
        self.cleanup_options.setVisible(enabled)
        self.cleanup_detail.setText(
            "Fixes punctuation and formatting after transcription."
            if enabled
            else (
                "Off: you get exactly what the speech engine heard, "
                "and no text is sent anywhere for cleanup."
            )
        )
        custom = bool(cleanup_prompts.custom_prompt(settings))
        level = resolve_transcript_cleanup_level(settings)
        # The built-in tiles must not appear selected while a custom prompt wins.
        self.levels.set_choice(level, custom)
        self.level_note.setText(
            "Your own cleanup prompt is in use. Picking a level asks before replacing it."
        )
        self.level_note.setVisible(custom)
        self.preview.setVisible(not custom)
        self.sample_note.setVisible(not custom)
        self.sample_result.setText(
            "Meet at two, actually three." if level == "light" else "Meet at 3."
        )
        self._provider_needs_key = False
        if enabled:
            name, status, privacy, remote, needs_key = provider_summary(settings)
            self.provider_detail.setText(f"{name} · {status}")
            set_style_property(self.provider_detail, "tone", "attention" if needs_key else "")
            self.provider_button.setText("Add key" if needs_key else "Change")
            self._provider_needs_key = needs_key
            self.privacy.setText(privacy)
            set_style_property(self.privacy, "remote", remote)
            self.ready_cleanup_note.setText(
                f"AI cleanup needs an API key for {name}. Until it has one, "
                "your text arrives without cleanup."
            )
        self.ready_cleanup.setVisible(self._provider_needs_key)

    def _refresh_speech(self):
        client_flow = remote_invite.is_client_only_edition() or self.role == ROLE_CLIENT
        if client_flow:
            pairing = self._current_client_pairing()
            if pairing is not None:
                self.speech_summary.setText(
                    f"Paired host · {pairing.host_name} ({pairing.address}) — no local model needed."
                )
                set_style_property(self.speech_summary, "tone", "ready")
            else:
                self.speech_summary.setText(
                    "Not paired with a host yet. Go back to Connect, or configure a cloud API in Settings."
                )
                set_style_property(self.speech_summary, "tone", "attention")
            self.speech_progress.hide()
            self.ready_progress.hide()
            self.speech_download_button.hide()
            self.speech_multilingual_row.hide()
            self.ready_download_button.hide()
            self.ready_speech.hide()
            self.ready_title.setText("You're ready")
            self.ready_lede.setText("Give it a try before you go.")
            return

        models = self._settings.models
        summary = models.voice_summary()
        progress = self._download_progress
        missing = self._speech_ready is False
        if progress:
            text, done, total = progress
            for bar in (self.speech_progress, self.ready_progress):
                bar.setRange(0, 100 if total > 0 else 0)
                if total > 0:
                    bar.setValue(int(done * 100 / total))
            self.speech_summary.setText(f"{summary} · {text}")
            self.ready_speech_note.setText(f"{summary}: {text}")
            tone = ""
        elif self._speech_ready is None:
            self.speech_summary.setText(f"{summary} · {models.voice_detail()}")
            tone = ""
        elif self._speech_ready:
            from services.guided_setup import speech_model_bundled

            status = (
                "Included and ready"
                if speech_model_bundled(self.manager.load_all_settings())
                else "Downloaded and ready"
            )
            self.speech_summary.setText(f"{summary} · {status}")
            tone = "ready"
        else:
            problem = self._download_error or (
                "Not downloaded yet. Download it now, or it's offered when you first dictate."
            )
            self.speech_summary.setText(f"{summary} · {problem}")
            self.ready_speech_note.setText(
                f"{summary}: {self._download_error}"
                if self._download_error
                else f"{summary} isn't downloaded yet, so your first dictation "
                "starts with the download."
            )
            tone = "attention"
        set_style_property(self.speech_summary, "tone", tone)
        set_style_property(self.ready_speech_note, "tone", tone)
        self.speech_progress.setVisible(bool(progress))
        self.ready_progress.setVisible(bool(progress))
        self.speech_download_button.setVisible(missing and not progress)
        settings = self.manager.load_all_settings()
        show_multilingual = (
            not progress
            and dictation_language.is_english_only_engine(settings)
            and bool(dictation_language.multilingual_engine_options())
        )
        self.speech_multilingual_row.setVisible(show_multilingual)
        self.ready_download_button.setVisible(not progress)
        self.ready_speech.setVisible(missing or bool(progress))
        waiting = missing or bool(progress)
        self.ready_title.setText("Almost ready" if waiting else "You're ready")
        self.ready_lede.setText(
            "One thing left before your first dictation."
            if waiting
            else "Give it a try before you go."
        )

    def _open_multilingual_menu(self):
        self.speech_multilingual_menu.clear()
        for backend, label, detail in dictation_language.multilingual_engine_options():
            action = self.speech_multilingual_menu.addAction(f"{label} \u2014 {detail}")
            action.triggered.connect(
                lambda _checked=False, backend=backend: self._choose_multilingual_engine(backend)
            )
        self.speech_multilingual_menu.addSeparator()
        self.speech_multilingual_menu.addAction("More speech engines\u2026").triggered.connect(
            lambda _checked=False: self._open(VOICE_MODEL)
        )
        self.speech_multilingual_menu.popup(
            self.speech_multilingual_button.mapToGlobal(
                QPoint(0, self.speech_multilingual_button.height())
            )
        )

    def _choose_multilingual_engine(self, backend: str):
        if self._settings is None:
            return
        self._settings.models.choose_backend(backend)
        self._download_error = ""
        self._update_speech_status()
        self.refresh()

    def _refresh_output(self):
        if self._settings._native_wayland:
            self.paste_detail.setText(
                "Uses the desktop's paste shortcut on Hyprland; other Wayland desktops paste manually."
            )
        elif sys.platform == "darwin":
            from services.hotkey_manager import is_accessibility_trusted

            self.paste_detail.setText(
                "Insert the text into the app you're typing in."
                if is_accessibility_trusted()
                else "Needs Accessibility access. Open More output options to grant it, or paste with ⌘V."
            )

    def _refresh_ready(self, settings, hotkey):
        shown = format_hotkey_display(hotkey)
        self.keycap.setText(shown or "No shortcut")
        if shown:
            keycap_help = shortcut_help(hotkey, settings)
        else:
            keycap_help = "Set a shortcut on the Record step, or use the Record button."
        self.keycap_help.setText(keycap_help)
        self.try_box.setPlaceholderText(
            f"Try it here: press {shown} and say something." if shown else "Try it here."
        )
        paste_key = "⌘V" if sys.platform == "darwin" else "Ctrl+V"
        set_style_property(self.try_note, "tone", self._try_result[0] if self._try_result else "")
        if self._try_result:
            self.try_note.setText(self._try_result[1])
        elif not self.paste_switch.isChecked():
            self.try_note.setText(
                f"Auto-paste is off, so click here and press {paste_key} after you speak."
                if self.copy_switch.isChecked()
                else "Auto-paste and copying are both off, so the text only appears in History."
            )
        else:
            self.try_note.setText("Your words land wherever the cursor is, in any app.")

    def _update_speech_status(self):
        if remote_invite.is_client_only_edition() or self.role == ROLE_CLIENT:
            self._speech_ready = None
            return
        self._speech_ready = guided_setup.speech_model_downloaded(self.manager.load_all_settings())

    # ---- host / client setup helpers ----

    def _refresh_connect_host(self):
        service = self._resolve_remote_service()
        pairing = service.client_pairing() if service is not None and hasattr(service, "client_pairing") else None
        if pairing is None:
            from services.remote_asr.settings import load_client_pairing

            pairing = load_client_pairing(self.manager.load_all_settings())

        self.scan_hosts_button.setEnabled(not self._scan_busy and not self._pairing_busy)
        self.invite_pair_button.setEnabled(not self._pairing_busy)
        self.manual_pair_button.setEnabled(not self._pairing_busy)

        # Rebuild nearby host list
        while self._nearby_hosts_layout.count():
            item = self._nearby_hosts_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()

        if self._scan_busy:
            self._nearby_hosts_layout.addWidget(_label("Scanning LAN and Tailscale for hosts…"))
        elif self._scan_result is not None and getattr(self._scan_result, "hosts", ()):
            for host in self._scan_result.hosts:
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(8)
                engine = (host.engine or {}).get("label") or "OpenWhisper Host"
                where = "Tailscale" if host.tailnet is not None else "This network"
                row_layout.addWidget(_label(f"{host.name} · {engine} · {where}"), 1)
                btn = _button(
                    "Connect" if (host.can_pair_without_code or host.approval) else "Use code",
                    lambda _=False, h=host: self._connect_discovered_host(h),
                    "guidedPrimary" if host.can_pair_without_code else "guidedButton",
                )
                btn.setEnabled(host.compatible and not self._pairing_busy)
                row_layout.addWidget(btn)
                self._nearby_hosts_layout.addWidget(row)
        else:
            self._nearby_hosts_layout.addWidget(
                _label(
                    "No nearby hosts found yet. Paste an invite link below, enter your host's "
                    "address and code, or click Search again."
                )
            )

        if pairing is not None and not self._pairing_busy:
            self.connect_status.setText(
                f"Paired with {pairing.host_name} ({pairing.address}) · "
                f"Identity {remote_protocol.short_fingerprint(pairing.fingerprint)}."
            )
            set_style_property(self.connect_status, "tone", "ready")

    def _scan_nearby_hosts(self):
        service = self._resolve_remote_service()
        if service is None or self._scan_busy:
            return
        self._scan_busy = True
        self._refresh_connect_host()

        def _work():
            try:
                scan = service.scan_nearby()
            except Exception:
                logger.debug("Guided setup host scan failed", exc_info=True)
                scan = None
            self._scan_done.emit(scan)

        threading.Thread(target=_work, name="guided-setup-scan", daemon=True).start()

    def _on_scan_done(self, scan):
        self._scan_busy = False
        self._scan_result = scan
        self._refresh_connect_host()

    def _on_invite_text_changed(self, text: str):
        raw = text.strip()
        if not raw:
            self.invite_preview.hide()
            return
        try:
            link = remote_invite.parse_invite_input(raw)
        except remote_invite.InviteValidationError as exc:
            self.invite_preview.setText(str(exc))
            set_style_property(self.invite_preview, "tone", "attention")
            self.invite_preview.show()
            return
        self.invite_preview.setText(
            f"Verified invite for {link.name} ({link.address}) · Fingerprint {link.short_fingerprint}"
        )
        set_style_property(self.invite_preview, "tone", "ready")
        self.invite_preview.show()

    def _pair_from_invite_input(self):
        raw = self.invite_input.text().strip()
        try:
            invite_link = remote_invite.parse_invite_input(raw)
        except remote_invite.InviteValidationError as exc:
            self.connect_status.setText(str(exc))
            set_style_property(self.connect_status, "tone", "attention")
            return
        self._start_invite_pairing(invite_link)

    def _start_invite_pairing(self, invite_link: remote_invite.InviteLink):
        service = self._resolve_remote_service()
        if service is None:
            self.connect_status.setText(
                "Remote engine service isn't ready yet. Open Remote engine settings to pair."
            )
            set_style_property(self.connect_status, "tone", "attention")
            return
        if self._pairing_busy:
            return

        # Never silently replace an existing pairing to a different host.
        existing = service.client_pairing() if hasattr(service, "client_pairing") else None
        if existing is not None:
            existing_fp = str(getattr(existing, "fingerprint", "") or "").upper()
            if existing_fp and existing_fp != invite_link.fingerprint.upper():
                if not remote_invite._qt_confirm_replace_host(self, existing, invite_link):
                    self.connect_status.setText("Kept existing paired host.")
                    set_style_property(self.connect_status, "tone", "")
                    return

        self._pairing_busy = True
        self.connect_status.setText(
            f"Verifying {invite_link.name} ({invite_link.short_fingerprint}) and pairing…"
        )
        set_style_property(self.connect_status, "tone", "")
        self._refresh_connect_host()

        def _work():
            try:
                pairing = remote_invite.pair_with_invite(
                    service,
                    invite_link,
                    confirm_replace=lambda _old, _new: True,
                )
            except Exception as exc:
                self._pair_done.emit(None, exc)
            else:
                self._pair_done.emit(pairing, None)

        threading.Thread(target=_work, name="guided-setup-invite-pair", daemon=True).start()

    def _connect_discovered_host(self, host):
        if host.can_pair_without_code:
            service = self._resolve_remote_service()
            if service is None or self._pairing_busy:
                return
            self._pairing_busy = True
            self.connect_status.setText(f"Connecting to {host.name} over Tailscale…")
            set_style_property(self.connect_status, "tone", "")
            self._refresh_connect_host()

            def _work():
                try:
                    pairing = service.pair(host.address, None, tailscale=True)
                except Exception as exc:
                    self._pair_done.emit(None, exc)
                else:
                    self._pair_done.emit(pairing, None)

            threading.Thread(target=_work, name="guided-setup-ts-pair", daemon=True).start()
            return
        self.manual_host_edit.setText(host.address)
        self.manual_code_edit.setFocus()
        self.connect_status.setText(
            f"Enter the 6-digit pairing code shown on {host.name} and click Pair."
        )
        set_style_property(self.connect_status, "tone", "")

    def _pair_manual(self):
        service = self._resolve_remote_service()
        if service is None or self._pairing_busy:
            return
        address = self.manual_host_edit.text().strip()
        code = "".join(ch for ch in self.manual_code_edit.text() if ch.isdigit())
        try:
            remote_protocol.parse_address(address)
        except ValueError as exc:
            self.connect_status.setText(str(exc))
            set_style_property(self.connect_status, "tone", "attention")
            return
        if not code:
            self.connect_status.setText("Enter the 6-digit pairing code shown on the host.")
            set_style_property(self.connect_status, "tone", "attention")
            return
        self._pairing_busy = True
        self.connect_status.setText(f"Pairing with {address}…")
        set_style_property(self.connect_status, "tone", "")
        self._refresh_connect_host()

        def _work():
            try:
                pairing = service.pair(address, code, tailscale=False)
            except Exception as exc:
                self._pair_done.emit(None, exc)
            else:
                self._pair_done.emit(pairing, None)

        threading.Thread(target=_work, name="guided-setup-manual-pair", daemon=True).start()

    def _on_pair_done(self, pairing, error):
        self._pairing_busy = False
        if error is not None or pairing is None:
            self.connect_status.setText(str(error) if error is not None else "Pairing failed.")
            set_style_property(self.connect_status, "tone", "attention")
            self._refresh_connect_host()
            return
        self.manager.save_setting(SettingsKey.SELECTED_MODEL, "remote")
        self.connect_status.setText(
            f"Paired with {pairing.host_name} · Identity "
            f"{remote_protocol.short_fingerprint(pairing.fingerprint)}."
        )
        set_style_property(self.connect_status, "tone", "ready")
        self._refresh_connect_host()
        self._fetch_paired_host_capabilities()

    def _fetch_paired_host_capabilities(self):
        service = self._resolve_remote_service()
        if service is None:
            return

        def _work():
            try:
                info = remote_invite.fetch_host_capabilities(service)
            except Exception:
                logger.debug("Could not query paired host capabilities", exc_info=True)
                info = {"capabilities": {}, "ai": {}, "engine": {}, "host_name": ""}
            self._caps_done.emit(info)

        threading.Thread(target=_work, name="guided-setup-host-caps", daemon=True).start()

    def _on_caps_done(self, info):
        if isinstance(info, dict):
            self._host_capabilities = dict(info.get("capabilities") or {})
            self._host_ai_meta = dict(info.get("ai") or {})
        self._refresh_host_ai()

    def _current_client_pairing(self):
        service = self._resolve_remote_service()
        if service is not None and hasattr(service, "client_pairing"):
            pairing = service.client_pairing()
            if pairing is not None:
                return pairing
        from services.remote_asr.settings import load_client_pairing

        return load_client_pairing(self.manager.load_all_settings())

    def _refresh_host_ai(self):
        pairing = self._current_client_pairing()
        if pairing is None:
            self.host_ai_summary.setText("No paired host yet — go back to Connect first.")
        else:
            self.host_ai_summary.setText(f"Capabilities offered by {pairing.host_name}")
        caps = self._host_capabilities
        for cap_id, badge in self.host_ai_badges.items():
            if cap_id == "speech":
                enabled = pairing is not None
            elif cap_id == "meeting_ai":
                enabled = bool(caps.get("meeting_ai") or caps.get("speaker_diarization"))
            else:
                enabled = bool(caps.get(cap_id))
            badge.setText("Available" if enabled else "Off on host")
            set_style_property(badge, "enabled", enabled)
        self.use_host_records_switch.setEnabled(bool(caps.get("records")))
        self.apply_host_ai_button.setEnabled(pairing is not None)

    def _apply_host_ai_choices(self):
        caps = dict(self._host_capabilities)
        if not self.use_host_ai_switch.isChecked():
            caps = {k: v for k, v in caps.items() if k == "records"}
        updates = remote_invite.apply_host_ai_defaults(
            caps,
            enable_records=self.use_host_records_switch.isChecked(),
            manager=self.manager,
        )
        enabled_names = ["speech"]
        if SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER in updates:
            enabled_names.append("AI cleanup")
        if SettingsKey.TYPESAFE_PROVIDER in updates:
            enabled_names.append("Fast Judgments")
        if SettingsKey.WALKTHROUGH_WRITE_UP_AGENT in updates:
            enabled_names.append("Walkthrough Write-Ups")
        if SettingsKey.MEETING_AGENT_CORE in updates and SettingsKey.MEETING_SPEAKER_ID_BACKEND in updates:
            enabled_names.append("Meeting AI & Speaker ID")
        elif SettingsKey.MEETING_AGENT_CORE in updates:
            enabled_names.append("Meeting AI")
        elif SettingsKey.MEETING_SPEAKER_ID_BACKEND in updates:
            enabled_names.append("Speaker ID")
        if SettingsKey.REMOTE_RECORDS_LOCATION in updates:
            enabled_names.append("Host Record Storage")
        self.host_ai_applied_note.setText(
            f"Enabled via paired host: {', '.join(enabled_names)}. No API keys needed here."
        )
        set_style_property(self.host_ai_applied_note, "tone", "ready")

    def _refresh_host_setup(self, settings: dict):
        service = self._resolve_remote_service()
        state = service.host_state() if service is not None and hasattr(service, "host_state") else {}
        host_on = bool(
            state.get("enabled")
            if state
            else remote_invite.read_setting_compat(
                "REMOTE_HOST_ENABLED", "remote_host_enabled", False, settings, self.manager
            )
        )
        with QSignalBlocker(self.host_enable_switch):
            self.host_enable_switch.setChecked(host_on)
        for cap_id, attr_name, fallback_key, _title, _desc in remote_invite.HOST_CAPABILITY_SPECS:
            sw = self.host_cap_switches.get(cap_id)
            if sw is not None:
                val = bool(
                    state.get(cap_id)
                    if cap_id in state
                    else remote_invite.read_setting_compat(
                        attr_name, fallback_key, False, settings, self.manager
                    )
                )
                with QSignalBlocker(sw):
                    sw.setChecked(val)
        rec_sw = self.host_cap_switches.get("records")
        if rec_sw is not None:
            rec_val = bool(
                state.get("keep_records")
                if "keep_records" in state
                else remote_invite.read_setting_compat(
                    "REMOTE_HOST_KEEP_RECORDS", "remote_host_keep_records", False, settings, self.manager
                )
            )
            with QSignalBlocker(rec_sw):
                rec_sw.setChecked(rec_val)
        self.host_revoke_invite_button.setVisible(bool(self._host_invite_url))

    def _on_guided_host_enable_toggled(self, checked: bool):
        service = self._resolve_remote_service()
        if service is not None and hasattr(service, "set_host_enabled"):
            threading.Thread(
                target=lambda: service.set_host_enabled(checked),
                name="guided-setup-host-enable",
                daemon=True,
            ).start()
        else:
            remote_invite.save_setting_compat(
                "REMOTE_HOST_ENABLED", "remote_host_enabled", checked, self.manager
            )

    def _on_guided_host_cap_toggled(self, cap_id: str, attr_name: str, fallback_key: str, checked: bool):
        service = self._resolve_remote_service()
        setter = getattr(service, f"set_{cap_id}", None) if service is not None else None
        if callable(setter):
            setter(checked)
        else:
            remote_invite.save_setting_compat(attr_name, fallback_key, checked, self.manager)

    def _on_guided_host_records_toggled(self, checked: bool):
        service = self._resolve_remote_service()
        if service is not None and hasattr(service, "set_keep_records"):
            service.set_keep_records(checked)
        else:
            remote_invite.save_setting_compat(
                "REMOTE_HOST_KEEP_RECORDS", "remote_host_keep_records", checked, self.manager
            )

    def _ensure_host_invite_server(self) -> remote_invite.HostInviteServer:
        service = self._resolve_remote_service()
        if self._host_invite_server is None:
            factory = getattr(service, "invite_server", None) if service is not None else None
            if callable(factory):
                self._host_invite_server = factory()
            else:
                self._host_invite_server = remote_invite.HostInviteServer(
                    host_info_provider=lambda: remote_invite.build_host_info_for_service(
                        service, self.manager.load_all_settings()
                    ),
                    pairing_code_provider=(
                        service.open_pairing if service is not None and hasattr(service, "open_pairing") else None
                    ),
                    cache_enabled_provider=lambda: bool(
                        remote_invite.read_setting_compat(
                            *remote_invite.HOST_INVITE_CACHE_SPEC, False, manager=self.manager
                        )
                    ),
                )
                if service is not None and hasattr(service, "register_invite_server"):
                    try:
                        service.register_invite_server(self._host_invite_server)
                    except Exception:
                        logger.debug("Could not register guided invite server", exc_info=True)
        return self._host_invite_server

    def _generate_and_copy_host_invite(self):
        service = self._resolve_remote_service()
        generate_fn = getattr(service, "generate_invite", None) if service is not None else None
        if callable(generate_fn):
            try:
                generated = generate_fn()
                url = str(generated["url"])
                factory = getattr(service, "invite_server", None)
                if callable(factory):
                    self._host_invite_server = factory()
            except Exception as exc:
                self.host_invite_url_label.setText(f"Could not start invite link server: {exc}")
                set_style_property(self.host_invite_url_label, "tone", "attention")
                return
        else:
            server = self._ensure_host_invite_server()
            port = int(
                remote_invite.read_setting_compat(
                    *remote_invite.HOST_INVITE_PORT_SPEC,
                    remote_invite.DEFAULT_INVITE_PORT,
                    manager=self.manager,
                )
            )
            try:
                if not server.running:
                    server.start(port=port)
                session = server.create_invite()
                info = remote_invite.build_host_info_for_service(
                    service, self.manager.load_all_settings()
                )
                host_addr = info.get("address") or "127.0.0.1"
                url = server.invite_url(str(host_addr), session.token)
            except Exception as exc:
                self.host_invite_url_label.setText(f"Could not start invite link server: {exc}")
                set_style_property(self.host_invite_url_label, "tone", "attention")
                return

        self._host_invite_url = url
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(url)
        self.host_invite_url_label.setText(f"Copied invite link (expires in 15 min):\n{url}")
        set_style_property(self.host_invite_url_label, "tone", "ready")
        self.host_revoke_invite_button.show()

        try:
            pix = _svg_pixmap(render_invite_qr_svg_bytes(url, module_size=4, quiet_zone=3), 148)
            if not pix.isNull():
                self.host_qr_label.setPixmap(pix)
                self.host_qr_frame.show()
        except Exception:
            logger.debug("Could not render invite QR preview", exc_info=True)

    def _revoke_guided_host_invite(self):
        service = self._resolve_remote_service()
        revoke_fn = getattr(service, "revoke_invite", None) if service is not None else None
        if callable(revoke_fn):
            revoke_fn()
        if self._host_invite_server is not None:
            self._host_invite_server.revoke_invite()
        self._host_invite_url = ""
        self.host_invite_url_label.setText("Invite link revoked. Generate a new link anytime.")
        set_style_property(self.host_invite_url_label, "tone", "")
        self.host_qr_frame.hide()
        self.host_revoke_invite_button.hide()

    # ---- actions ----

    def _source_result(self):
        message = self.settings.message_label.text()
        self.show_error(
            message if message.startswith(("Couldn't", "That shortcut", "Could not")) else ""
        )
        self.refresh()

    def _apply_switch(self, destination, attribute, key, value):
        self.settings.ensure_page(destination)
        source = getattr(self.settings, attribute)
        # Resync first: a previous failed save must allow the same choice again.
        with QSignalBlocker(source):
            source.setChecked(resolve_bool_setting(key, self.manager.load_all_settings()))
        if source.isEnabled():
            source.setChecked(value)
        self._source_result()
        with QSignalBlocker(source):
            source.setChecked(resolve_bool_setting(key, self.manager.load_all_settings()))
        if destination == CLEANUP:
            self.settings._update_cleanup_prompt_ui()
            if self.cleanup_switch.isChecked():
                self._privacy_reveal_timer.start(0)

    def _reveal_cleanup_privacy(self):
        if self.page == CLEAN_UP and self.cleanup_switch.isChecked():
            self.scroll.ensureWidgetVisible(self.privacy, 0, 8)

    def _choose_microphone(self, _index):
        source = self.settings.audio_device_combo
        index = source.findData(self.microphone.currentData())
        if index < 0:
            # The worker replaced its inventory while this menu was open.
            self.refresh()
            return
        source.setCurrentIndex(index)
        self._source_result()
        if self.mic_check.running:
            self.mic_check.start()

    def _toggle_mic_check(self):
        if self.mic_check.running:
            self.mic_check.stop()
            return
        self.mic_check.show()
        self.mic_check.start()
        self.pages.adjustSize()

    def _mic_check_changed(self, running):
        self.mic_test_button.setText("Stop" if running else "Test")

    def _choose_trigger(self, _index):
        self.settings.ensure_page(HOTKEYS)
        source = self.settings.record_mode_combo

        def saved():
            return source.findData(resolve_recording_trigger_mode(self.manager.load_all_settings()))

        # Resync first: a previous failed save must allow the same choice again.
        with QSignalBlocker(source):
            source.setCurrentIndex(saved())
        source.setCurrentIndex(source.findData(self.trigger.currentData()))
        self._source_result()
        with QSignalBlocker(source):
            source.setCurrentIndex(saved())
        # Settings shows the double-tap row for the saved mode, not a refused one.
        self.settings._load_hands_free_latch(self.manager.load_all_settings())

    def _capture_shortcut(self):
        self.shortcut.setFocus()
        self.shortcut.begin_capture()

    def _shortcut_captured(self, hotkey):
        self.settings.current_hotkeys = self.manager.load_hotkey_settings()
        self.settings._on_local_hotkey_captured("record_toggle", hotkey)
        self._source_result()

    def _choose_level(self, index):
        level = cleanup_levels.SEGMENTS[index][0]
        if level != cleanup_levels.CUSTOM:
            self.settings.choose_cleanup_level(level)
        # activated fires before the bar finishes selecting the clicked button.
        self._source_result()
        self._queue_refresh()

    def _provider_action(self):
        self._open(API_KEYS if self._provider_needs_key else CLEANUP)

    # ---- the speech download ----

    def _download_speech(self):
        model = guided_setup.speech_model_name(self.manager.load_all_settings())
        if model is None or self._download_model is None:
            self._open(VOICE_MODEL)
            return
        self._download_error = ""
        self.expecting_download = True
        self._requested_model = model
        # The app's own request: its consent dialogs decide whether it starts.
        self._download_model(model)
        self.refresh()

    def shows_model_download(self, name):
        return self._model_busy == name

    def shows_runtime_install(self, component_id):
        """Whether this window is showing the install the user started here."""
        from services.local_asr.catalog import RUNTIME_IDS

        return self.expecting_download and self.isVisible() and component_id in RUNTIME_IDS

    def model_download_started(self, name):
        if not self.expecting_download or name != self._requested_model:
            return
        self._model_busy = name
        self._download_progress = ("Downloading…", 0, 0)
        self.refresh()

    def model_download_progress(self, name, done, total):
        if name != self._model_busy:
            return
        text = "Downloading…"
        if total > 0:
            from services.format_utils import format_size_bytes

            text = f"Downloading… {format_size_bytes(done)} of {format_size_bytes(total)}"
        self._download_progress = (text, done, total)
        self._refresh_speech()

    def model_download_finished(self, name, success):
        if name != self._model_busy:
            return
        self._model_busy = None
        if not success:
            self._download_error = "The download didn't finish. Check your connection and try again."
        self._download_settled()

    def runtime_install_progress(self, component_id, done, total):
        if not self.shows_runtime_install(component_id) and not self._runtime_busy:
            return
        self._runtime_busy = True
        if self._model_busy is None:
            self._download_progress = ("Installing what the model runs on…", done, total)
            self._refresh_speech()

    def runtime_install_finished(self, component_id, success, message):
        if not self._runtime_busy:
            return
        self._runtime_busy = False
        if not success:
            self._download_error = message or "The install didn't finish. Try again."
        self._download_settled()

    def _download_settled(self):
        if self._model_busy is not None:
            return
        if self._runtime_busy:
            self._download_progress = ("Installing what the model runs on…", 0, 0)
        else:
            self._download_progress = None
            self._update_speech_status()
            if self._speech_ready is not False or self._download_error:
                self.expecting_download = False
        if self._settings is not None:
            self.refresh()

    # ---- trying it out ----

    def dictation_received(self, text):
        """A dictation finished while the try-it page is up."""
        if self.page != READY or not self.isVisible() or not text:
            return
        self._tried_text = text
        self._try_result = ("ready", "That worked. OpenWhisper is set up.")
        # A first dictation may have just downloaded the model.
        self._update_speech_status()
        self._queue_refresh()
        # A paste lands a moment after the transcript is announced; wait it out.
        self._try_fill_timer.start(1500 if self.paste_switch.isChecked() else 300)

    def dictation_failed(self, message):
        if self.page != READY or not self.isVisible():
            return
        self._try_fill_timer.stop()
        self._try_result = (
            "attention",
            f"That one didn't come through: {message}" if message
            else "That one didn't come through. Try again.",
        )
        self._queue_refresh()

    def _fill_try_box(self):
        # Auto-paste is off, or could not reach this box: show the words anyway.
        if self.page != READY or self.try_box.toPlainText().strip():
            return
        self.try_box.setPlainText(self._tried_text)
        if self.copy_switch.isChecked():
            where = "It's on your clipboard too."
        else:
            where = "It's saved in History."
        self._try_result = ("ready", f"That worked. Here's what you said. {where}")
        self.refresh()

    def show_error(self, text):
        self.error.setText(text)
        self.error.setVisible(bool(text))

    # ---- navigation ----

    def begin(self, *, welcome: bool) -> None:
        """Start a fresh pass: the welcome page when offered, else the first step."""
        self._finished = guided_setup.is_completed(self.manager)
        self.try_box.clear()
        self._download_error = ""
        self._try_result = None
        self._try_fill_timer.stop()
        self.show_error("")

        pending_invite = remote_invite.consume_pending_startup_invite()
        if pending_invite is not None:
            self.begin_with_invite(pending_invite)
            return

        if remote_invite.is_client_only_edition():
            self.role = ROLE_CLIENT
            if welcome:
                self.show_page(CONNECT_HOST)
            elif self._save_progress(guided_setup.STARTED):
                self.show_page(CONNECT_HOST)
            return

        self.select_role(ROLE_LOCAL)
        if welcome:
            self.show_page(WELCOME)
        elif self._save_progress(guided_setup.STARTED):
            self.show_page(RECORD)

    def begin_with_invite(self, invite: remote_invite.InviteLink) -> None:
        """Jump straight into Connect to Host with ``invite`` pre-filled for confirmation."""
        self._finished = guided_setup.is_completed(self.manager)
        self.role = ROLE_CLIENT
        self._pending_invite = invite
        self.invite_input.setText(invite.uri)
        self.show_page(CONNECT_HOST)

    def _flow_pages(self) -> tuple[int, ...]:
        steps = self._active_steps()
        return tuple(p for p, _title in steps) + (READY,)

    def show_page(self, page):
        self.shortcut.cancel_capture()
        self.mic_check.stop()
        self.mic_check.hide()
        self.page = max(WELCOME, min(HOST_SETUP, page))
        self.pages.setCurrentIndex(self.page)
        # Size the stack to this page alone, or every page scrolls as far as
        # the tallest one (Clean up with its options open).
        for index in range(self.pages.count()):
            policy = QSizePolicy.Policy.Preferred if index == self.page else QSizePolicy.Policy.Ignored
            self.pages.widget(index).setSizePolicy(policy, policy)
        self.pages.adjustSize()

        active_steps = self._active_steps()
        if getattr(self, "_step_spec", None) != active_steps:
            self._rebuild_step_buttons(active_steps)

        step_pages = [p for p, _t in active_steps]
        if self.page in step_pages:
            step = step_pages.index(self.page)
        elif self.page == READY:
            step = len(step_pages)
        else:
            step = -1

        self.step_row.setVisible(self.page != WELCOME)
        for index, button in enumerate(self.step_buttons):
            done = step >= 0 and index < step
            button.setText(f"{'✓' if done else index + 1}  {active_steps[index][1]}")
            set_style_property(button, "active", index == step)
            set_style_property(button, "done", done)

        if self.page == WELCOME:
            skip = "Not now"
        else:
            # Reopened from the File menu after finishing: nothing is skipped.
            skip = "Close" if self._finished else "Skip setup"
        self.skip_button.setText(skip)
        self.skip_button.setVisible(self.page != READY)

        flow = self._flow_pages()
        if self.page == WELCOME:
            show_back = False
        elif remote_invite.is_client_only_edition():
            show_back = self.page in flow and flow.index(self.page) > 0
        elif self.role == ROLE_LOCAL:
            show_back = self.page > RECORD
        else:
            show_back = self.page in flow

        self.back_button.setVisible(show_back)
        self.next_button.setText(
            {WELCOME: "Get started", OUTPUT: "Finish", READY: "Done"}.get(self.page, "Continue")
        )
        self.scroll.verticalScrollBar().setValue(0)
        if self.page in (RECORD, READY):
            self._update_speech_status()
        if self.page != WELCOME:
            self._ensure_settings()
            self.refresh()
            if self.page == CONNECT_HOST and self._scan_result is None and not self._scan_busy:
                self._scan_nearby_hosts()
            elif self.page == HOST_AI:
                self._fetch_paired_host_capabilities()
        self._reflow()
        if self.page == CLEAN_UP and self.cleanup_switch.isChecked():
            self._privacy_reveal_timer.start(0)
        if self.isVisible():
            self._focus_page()

    def _focus_page(self):
        # Never the header's All settings link, where Enter would leave setup.
        (self.try_box if self.page == READY else self.next_button).setFocus()

    def _back(self):
        flow = self._flow_pages()
        if self.page in flow:
            idx = flow.index(self.page)
            if idx > 0:
                self.show_page(flow[idx - 1])
                return
            if not remote_invite.is_client_only_edition() and self.role != ROLE_LOCAL:
                self.show_page(WELCOME)
                return
        self.show_page(max(WELCOME, self.page - 1))

    def _continue(self):
        if self.page == WELCOME:
            if self._save_progress(guided_setup.STARTED):
                first_page = self._active_steps()[0][0]
                self.show_page(first_page)
        elif self.page == OUTPUT:
            if self._save_outcome(guided_setup.COMPLETED):
                self._finished = True
                self.show_page(READY)
        elif self.page == READY:
            self.accept()
        else:
            if self.page == HOST_AI and self.use_host_ai_switch.isChecked():
                if self._current_client_pairing() is not None:
                    self._apply_host_ai_choices()
            flow = self._flow_pages()
            if self.page in flow:
                idx = flow.index(self.page)
                if idx + 1 < len(flow):
                    self.show_page(flow[idx + 1])
                    return
            self.show_page(self.page + 1)

    def _save_outcome(self, state):
        try:
            guided_setup.save_state(state, self.manager)
        except Exception as exc:
            logger.warning("Couldn't save guided setup progress", exc_info=True)
            self.show_error(format_action_error("Couldn't save setup progress", exc))
            return False
        self.shortcut.cancel_capture()
        self.show_error("")
        return True

    def _save_progress(self, state):
        # A finished setup stays finished when it is reopened and left again.
        return True if self._finished else self._save_outcome(state)

    def reject(self):
        if self._save_progress(guided_setup.SKIPPED):
            super().reject()

    def _all_settings(self):
        if self._save_progress(guided_setup.SKIPPED):
            self.accept()
            self.open_all_settings()

    def _open(self, destination):
        self.shortcut.cancel_capture()
        self.open_destination(destination)

    # ---- layout ----

    def _reflow(self):
        narrow = self.width() < round(620 * current_ui_font_scale())
        for row, control in self._rows:
            row.setDirection(
                QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight
            )
            if control is not None:
                row.setAlignment(
                    control,
                    Qt.AlignmentFlag.AlignLeft if narrow else Qt.AlignmentFlag.AlignVCenter,
                )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_rows"):
            self._reflow()

    def changeEvent(self, event):
        super().changeEvent(event)
        if (
            event.type() == QEvent.Type.ActivationChange
            and self.isActiveWindow()
            and self.page != WELCOME
        ):
            # Back from Settings (a download, a key, another engine).
            if self.page in (RECORD, READY):
                self._update_speech_status()
            self._queue_refresh()
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(
            self, "_rows"
        ):
            self._reflow()

    def showEvent(self, event):
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry().size() - QSize(40, 60)
            self.resize(self.size().boundedTo(available).expandedTo(self.minimumSize()))
        super().showEvent(event)
        self._queue_refresh()
        self._focus_page()

    def hideEvent(self, event):
        self.shortcut.cancel_capture()
        self.mic_check.stop()
        self._try_fill_timer.stop()
        # A request that was declined must not claim later installs.
        if self._model_busy is None and not self._runtime_busy:
            self.expecting_download = False
        super().hideEvent(event)
