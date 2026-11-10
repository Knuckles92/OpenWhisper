"""Optional first-run setup: a welcome, Record → Clean up → Output, then a try-it page.

The Settings window remains the owner of save handlers, confirmations and
runtime callbacks. These controls project its saved values; opening,
stepping and illustrating cleanup never applies a recommendation. Settings
is only built once the user leaves the welcome page, so offering setup at
startup costs nothing.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from PyQt6.QtCore import QEvent, QSignalBlocker, QSize, Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QProgressBar,
    QBoxLayout,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from services import cleanup_prompts, guided_setup
from services.credentials import resolve_credential
from services.hotkey_manager import format_hotkey_display
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

WELCOME, RECORD, CLEAN_UP, OUTPUT, READY = range(5)
STEPS = ("Record", "Clean up", "Output")

_STYLE = """
QDialog#guidedSetupDialog { background: @slate-bg; }
QWidget#guidedPage, QWidget#guidedCanvas, QWidget#guidedGroup { background: transparent; }
QFrame#guidedHeader { background: @slate-field; border: none; border-bottom: 1px solid @slate-border; }
QFrame#guidedFooter { background: transparent; border: none; border-top: 1px solid @slate-border-subtle; }
QFrame#guidedCard, QFrame#guidedPreview {
    background: @slate-field; border: 1px solid @slate-border; border-radius: 12px;
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


def provider_summary(settings: dict) -> tuple[str, str, str, bool, bool]:
    """``(name, status, privacy, remote, needs_key)`` from saved configuration.

    A saved key is reported as configured, never as a tested connection.
    """
    provider = resolve_transcript_cleanup_provider(settings)
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
    """

    def __init__(
        self,
        get_settings: Callable,
        parent=None,
        *,
        open_destination: Callable,
        open_all_settings: Callable,
        download_model: Callable | None = None,
        manager=None,
    ):
        super().__init__(parent)
        self._get_settings = get_settings
        self._settings = None
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
        self.page = WELCOME
        self._finished = False
        self._speech_ready = None
        self._provider_needs_key = False
        # (tone, text) once a dictation has been tried on the last page.
        self._try_result = None
        self._tried_text = ""
        self._rows = []
        self.setObjectName("guidedSetupDialog")
        self.setWindowTitle("Set up OpenWhisper")
        self.setWindowIcon(app_icon())
        self.setModal(False)
        # Dictation and the in-app shortcut fallback treat it as OpenWhisper's
        # own window, so the try-it box on the last page takes dictation.
        self.setProperty("openwhisperHotkeyWindow", True)
        self.setMinimumSize(540, 460)
        self.resize(720, 690)
        self.setSizeGripEnabled(True)
        self.setStyleSheet(_STYLE)
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
        self.show_page(WELCOME)

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
        self.step_row, steps = _group(QHBoxLayout, 8)
        self.step_buttons = []
        for index, title in enumerate(STEPS):
            step = QPushButton(title)
            step.setObjectName("guidedStep")
            step.setCursor(Qt.CursorShape.PointingHandCursor)
            step.setAccessibleName(f"Step {index + 1} of {len(STEPS)}: {title}")
            step.clicked.connect(lambda _=False, page=RECORD + index: self.show_page(page))
            steps.addWidget(step, 1)
            self.step_buttons.append(step)
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

    def _welcome_page(self):
        column, _, _ = self._page(
            "Welcome to OpenWhisper",
            "Speak, and your words appear wherever you're typing. "
            "Three quick choices and you're ready.",
            "guidedHeroTitle",
        )
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(64, 64))
        column.insertWidget(0, icon)
        card = self._card(column)
        for svg, title, detail in (
            ("microphone-blue.svg", "Record", "Your microphone and the shortcut that starts dictation."),
            ("wand-purple.svg", "Clean up", "Optional AI tidying for punctuation and filler words."),
            ("bolt-green.svg", "Output", "Paste into the app you're using, copy it, or both."),
        ):
            row = QHBoxLayout()
            row.setSpacing(14)
            glyph = QLabel()
            glyph.setPixmap(design_icon(svg).pixmap(22, 22))
            glyph.setAlignment(Qt.AlignmentFlag.AlignTop)
            row.addWidget(glyph)
            text = QVBoxLayout()
            text.setSpacing(2)
            text.addWidget(_label(title, "guidedRowTitle"))
            text.addWidget(_label(detail))
            row.addLayout(text, 1)
            card.addLayout(row)
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
            self.speech_summary.setText(f"{summary} · Downloaded and ready")
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
        self.ready_download_button.setVisible(not progress)
        self.ready_speech.setVisible(missing or bool(progress))
        waiting = missing or bool(progress)
        self.ready_title.setText("Almost ready" if waiting else "You're ready")
        self.ready_lede.setText(
            "One thing left before your first dictation."
            if waiting
            else "Give it a try before you go."
        )

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
        self._speech_ready = guided_setup.speech_model_downloaded(self.manager.load_all_settings())

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
        if welcome:
            self.show_page(WELCOME)
        elif self._save_progress(guided_setup.STARTED):
            self.show_page(RECORD)

    def show_page(self, page):
        self.shortcut.cancel_capture()
        self.mic_check.stop()
        self.mic_check.hide()
        self.page = max(WELCOME, min(READY, page))
        self.pages.setCurrentIndex(self.page)
        # Size the stack to this page alone, or every page scrolls as far as
        # the tallest one (Clean up with its options open).
        for index in range(self.pages.count()):
            policy = QSizePolicy.Policy.Preferred if index == self.page else QSizePolicy.Policy.Ignored
            self.pages.widget(index).setSizePolicy(policy, policy)
        self.pages.adjustSize()
        step = self.page - RECORD
        self.step_row.setVisible(self.page != WELCOME)
        for index, button in enumerate(self.step_buttons):
            done = index < step
            button.setText(f"{'✓' if done else index + 1}  {STEPS[index]}")
            set_style_property(button, "active", index == step)
            set_style_property(button, "done", done)
        if self.page == WELCOME:
            skip = "Not now"
        else:
            # Reopened from the File menu after finishing: nothing is skipped.
            skip = "Close" if self._finished else "Skip setup"
        self.skip_button.setText(skip)
        self.skip_button.setVisible(self.page != READY)
        self.back_button.setVisible(self.page > RECORD)
        self.next_button.setText(
            {WELCOME: "Get started", OUTPUT: "Finish", READY: "Done"}.get(self.page, "Continue")
        )
        self.scroll.verticalScrollBar().setValue(0)
        if self.page in (RECORD, READY):
            self._update_speech_status()
        if self.page != WELCOME:
            self._ensure_settings()
            self.refresh()
        self._reflow()
        if self.page == CLEAN_UP and self.cleanup_switch.isChecked():
            self._privacy_reveal_timer.start(0)
        if self.isVisible():
            self._focus_page()

    def _focus_page(self):
        # Never the header's All settings link, where Enter would leave setup.
        (self.try_box if self.page == READY else self.next_button).setFocus()

    def _back(self):
        self.show_page(self.page - 1)

    def _continue(self):
        if self.page == WELCOME:
            if self._save_progress(guided_setup.STARTED):
                self.show_page(RECORD)
        elif self.page == OUTPUT:
            if self._save_outcome(guided_setup.COMPLETED):
                self._finished = True
                self.show_page(READY)
        elif self.page == READY:
            self.accept()
        else:
            self.show_page(self.page + 1)

    def _save_outcome(self, state):
        try:
            guided_setup.save_state(state, self.manager)
        except Exception as exc:
            self.show_error(f"Couldn't save setup progress: {exc}")
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
