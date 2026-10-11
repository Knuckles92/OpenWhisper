"""Optional Audio → Text → Output setup, independent of Settings navigation.

The settings window remains the owner of save handlers, confirmations and
runtime callbacks. These controls project its saved values, like Basic does;
opening, stepping and illustrating cleanup never applies recommendations.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from PyQt6.QtCore import QEvent, QSignalBlocker, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
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
from ui_qt.dialogs.settings_destinations import CLEANUP, GENERAL, RECORDING, VOICE_MODEL
from ui_qt.utils.app_icon import app_icon
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets.hotkey_capture import mouse_shortcut_note
from ui_qt.widgets.no_wheel import ElidingComboBox
from ui_qt.widgets.profile_hotkey_input import ProfileHotkeyInput
from ui_qt.widgets.segmented_bar import SegmentedBar
from ui_qt.widgets.settings_switch import SettingsSwitch
from ui_qt.widgets.wrapped_label import WrappedLabel

_STYLE = """
QDialog#guidedSetupDialog, QDialog#guidedSetupInvitation { background: @slate-bg; }
QWidget#guidedPage, QWidget#guidedCanvas { background: transparent; }
QFrame#guidedHeader { background: @slate-field; border-bottom: 1px solid @slate-border; }
QFrame#guidedPanel, QFrame#guidedPreview {
    background: @slate-field; border: 1px solid @slate-border; border-radius: 10px;
}
QLabel#guidedBrand, QLabel#guidedRowTitle { color: @slate-text; font-size: 15px; font-weight: 600; border: none; background: transparent; }
QLabel#guidedTitle { color: @slate-text; font-size: 24px; font-weight: 600; border: none; background: transparent; }
QLabel#guidedPanelTitle { color: @slate-text; font-size: 20px; font-weight: 600; border: none; background: transparent; }
QLabel#guidedDetail { color: @slate-text-3; font-size: 13px; border: none; background: transparent; }
QLabel#guidedSample { color: @slate-text; font-size: 18px; border: none; background: transparent; }
QLabel#guidedPrivacy { color: @slate-text-2; background: @slate-raised; border: 1px solid @slate-border; border-radius: 7px; padding: 12px; font-size: 13px; }
QLabel#guidedPrivacy[remote="true"] { color: @warning-text; background: @warning-tint; border-color: @warning-tint-border; }
QLabel#guidedError { color: @danger-text; font-size: 13px; background: transparent; border: none; }
QLabel#guidedStep { color: @slate-text-3; padding: 10px 6px; border-bottom: 2px solid @slate-border; font-size: 14px; background: transparent; }
QLabel#guidedStep[active="true"] { color: @accent; border-color: @accent; font-weight: 600; }
QLabel#guidedStep[done="true"] { color: @slate-text-2; border-color: @accent; }
QPushButton#guidedLink { color: @accent; background: transparent; border: none; padding: 6px 8px; font-size: 13px; }
QPushButton#guidedLink:hover { color: @accent-hover; text-decoration: underline; }
QPushButton#guidedLink:focus { border: 1px solid @accent; border-radius: 5px; }
QPushButton#guidedButton { color: @slate-text; background: @slate-field; border: 1px solid @slate-border-strong; border-radius: 7px; padding: 10px 18px; font-size: 13px; }
QPushButton#guidedButton:hover { background: @slate-raised; }
QPushButton#guidedButton:focus { border-color: @accent; }
QPushButton#guidedPrimary { color: @on-accent; background: @accent; border: 1px solid @accent; border-radius: 7px; padding: 10px 18px; font-size: 13px; font-weight: 600; }
QPushButton#guidedPrimary:hover { background: @accent-hover; }
QPushButton#guidedPrimary:focus { border: 2px solid @slate-text; }
QScrollArea#guidedScroll { background: transparent; border: none; }
"""


def provider_summary(settings: dict) -> tuple[str, str, str, bool]:
    """Saved configuration, never a claim of a tested network connection."""
    provider = resolve_transcript_cleanup_provider(settings)
    profile = get_profile(provider, settings)
    if profile is None:
        return (
            "Provider unavailable",
            "Choose a provider in All settings.",
            "The saved cleanup provider is unavailable. Choose a provider before using AI cleanup.",
            True,
        )
    local = profile.is_local
    privacy = (
        f"Transcript text is sent to {profile.name} on this computer when cleanup is on."
        if local
        else f"Transcript text is sent to {profile.name} when cleanup is on."
    ) + " No audio is sent for cleanup."
    if profile.api_key_env:
        status = (
            "API key saved · Connection not tested"
            if resolve_credential(profile.api_key_env)
            else "API key needed"
        )
    else:
        status = "No API key needed · Connection not tested"
    return profile.name, status, privacy, not local


def _label(text, name="guidedDetail"):
    label = WrappedLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setObjectName(name)
    return label


def _button(text, callback, name="guidedButton"):
    button = QPushButton(text)
    button.setObjectName(name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setAutoDefault(False)
    button.clicked.connect(callback)
    return button


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


class GuidedSetupInvitation(QDialog):
    start_requested = pyqtSignal()

    def __init__(self, parent=None, *, manager=None):
        super().__init__(parent)
        self.manager = settings_manager if manager is None else manager
        self.setObjectName("guidedSetupInvitation")
        self.setWindowTitle("Welcome to OpenWhisper")
        self.setWindowIcon(app_icon())
        self.setModal(False)
        self.setStyleSheet(_STYLE)
        self.setMinimumWidth(460)
        self.resize(560, 260)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)
        layout.addWidget(
            _label("Would you like help setting up OpenWhisper?", "guidedPanelTitle")
        )
        layout.addWidget(
            _label(
                "Choose your microphone, how your words are cleaned up, and where your text goes. You can change these choices later."
            )
        )
        self.error = _label("", "guidedError")
        self.error.hide()
        layout.addWidget(self.error)
        row = QHBoxLayout()
        self.skip_button = _button("Skip for now", self.reject)
        self.start_button = _button("Start guided setup", self.start, "guidedPrimary")
        row.addWidget(self.skip_button)
        row.addStretch()
        row.addWidget(self.start_button)
        layout.addLayout(row)

    def _save(self, state):
        try:
            guided_setup.save_state(state, self.manager)
        except Exception as exc:
            self.error.setText(f"Couldn't save setup progress: {exc}")
            self.error.show()
            return False
        return True

    def start(self):
        if self._save(guided_setup.STARTED):
            self.accept()
            self.start_requested.emit()

    def reject(self):
        if self._save(guided_setup.SKIPPED):
            super().reject()


class GuidedSetupDialog(QDialog):
    """Focused projections of existing controls; choices save immediately."""

    def __init__(
        self,
        settings,
        parent=None,
        *,
        open_destination: Callable,
        open_all_settings: Callable,
        manager=None,
    ):
        super().__init__(parent)
        self.settings = settings
        self.manager = settings_manager if manager is None else manager
        self.open_destination = open_destination
        self.open_all_settings = open_all_settings
        self.step = 0
        self._rows = []
        self.setObjectName("guidedSetupDialog")
        self.setWindowTitle("OpenWhisper · Guided setup")
        self.setWindowIcon(app_icon())
        self.setModal(False)
        self.setMinimumSize(620, 460)
        self.resize(920, 850)
        self.setSizeGripEnabled(True)
        self.setStyleSheet(_STYLE)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh)
        self._privacy_reveal_timer = QTimer(self)
        self._privacy_reveal_timer.setSingleShot(True)
        self._privacy_reveal_timer.timeout.connect(self._reveal_cleanup_privacy)
        self._setup_ui()
        self.settings.ensure_page(RECORDING)
        # The existing microphone worker fills this model asynchronously.
        model = self.settings.audio_device_combo.model()
        for signal in (
            model.modelReset,
            model.rowsInserted,
            model.rowsRemoved,
            model.dataChanged,
        ):
            signal.connect(self._queue_refresh)
        self.set_step(0)

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QFrame()
        header.setObjectName("guidedHeader")
        row = QHBoxLayout(header)
        row.setContentsMargins(24, 14, 24, 14)
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(32, 32))
        row.addWidget(icon)
        row.addWidget(_label("OpenWhisper", "guidedBrand"))
        row.addWidget(_label("Guided setup"))
        row.addStretch()
        self.all_settings_button = _button(
            "All settings", self._all_settings, "guidedLink"
        )
        row.addWidget(self.all_settings_button)
        root.addWidget(header)
        body = QWidget()
        body.setObjectName("guidedCanvas")
        column = QVBoxLayout(body)
        column.setContentsMargins(28, 20, 28, 16)
        column.setSpacing(14)
        column.addWidget(_label("Set up dictation", "guidedTitle"))
        column.addWidget(_label("Choose how your spoken words become text."))
        steps = QHBoxLayout()
        self.step_labels = []
        for title in ("Audio", "Text", "Output"):
            label = _label(title, "guidedStep")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            steps.addWidget(label, 1)
            self.step_labels.append(label)
        column.addLayout(steps)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("guidedScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pages = QStackedWidget()
        self.pages.setObjectName("guidedCanvas")
        self.pages.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._audio_page()
        self._text_page()
        self._output_page()
        self.scroll.setWidget(self.pages)
        column.addWidget(self.scroll, 1)
        self.error = _label("", "guidedError")
        self.error.hide()
        column.addWidget(self.error)
        actions = QHBoxLayout()
        self.back_button = _button("Back", lambda: self.set_step(self.step - 1))
        actions.addWidget(self.back_button)
        actions.addStretch()
        self.next_button = _button("Continue", self._continue, "guidedPrimary")
        actions.addWidget(self.next_button)
        column.addLayout(actions)
        footer = QHBoxLayout()
        footer.addWidget(
            _label("Changes save automatically. You can change them later."), 1
        )
        self.skip_button = _button("Skip setup", self.reject, "guidedLink")
        footer.addWidget(self.skip_button)
        column.addLayout(footer)
        root.addWidget(body, 1)

    def _panel(self, title, detail):
        page = QWidget()
        page.setObjectName("guidedPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        panel = QFrame()
        panel.setObjectName("guidedPanel")
        column = QVBoxLayout(panel)
        column.setContentsMargins(22, 18, 22, 18)
        column.setSpacing(12)
        column.addWidget(_label(title, "guidedPanelTitle"))
        column.addWidget(_label(detail))
        layout.addWidget(panel)
        layout.addStretch()
        self.pages.addWidget(page)
        return column

    def _row(self, column, title, detail, control):
        row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        row.setSpacing(18)
        text = QVBoxLayout()
        text.setSpacing(4)
        text.addWidget(_label(title, "guidedRowTitle"))
        description = _label(detail)
        text.addWidget(description)
        row.addLayout(text, 1)
        row.addWidget(control)
        control.setAccessibleName(title)
        column.addLayout(row)
        self._rows.append((row, control))
        return description

    def _audio_page(self):
        column = self._panel(
            "Choose how you record", "Pick a microphone and a shortcut for dictation."
        )
        self.microphone = ElidingComboBox()
        self.microphone.setObjectName("basicSettingsCombo")
        self.microphone.setMinimumHeight(38)
        self.microphone.setMinimumWidth(200)
        self.microphone.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._row(
            column,
            "Microphone",
            "Your saved microphone backups stay in place.",
            self.microphone,
        )
        self.microphone.currentIndexChanged.connect(self._choose_microphone)
        holder = QWidget()
        shortcut_row = QHBoxLayout(holder)
        shortcut_row.setContentsMargins(0, 0, 0, 0)
        self.shortcut = ProfileHotkeyInput()
        self.shortcut.setAccessibleName("Recording shortcut")
        self.shortcut.setToolTip("Click Change, then press a shortcut. Escape cancels.")
        self.shortcut.capture_changed.connect(self.settings._on_profile_capture)
        self.shortcut.captured.connect(self._shortcut_captured)
        shortcut_row.addWidget(self.shortcut)
        shortcut_row.addWidget(_button("Change", self._capture_shortcut))
        self.shortcut_detail = self._row(column, "Recording shortcut", "", holder)
        self._row(
            column,
            "More audio options",
            "Manage backups, recording mode, and microphone permissions.",
            _button("Audio settings", lambda: self._open(RECORDING), "guidedLink"),
        )

        self.speech_summary = self._row(
            column,
            "Speech engine",
            "",
            _button("Change", lambda: self._open(VOICE_MODEL), "guidedLink"),
        )

    def _text_page(self):
        column = self._panel(
            "How much cleanup would you like?",
            "Choose how closely the transcript follows your words.",
        )
        self.cleanup_switch = SettingsSwitch()
        self._row(
            column,
            "Use AI cleanup",
            "Fix punctuation and formatting after transcription.",
            self.cleanup_switch,
        )
        self.cleanup_switch.toggled.connect(
            lambda value: self._apply_switch(
                CLEANUP,
                "transcript_cleanup_check",
                SettingsKey.TRANSCRIPT_CLEANUP_ENABLED,
                value,
            )
        )
        self.cleanup_options = QWidget()
        options = QVBoxLayout(self.cleanup_options)
        options.setContentsMargins(0, 0, 0, 0)
        options.setSpacing(14)
        self.privacy = _label("", "guidedPrivacy")
        options.addWidget(self.privacy)
        self.provider_detail = self._row(
            options,
            "Cleanup provider",
            "",
            _button("Change", lambda: self._open(CLEANUP), "guidedLink"),
        )
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
        for title, text in (
            ("What you say", "Meet at two, actually three."),
            ("Your transcript", ""),
        ):
            copy = QVBoxLayout()
            copy.addWidget(_label(title))
            sample = _label(text, "guidedSample")
            copy.addWidget(sample)
            preview.addLayout(copy, 1)
        self.sample_result = sample
        options.addWidget(self.preview)
        self.sample_note = _label(
            "Illustrative example. Actual results depend on your model and instructions."
        )
        options.addWidget(self.sample_note)
        column.addWidget(self.cleanup_options)
        self.cleanup_off_note = _label(
            "Cleanup is off. The speech engine's transcript is kept without sending text to a cleanup provider."
        )
        column.addWidget(self.cleanup_off_note)

    def _output_page(self):
        column = self._panel(
            "Where should your text go?", "Choose what happens when dictation finishes."
        )
        self.paste_switch = SettingsSwitch()
        self.paste_detail = self._row(
            column,
            "Paste automatically",
            "Insert the transcript into the active window.",
            self.paste_switch,
        )
        self.paste_switch.toggled.connect(
            lambda value: self._apply_switch(
                GENERAL, "auto_paste_check", SettingsKey.AUTO_PASTE, value
            )
        )
        self.copy_switch = SettingsSwitch()
        self._row(
            column,
            "Copy to clipboard",
            "Keep text ready for manual paste when auto-paste is off or unavailable.",
            self.copy_switch,
        )
        self.copy_switch.toggled.connect(
            lambda value: self._apply_switch(
                GENERAL, "copy_clipboard_check", SettingsKey.COPY_CLIPBOARD, value
            )
        )
        self.live_switch = SettingsSwitch()
        self._row(
            column,
            "Show live transcript",
            "See words appear while you speak. Availability depends on your speech engine.",
            self.live_switch,
        )
        self.live_switch.toggled.connect(
            lambda value: self._apply_switch(
                RECORDING,
                "streaming_enabled_check",
                SettingsKey.STREAMING_ENABLED,
                value,
            )
        )
        self._row(
            column,
            "More output options",
            "Clipboard restoration and platform permissions are available in All settings.",
            _button("Output settings", lambda: self._open(GENERAL), "guidedLink"),
        )
        self.ready_note = _label("")
        column.addWidget(self.ready_note)

    def _queue_refresh(self, *_):
        self._refresh_timer.start(0)

    def refresh(self):
        settings = self.manager.load_all_settings()
        source = self.settings.audio_device_combo
        items = [
            (source.itemText(i), source.itemData(i)) for i in range(source.count())
        ]
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
                max(
                    0,
                    self.microphone.findData(
                        settings_microphones.preferred_token(settings)
                    ),
                )
            )
        hotkeys = self.manager.load_hotkey_settings()
        if not self.shortcut._capturing:
            self.shortcut.set_hotkey(hotkeys.get("record_toggle", ""))
        if resolve_recording_trigger_mode(settings) == "push_hold":
            detail = "Hold to record; release to transcribe."
            if resolve_recording_hands_free_latch(settings):
                detail += " Double-tap for hands-free recording."
        else:
            detail = "Press to start or stop dictation."
        self.shortcut_detail.setText(
            " ".join(filter(None, (detail, mouse_shortcut_note(self.shortcut.hotkey))))
        )
        for switch, key in (
            (self.cleanup_switch, SettingsKey.TRANSCRIPT_CLEANUP_ENABLED),
            (self.paste_switch, SettingsKey.AUTO_PASTE),
            (self.copy_switch, SettingsKey.COPY_CLIPBOARD),
            (self.live_switch, SettingsKey.STREAMING_ENABLED),
        ):
            with QSignalBlocker(switch):
                switch.setChecked(resolve_bool_setting(key, settings))
        enabled = self.cleanup_switch.isChecked()
        self.cleanup_options.setVisible(enabled)
        self.cleanup_off_note.setVisible(not enabled)
        custom = bool(cleanup_prompts.custom_prompt(settings))
        level = resolve_transcript_cleanup_level(settings)
        # The built-in tiles must not appear selected while a custom prompt wins.
        self.levels.set_choice(level, custom)
        self.level_note.setText(
            "Your custom prompt is in use. Choosing a level asks before replacing it."
            if custom
            else cleanup_levels.SHORT_NOTES[level]
        )
        self.level_note.setVisible(custom)
        self.preview.setVisible(not custom)
        self.sample_note.setVisible(not custom)
        self.sample_result.setText(
            "Meet at two, actually three." if level == "light" else "Meet at 3."
        )
        if enabled:
            name, status, privacy, remote = provider_summary(settings)
            self.provider_detail.setText(f"{name} · {status}")
            self.privacy.setText(privacy)
            set_style_property(self.privacy, "remote", remote)
        self.speech_summary.setText(
            f"{self.settings.models.voice_summary()} · {self.settings.models.voice_detail()}"
        )
        if self.settings._native_wayland:
            self.paste_detail.setText(
                "Uses the desktop's paste shortcut on Hyprland; other Wayland desktops require manual paste."
            )
        elif sys.platform == "darwin":
            from services.hotkey_manager import is_accessibility_trusted

            self.paste_detail.setText(
                "Insert the transcript into the active window."
                if is_accessibility_trusted()
                else "Auto-paste needs Accessibility access. Open Output settings to grant access, or paste manually with ⌘V."
            )
        shortcut = (
            format_hotkey_display(hotkeys.get("record_toggle", ""))
            or "the Record button"
        )
        self.ready_note.setText(
            f"Use {shortcut} to start dictation. If your speech model isn't installed yet, open All settings to set it up."
        )

    def _source_result(self):
        message = self.settings.message_label.text()
        self.show_error(
            message
            if message.startswith(("Couldn't", "That shortcut", "Could not"))
            else ""
        )
        self.refresh()

    def _apply_switch(self, destination, attribute, key, value):
        self.settings.ensure_page(destination)
        source = getattr(self.settings, attribute)
        # Resync first: a previous failed save must allow the same choice again.
        with QSignalBlocker(source):
            source.setChecked(
                resolve_bool_setting(key, self.manager.load_all_settings())
            )
        if source.isEnabled():
            source.setChecked(value)
        self._source_result()
        with QSignalBlocker(source):
            source.setChecked(
                resolve_bool_setting(key, self.manager.load_all_settings())
            )

        if destination == CLEANUP:
            self.settings._update_cleanup_prompt_ui()
            if self.cleanup_switch.isChecked():
                self._privacy_reveal_timer.start(0)

    def _reveal_cleanup_privacy(self):
        if self.step == 1 and self.cleanup_switch.isChecked():
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

    def show_error(self, text):
        self.error.setText(text)
        self.error.setVisible(bool(text))

    def set_step(self, step):
        self.shortcut.cancel_capture()
        self.step = max(0, min(2, step))
        self.pages.setCurrentIndex(self.step)
        for index, label in enumerate(self.step_labels):
            label.setText(
                f"{'✓' if index < self.step else index + 1}  {('Audio', 'Text', 'Output')[index]}"
            )
            set_style_property(label, "active", index == self.step)
            set_style_property(label, "done", index < self.step)
        self.back_button.setEnabled(self.step > 0)
        self.next_button.setText("Finish setup" if self.step == 2 else "Continue")
        self.scroll.verticalScrollBar().setValue(0)
        self.refresh()
        self._reflow()
        if self.step == 1 and self.cleanup_switch.isChecked():
            self._privacy_reveal_timer.start(0)

    def _continue(self):
        if self.step < 2:
            self.set_step(self.step + 1)
        elif self._save_outcome(guided_setup.COMPLETED):
            self.accept()

    def _save_outcome(self, state):
        try:
            guided_setup.save_state(state, self.manager)
        except Exception as exc:
            self.show_error(f"Couldn't save setup progress: {exc}")
            return False
        self.shortcut.cancel_capture()
        self.show_error("")
        return True

    def reject(self):
        if self._save_outcome(guided_setup.SKIPPED):
            super().reject()

    def _all_settings(self):
        if self._save_outcome(guided_setup.SKIPPED):
            self.accept()
            self.open_all_settings()

    def _open(self, destination):
        self.shortcut.cancel_capture()
        self.open_destination(destination)

    def _reflow(self):
        narrow = self.width() < round(760 * current_ui_font_scale())
        for row, control in self._rows:
            row.setDirection(
                QBoxLayout.Direction.TopToBottom
                if narrow
                else QBoxLayout.Direction.LeftToRight
            )
            if control is not None:
                row.setAlignment(
                    control,
                    Qt.AlignmentFlag.AlignLeft
                    if narrow
                    else Qt.AlignmentFlag.AlignVCenter,
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
            and hasattr(self, "microphone")
        ):
            self._queue_refresh()
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
        ) and hasattr(self, "_rows"):
            self._reflow()

    def showEvent(self, event):
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry().size() - QSize(40, 60)
            self.resize(self.size().boundedTo(available).expandedTo(self.minimumSize()))
        super().showEvent(event)
        self._queue_refresh()

    def hideEvent(self, event):
        self.shortcut.cancel_capture()
        super().hideEvent(event)
