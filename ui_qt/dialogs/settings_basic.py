"""Curated settings pages that use the full Settings window's save paths.

The controls are projections of existing settings, not another store. Model
and cloud pages are built only when an edit needs their handler; opening Basic
never loads speech runtimes or fetches a provider's model catalog.
"""

import sys

from PyQt6.QtCore import QEvent, QSignalBlocker, Qt
from PyQt6.QtWidgets import (
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.credentials import resolve_credential
from services.settings import (
    SettingsKey,
    UiFontScale,
    UiTheme,
    resolve_meeting_end_polish,
    resolve_meeting_end_report,
    resolve_recording_trigger_mode,
    resolve_transcript_cleanup_provider,
    resolve_ui_font_scale,
    resolve_ui_theme,
    resolve_update_check_enabled,
    resolve_update_notify_enabled,
    setting_value,
)
from services.text_llm import get_profile, profile_display_name
from ui_qt.dialogs.settings_destinations import (
    BASIC_APP,
    BASIC_DICTATION,
    CLEANUP,
    GENERAL,
    MEETING_AFTER,
    MEETING_INTELLIGENCE,
    MEETING_VOICE,
    RECORDING,
    VOICE_MODEL,
)
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.icons import design_icon
from ui_qt.widgets.no_wheel import ElidingComboBox
from ui_qt.widgets.profile_hotkey_input import ProfileHotkeyInput
from ui_qt.widgets.settings_switch import SettingsSwitch  # noqa: F401  (re-exported)
from ui_qt.widgets.speech_backend_picker import populate_backend_combo
from ui_qt.widgets.wrapped_label import WrappedLabel


class BasicSettingsPage(QWidget):
    """One of the Dictation, Meetings, and App tabs."""

    def __init__(self, dialog, destination):
        super().__init__()
        self.setObjectName("basicSettingsPage")
        self.dialog = dialog
        self.destination = destination
        self.controls = {}
        self._bindings = []
        self._rows = []
        self.shortcut = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        if destination == BASIC_DICTATION:
            self._build_dictation(layout)
        elif destination == BASIC_APP:
            self._build_app(layout)
        else:
            self._build_meetings(layout)
        self.refresh()

    def _group(self, layout, title, icon):
        panel = QFrame()
        panel.setObjectName("basicSettingsGroup")
        column = QVBoxLayout(panel)
        column.setContentsMargins(18, 12, 18, 6)
        column.setSpacing(0)
        header = QHBoxLayout()
        glyph = QLabel()
        glyph.setObjectName("basicSettingsIcon")
        glyph.setPixmap(design_icon(icon).pixmap(20, 20))
        header.addWidget(glyph)
        name = QLabel(title)
        name.setObjectName("basicSettingsGroupTitle")
        header.addWidget(name)
        header.addStretch()
        column.addLayout(header)
        layout.addWidget(panel)
        return column

    def _row(self, group, title, description, control):
        if group.count() > 1:
            divider = QFrame()
            divider.setObjectName("basicSettingsDivider")
            divider.setFixedHeight(1)
            group.addWidget(divider)
        row = QHBoxLayout()
        row.setContentsMargins(0, 7, 0, 7)
        row.setSpacing(24)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        name = WrappedLabel(title)
        name.setObjectName("basicSettingsRowTitle")
        detail = WrappedLabel(description)
        detail.setObjectName("basicSettingsDescription")
        copy.addWidget(name)
        copy.addWidget(detail)
        row.addLayout(copy, 1)
        row.addWidget(control, alignment=Qt.AlignmentFlag.AlignVCenter)
        control.setAccessibleName(title)
        group.addLayout(row)
        self._rows.append((row, control))
        return detail

    def _reflow_rows(self):
        narrow = self.width() < round(640 * current_ui_font_scale())
        for row, control in self._rows:
            expanding = (
                control.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Expanding
            )
            row.setDirection(
                QBoxLayout.Direction.TopToBottom
                if narrow
                else QBoxLayout.Direction.LeftToRight
            )
            row.setSpacing(8 if narrow else 24)
            row.setStretch(1, 2 if expanding and not narrow else 0)
            row.setAlignment(
                control,
                Qt.AlignmentFlag.AlignLeft
                if narrow and not expanding
                else Qt.AlignmentFlag.AlignVCenter,
            )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow_rows()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
        ) and hasattr(self, "_rows"):
            self._reflow_rows()

    def _combo(self, *, expanding=False):
        combo = ElidingComboBox()
        combo.setObjectName("basicSettingsCombo")
        combo.setMinimumHeight(38)
        combo.setMinimumWidth(160)
        if expanding:
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        else:
            combo.setMaximumWidth(260)
        return combo

    def _bind(self, control, destination, attribute, key, resolver=None):
        resolver = resolver or (lambda settings: setting_value(key, settings))
        self.controls[key] = control
        self._bindings.append((control, resolver))
        signal = (
            control.toggled
            if isinstance(control, SettingsSwitch)
            else control.currentIndexChanged
        )
        signal.connect(lambda _value: self._apply(control, destination, attribute))

    def _apply(self, control, destination, attribute):
        # Use the owning page's original handler, including callbacks,
        # platform restrictions, confirmations, and multi-key changes.
        # Building that page refreshes the projections, so retain the edit
        # before materializing its source.
        value = (
            control.isChecked()
            if isinstance(control, SettingsSwitch)
            else control.currentData()
        )
        self.dialog.ensure_page(destination)
        source = getattr(self.dialog, attribute)
        if isinstance(control, SettingsSwitch):
            source.setChecked(value)
        else:
            index = source.findData(value)
            if index >= 0:
                source.setCurrentIndex(index)
        self.refresh()
        # A failed save or a declined confirmation restores the projection
        # and its source, so trying the same change again still emits a signal.
        with QSignalBlocker(source):
            if isinstance(control, SettingsSwitch):
                source.setChecked(control.isChecked())
            else:
                source.setCurrentIndex(max(0, source.findData(control.currentData())))

    def _toggle(
        self, group, title, description, destination, attribute, key, resolver=None
    ):
        switch = SettingsSwitch()
        self._row(group, title, description, switch)
        self._bind(switch, destination, attribute, key, resolver)
        return switch

    def _microphone(self, group):
        # Audio discovery already runs on a worker and keeps unavailable saved
        # devices selectable. Share its inventory between all three views.
        self.dialog.ensure_page(RECORDING)
        combo = self._combo(expanding=True)
        self._row(group, "Microphone", "Used for dictation and meetings.", combo)
        self._bind(
            combo,
            RECORDING,
            "audio_device_combo",
            SettingsKey.AUDIO_INPUT_DEVICE,
            lambda settings: settings.get(SettingsKey.AUDIO_INPUT_DEVICE),
        )

    def _shortcut(self, group):
        holder = QWidget()
        holder.setObjectName("basicSettingsShortcut")
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.shortcut = ProfileHotkeyInput()
        self.shortcut.setMinimumWidth(140)
        self.shortcut.setMaximumWidth(210)
        self.shortcut.setAccessibleName("Recording shortcut")
        self.shortcut.capture_changed.connect(self.dialog._on_profile_capture)
        self.shortcut.captured.connect(
            lambda hotkey: self.dialog._on_local_hotkey_captured(
                "record_toggle", hotkey
            )
        )
        row.addWidget(self.shortcut)
        change = QPushButton("Change")
        change.setObjectName("basicSettingsChangeShortcut")
        change.clicked.connect(self._begin_shortcut_capture)
        row.addWidget(change)
        self.shortcut_description = self._row(group, "Recording shortcut", "", holder)

    def _begin_shortcut_capture(self):
        self.shortcut.setFocus()
        self.shortcut.begin_capture()

    def _advanced_link(self, layout, text, destination):
        link = QPushButton(text + "  →")
        link.setObjectName("basicSettingsAdvancedLink")
        link.setCursor(Qt.CursorShape.PointingHandCursor)
        link.clicked.connect(lambda: self.dialog.select_destination(destination))
        layout.addWidget(link, alignment=Qt.AlignmentFlag.AlignLeft)

    def _build_dictation(self, layout):
        record = self._group(layout, "Record", "microphone-blue.svg")
        self._microphone(record)
        self._shortcut(record)
        transcribe = self._group(layout, "Transcribe", "stack-purple.svg")
        self.voice_combo = self._combo()
        self.voice_combo.setMinimumWidth(260)
        populate_backend_combo(self.voice_combo)
        self.voice_detail = self._row(transcribe, "Voice model", "", self.voice_combo)
        self.voice_combo.currentIndexChanged.connect(self._change_voice)
        self._toggle(
            transcribe,
            "Clean up my dictation",
            "Fix punctuation and formatting after recording.",
            CLEANUP,
            "transcript_cleanup_check",
            SettingsKey.TRANSCRIPT_CLEANUP_ENABLED,
        )
        self.cleanup_status = WrappedLabel("")
        self.cleanup_status.setObjectName("basicSettingsStatus")
        transcribe.addWidget(self.cleanup_status)
        self._advanced_link(transcribe, "Set up AI cleanup", CLEANUP)
        output = self._group(layout, "Output", "bolt-green.svg")
        paste = SettingsSwitch()
        self.paste_description = self._row(
            output,
            "Paste text automatically",
            "Insert the transcript into the active window.",
            paste,
        )
        self._bind(paste, GENERAL, "auto_paste_check", SettingsKey.AUTO_PASTE)
        self._toggle(
            output,
            "Show live transcript",
            "See words appear while you speak.",
            RECORDING,
            "streaming_enabled_check",
            SettingsKey.STREAMING_ENABLED,
        )
        if self.dialog._native_wayland:
            self.paste_description.setText(
                "Uses the desktop's paste shortcut on Hyprland; other Wayland desktops use manual paste."
            )
        if sys.platform == "darwin":
            self.accessibility_button = QPushButton("Set up auto-paste…")
            self.accessibility_button.setObjectName("basicSettingsAdvancedLink")
            self.accessibility_button.clicked.connect(self._setup_accessibility)
            output.addWidget(
                self.accessibility_button, alignment=Qt.AlignmentFlag.AlignLeft
            )
        self._advanced_link(layout, "More dictation options in Advanced", VOICE_MODEL)

    def _change_voice(self):
        self.dialog.models.choose_backend(self.voice_combo.currentData())
        self.refresh()

    def _setup_accessibility(self):
        self.dialog._open_accessibility_setup()
        self.refresh()

    def _build_meetings(self, layout):
        record = self._group(layout, "Record", "microphone-blue.svg")
        self._microphone(record)
        voice = self._group(layout, "Transcribe", "stack-purple.svg")
        self.meeting_voice = WrappedLabel("")
        self.meeting_voice.setObjectName("basicSettingsSummary")
        voice.addWidget(self.meeting_voice)
        self._advanced_link(voice, "Voice model, language, and speakers", MEETING_VOICE)
        after = self._group(layout, "After the meeting", "check-green.svg")
        self._toggle(
            after,
            "Clean up the transcript",
            "Improve readability after the meeting. Requires AI insights.",
            MEETING_AFTER,
            "meeting_end_polish_check",
            SettingsKey.MEETING_END_POLISH,
            resolve_meeting_end_polish,
        )
        self._toggle(
            after,
            "Write a final report",
            "Create a summary and action items. Requires AI insights.",
            MEETING_AFTER,
            "meeting_end_report_check",
            SettingsKey.MEETING_END_REPORT,
            resolve_meeting_end_report,
        )
        intelligence = self._group(layout, "AI insights", "stack-purple.svg")
        self.meeting_intelligence = WrappedLabel("")
        self.meeting_intelligence.setObjectName("basicSettingsSummary")
        intelligence.addWidget(self.meeting_intelligence)
        self._advanced_link(
            intelligence, "Set up meeting intelligence", MEETING_INTELLIGENCE
        )
        self._advanced_link(layout, "More meeting options in Advanced", MEETING_AFTER)

    def _build_app(self, layout):
        appearance = self._group(layout, "Appearance", "typography-blue.svg")
        for title, description, key, attribute, choices, resolver in (
            (
                "Theme",
                "Dark, light, or match your operating system.",
                SettingsKey.UI_THEME,
                "ui_theme_combo",
                [(UiTheme.LABELS[value], value) for value in UiTheme.ALL],
                resolve_ui_theme,
            ),
            (
                "Font size",
                "Text size in windows and dialogs.",
                SettingsKey.UI_FONT_SCALE,
                "ui_font_scale_combo",
                [(UiFontScale.LABELS[value], value) for value in UiFontScale.ALL],
                resolve_ui_font_scale,
            ),
        ):
            combo = self._combo()
            for label, value in choices:
                combo.addItem(label, value)
            self._row(appearance, title, description, combo)
            self._bind(combo, GENERAL, attribute, key, resolver)
        window = self._group(layout, "Window", "box-blue.svg")
        tray = self._toggle(
            window,
            "Keep running when I close the window",
            "Keep hotkeys available in the system tray.",
            GENERAL,
            "minimize_tray_check",
            SettingsKey.MINIMIZE_TRAY,
            lambda settings: (
                self.dialog._tray_available
                and setting_value(SettingsKey.MINIMIZE_TRAY, settings)
            ),
        )
        tray.setEnabled(self.dialog._tray_available)
        if not self.dialog._tray_available:
            tray.setToolTip("This desktop session does not provide a system tray.")
        updates = self._group(layout, "Updates", "refresh-blue.svg")
        self._toggle(
            updates,
            "Check for updates automatically",
            "Look for new releases in the background.",
            GENERAL,
            "update_check_check",
            SettingsKey.UPDATE_CHECK_ENABLED,
            resolve_update_check_enabled,
        )
        self.update_notify = self._toggle(
            updates,
            "Notify me about updates",
            "Let me know when a new version is available.",
            GENERAL,
            "update_notify_check",
            SettingsKey.UPDATE_NOTIFY_ENABLED,
            resolve_update_notify_enabled,
        )
        self._advanced_link(layout, "More app options in Advanced", GENERAL)

    def refresh(self):
        settings = self.dialog._settings_snapshot()
        microphone = self.controls.get(SettingsKey.AUDIO_INPUT_DEVICE)
        if microphone is not None:
            source = self.dialog.audio_device_combo
            with QSignalBlocker(microphone):
                microphone.clear()
                for index in range(source.count()):
                    microphone.addItem(source.itemText(index), source.itemData(index))
        for control, resolve in self._bindings:
            with QSignalBlocker(control):
                value = resolve(settings)
                if isinstance(control, SettingsSwitch):
                    control.setChecked(bool(value))
                else:
                    control.setCurrentIndex(max(0, control.findData(value)))
        if self.destination == BASIC_DICTATION:
            with QSignalBlocker(self.voice_combo):
                self.voice_combo.setCurrentIndex(
                    max(
                        0,
                        self.voice_combo.findData(
                            setting_value(SettingsKey.SELECTED_MODEL, settings)
                        ),
                    )
                )
            self.voice_detail.setText(
                self.dialog.models.voice_detail()
                if self.dialog.models.voice_is_remote()
                else "Audio is transcribed on this computer."
            )
            if sys.platform == "darwin":
                from services.hotkey_manager import is_accessibility_trusted

                trusted = is_accessibility_trusted()
                self.accessibility_button.setVisible(not trusted)
                self.paste_description.setText(
                    "Insert the transcript into the active window."
                    if trusted
                    else "Auto-paste needs Accessibility access. Use ⌘V to paste for now."
                )
            provider = resolve_transcript_cleanup_provider(settings)
            profile = get_profile(provider, settings)
            name = profile_display_name(provider, settings)
            if profile and profile.is_local:
                status = f"{name} · Transcript text stays on this computer."
            else:
                configured = profile and (
                    not profile.api_key_env or resolve_credential(profile.api_key_env)
                )
                status = f"{name} · Transcript text goes to this provider when cleanup is on."
                if not configured:
                    status += " API key needed."
            self.cleanup_status.setText(status)
            self.refresh_shortcut(settings)
        elif self.destination == BASIC_APP:
            self.update_notify.setEnabled(
                self.controls[SettingsKey.UPDATE_CHECK_ENABLED].isChecked()
            )
        else:
            self.meeting_voice.setText(
                self.dialog.models.meeting_voice_summary()
                + "\n"
                + self.dialog.models.meeting_voice_detail()
            )
            value, detail = self.dialog.models.meeting_intelligence_overview()
            self.meeting_intelligence.setText(
                value
                + ("\n" + detail if detail else "")
                + "\nEnable AI insights for each meeting when you start it."
            )

    def refresh_shortcut(self, settings=None):
        if self.shortcut is None:
            return
        if not self.shortcut._capturing:
            self.shortcut.set_hotkey(
                self.dialog.current_hotkeys.get("record_toggle", "")
            )
        mode = resolve_recording_trigger_mode(
            settings or self.dialog._settings_snapshot()
        )
        self.shortcut_description.setText(
            "Hold to record; release to stop and transcribe."
            if mode == "push_hold"
            else "Press to start or stop dictation."
        )

    def cancel_capture(self):
        if self.shortcut is not None:
            self.shortcut.cancel_capture()
