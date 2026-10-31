"""Settings → Home: the settings people reach for most, and where words go.

Home is the window's landing page. Its rows are projections of the
destinations' own controls, not another store: a change runs that page's
original handler, with its callbacks, confirmations and multi-key saves.
Model and cloud pages are built only when an edit needs their handler, and
the microphone list is read only once its menu opens, so opening Settings
never loads a speech runtime, fetches a provider's model catalog, or asks
the audio driver for devices.

Above the cards, the where strip says what stays on this computer and what
leaves it; the Storage card repeats the Downloads totals. Settings composes a
:class:`HomeSummary` for both, from the values its destinations report.
"""

import sys
from dataclasses import dataclass, field

from PyQt6.QtCore import QEvent, QRectF, QSignalBlocker, Qt
from PyQt6.QtGui import QPainter, QPainterPath
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

from services import cleanup_prompts
from services.cleanup_prompts import CleanupLevel
from services.audio_devices import display_name
from services.credentials import resolve_credential
from services.format_utils import format_size_bytes
from services.settings import (
    SettingsKey,
    UiFontScale,
    UiTheme,
    resolve_flow_features_intro_seen,
    resolve_meeting_end_polish,
    resolve_meeting_end_report,
    resolve_recording_hands_free_latch,
    resolve_recording_trigger_mode,
    resolve_transcript_cleanup_level,
    resolve_transcript_cleanup_provider,
    resolve_ui_font_scale,
    resolve_ui_theme,
    resolve_update_check_enabled,
    resolve_update_notify_enabled,
    resolve_update_restart_prompt_enabled,
    setting_value,
)
from services.text_llm import get_profile, profile_display_name
from ui_qt.dialogs import cleanup_levels, settings_microphones
from ui_qt.dialogs.settings_destinations import (
    APPEARANCE,
    BASIC_APP,
    BASIC_DICTATION,
    BASIC_MEETINGS,
    CLEANUP,
    DICTIONARY,
    DOWNLOADS,
    GENERAL,
    MEETING_AFTER,
    MEETING_INTELLIGENCE,
    MEETING_VOICE,
    RECORDING,
)
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.icons import design_icon
from ui_qt.utils.palette import token_color
from ui_qt.widgets.buttons import Button, fit_compact_button, neutral_button
from ui_qt.widgets.command_settings import INPUT_MIN_WIDTH
from ui_qt.widgets.hotkey_capture import mouse_shortcut_note
from ui_qt.widgets.no_wheel import ElidingComboBox
from ui_qt.widgets.profile_hotkey_input import ProfileHotkeyInput
from ui_qt.widgets.setting_tile import InfoTile
from ui_qt.widgets.settings_switch import SettingsSwitch
from ui_qt.widgets.speech_backend_picker import populate_backend_combo
from ui_qt.widgets.wrapped_label import WrappedLabel

#: Storage bar colours, in backend legend order.
_BACKEND_TOKENS = ("accent", "success", "purple", "warning-text-soft", "accent-cyan")
#: Narrowest card, at 100% text, that keeps a control beside its row's text.
_SIDE_BY_SIDE_WIDTH = 400
#: Narrowest card, at 100% text, that the page sets in two columns.
_COLUMN_WIDTH = 430
#: The old Basic tabs' keys, now the cards a deep link scrolls to.
SECTIONS = {BASIC_DICTATION: "dictation", BASIC_MEETINGS: "meetings", BASIC_APP: "app"}


@dataclass
class HomeSummary:
    """What the where strip and the Storage card show, composed by Settings."""

    local_items: list[str] = field(default_factory=list)
    cloud_items: list[str] = field(default_factory=list)
    storage_downloaded: int = 0
    storage_total: int = 0
    storage_bytes: int = 0
    storage_by_backend: dict[str, int] = field(default_factory=dict)
    storage_checking: bool = False
    components: list[tuple[str, bool]] = field(default_factory=list)
    footer: str = ""


class StorageBar(QWidget):
    """One rounded bar split by how much each backend's models use."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("homeStorageBar")
        self.setFixedHeight(8)
        self._segments: list[tuple[float, str]] = []

    def set_segments(self, sizes: list[int]) -> None:
        total = sum(sizes)
        self._segments = [
            (size / total, _BACKEND_TOKENS[index % len(_BACKEND_TOKENS)])
            for index, size in enumerate(sizes)
            if total and size
        ]
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        painter.setClipPath(clip)
        painter.fillRect(rect, token_color("slate-raised"))
        x = rect.left()
        for fraction, token in self._segments:
            width = rect.width() * fraction
            painter.fillRect(QRectF(x, rect.top(), width, rect.height()), token_color(token))
            x += width
        painter.end()


class _MicrophoneCombo(ElidingComboBox):
    """Shows the saved microphone, and reads the device list once opened."""

    def __init__(self, before_popup):
        super().__init__()
        self._before_popup = before_popup

    def showPopup(self) -> None:
        self._before_popup()
        super().showPopup()


class HomeCard(QFrame):
    """One group of Home rows under an icon and a title."""

    def __init__(self, title: str, icon: str, parent=None):
        super().__init__(parent)
        self.setObjectName("homeCard")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        self._rows: list[tuple[QBoxLayout, QWidget]] = []
        self._stacked = None
        self.rows = QVBoxLayout(self)
        self.rows.setContentsMargins(18, 13, 18, 8)
        self.rows.setSpacing(0)
        header = QHBoxLayout()
        header.setSpacing(8)
        glyph = QLabel()
        glyph.setObjectName("homeCardIcon")
        glyph.setPixmap(design_icon(icon).pixmap(18, 18))
        header.addWidget(glyph)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("homeCardTitle")
        header.addWidget(self.title_label)
        header.addStretch()
        self.rows.addLayout(header)
        self.rows.addSpacing(4)

    def add_row(self, row: QBoxLayout, control: QWidget) -> None:
        if self._rows:
            divider = QFrame()
            divider.setObjectName("homeDivider")
            divider.setFixedHeight(1)
            self.rows.addWidget(divider)
        self.rows.addLayout(row)
        self._rows.append((row, control))
        self._stacked = None
        self._reflow()

    def _reflow(self) -> None:
        stacked = self.width() < round(_SIDE_BY_SIDE_WIDTH * current_ui_font_scale())
        if stacked == self._stacked:
            return
        self._stacked = stacked
        for row, control in self._rows:
            expanding = control.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Expanding
            row.setDirection(
                QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight
            )
            row.setSpacing(8 if stacked else 20)
            row.setStretch(1, 3 if expanding and not stacked else 0)
            row.setAlignment(
                control,
                Qt.AlignmentFlag.AlignLeft if stacked and not expanding else Qt.AlignmentFlag.AlignVCenter,
            )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._stacked = None
            self._reflow()


class HomePage(QWidget):
    """Home's where strip, its cards of everyday settings, and its footer."""

    def __init__(self, dialog):
        super().__init__()
        self.setObjectName("homePage")
        self.dialog = dialog
        self.controls = {}
        # (row layout, control) for every card's rows, in order.
        self._rows = []
        self._bindings = []
        self._refresh_hooks = []
        self._two_columns = None
        self.shortcut = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.intro = None
        self._build_intro(layout)
        layout.addWidget(self._build_where_strip())

        self.cards = {
            "dictation": HomeCard("Dictation", "microphone-blue.svg"),
            "personalize": HomeCard("Personalize", "wand-purple.svg"),
            "meetings": HomeCard("Meetings", "users-amber.svg"),
            "app": HomeCard("App", "adjustments-slate.svg"),
            "storage": HomeCard("Storage", "download-blue.svg"),
        }
        self._build_dictation(self.cards["dictation"].rows)
        self._build_personalize(self.cards["personalize"].rows)
        self._build_meetings(self.cards["meetings"].rows)
        self._build_app(self.cards["app"].rows)
        self._build_storage(self.cards["storage"])

        columns = QHBoxLayout()
        self._column_row = columns
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(12)
        self._columns = (QVBoxLayout(), QVBoxLayout())
        for column in self._columns:
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(12)
            columns.addLayout(column, stretch=1)
        layout.addLayout(columns)
        self.footer_label = WrappedLabel("")
        self.footer_label.setObjectName("homeFooter")
        layout.addWidget(self.footer_label)
        self._reflow_columns()
        self.refresh()

    def _build_intro(self, layout):
        """The dismissible "New in OpenWhisper" note, until it is dismissed."""
        if resolve_flow_features_intro_seen(self.dialog._settings_snapshot()):
            return
        self.intro = InfoTile(
            "New in OpenWhisper",
            "Your own dictionary and snippets, styles that match each app, "
            "Command Mode for editing by voice, hands-free dictation, Stats, "
            "and a Scratchpad.",
            design_icon("wand-purple.svg"),
        )
        self.intro.setProperty("kind", "intro")
        self.intro.setProperty("tileId", "flowIntro")
        dismiss = neutral_button(Button("Got it"))
        dismiss.setToolTip("Hide this note")
        fit_compact_button(dismiss, 88)
        dismiss.clicked.connect(self._dismiss_intro)
        self.intro.add_trailing(dismiss)
        layout.addWidget(self.intro)

    def _dismiss_intro(self):
        if self.dialog._persist(SettingsKey.FLOW_FEATURES_INTRO_SEEN, True):
            self.intro.hide()

    def section(self, key: str) -> QWidget:
        """The card an old Basic tab's key now opens."""
        return self.cards[SECTIONS[key]]

    # ---- layout ----

    def _reflow_columns(self) -> None:
        scale = current_ui_font_scale()
        two = self.width() >= round((2 * _COLUMN_WIDTH + 12) * scale)
        self._where_row.setDirection(
            QBoxLayout.Direction.LeftToRight if two else QBoxLayout.Direction.TopToBottom
        )
        self._where_divider.setVisible(two)
        if two == self._two_columns:
            return
        self._two_columns = two
        order = (("dictation", "app"), ("personalize", "meetings", "storage")) if two else (
            ("dictation", "personalize", "meetings", "app", "storage"), ())
        for column in self._columns:
            while column.count():
                column.takeAt(0)
        for column, names in zip(self._columns, order):
            for name in names:
                column.addWidget(self.cards[name])
            column.addStretch()
        # One column takes the whole width.
        self._column_row.setStretch(1, 1 if two else 0)
        self._column_row.setSpacing(12 if two else 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow_columns()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(self, "cards"):
            self._two_columns = None
            self._reflow_columns()

    # ---- rows, for this page and the page modules' home_rows ----

    def add_row(self, group, title, description, control):
        """Add a titled row with its control; return the description label."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 7, 0, 7)
        row.setSpacing(20)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        name = WrappedLabel(title)
        name.setObjectName("homeRowTitle")
        detail = WrappedLabel(description)
        detail.setObjectName("homeRowDescription")
        copy.addWidget(name)
        copy.addWidget(detail)
        row.addLayout(copy, 1)
        row.addWidget(control, alignment=Qt.AlignmentFlag.AlignVCenter)
        control.setAccessibleName(title)
        group.parentWidget().add_row(row, control)
        self._rows.append((row, control))
        return detail

    def combo(self, *, expanding=False):
        combo = ElidingComboBox()
        self._style_combo(combo, expanding=expanding)
        return combo

    @staticmethod
    def _style_combo(combo, *, expanding=False):
        combo.setObjectName("homeCombo")
        combo.setMinimumHeight(36)
        combo.setMinimumWidth(150)
        if expanding:
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        else:
            combo.setMaximumWidth(240)

    def bind(self, control, destination, attribute, key, resolver=None):
        """Show a setting in ``control`` and save edits through a page's own control."""
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

    def toggle(self, group, title, description, destination, attribute, key, resolver=None):
        switch = SettingsSwitch()
        self.add_row(group, title, description, switch)
        self.bind(switch, destination, attribute, key, resolver)
        return switch

    def add_refresh_hook(self, hook):
        """Call ``hook(settings)`` whenever Home refreshes its rows."""
        self._refresh_hooks.append(hook)

    def _link(self, layout, text, destination):
        link = QPushButton(text + "  →")
        link.setObjectName("homeLink")
        link.setCursor(Qt.CursorShape.PointingHandCursor)
        link.clicked.connect(lambda: self.dialog.select_destination(destination))
        layout.addWidget(link, alignment=Qt.AlignmentFlag.AlignLeft)
        return link

    def _page_row(self, group, title, button_text, name, destination):
        """A row whose button opens a page; return the description label."""
        button = neutral_button(Button(button_text))
        fit_compact_button(button)
        button.clicked.connect(lambda: self.dialog.select_destination(destination))
        detail = self.add_row(group, title, "", button)
        # Named for what it does, which the row title alone doesn't say.
        button.setAccessibleName(name)
        return detail

    # ---- cards ----

    def _build_dictation(self, group):
        self._microphone(group)
        self._shortcut(group)
        self.voice_combo = self.combo()
        self.voice_combo.setMinimumWidth(200)
        populate_backend_combo(self.voice_combo)
        self.voice_detail = self.add_row(group, "Voice model", "", self.voice_combo)
        self.voice_combo.currentIndexChanged.connect(self._change_voice)
        self.toggle(
            group,
            "Clean up my dictation",
            "Fix punctuation and formatting after recording.",
            CLEANUP,
            "transcript_cleanup_check",
            SettingsKey.TRANSCRIPT_CLEANUP_ENABLED,
        )
        self._cleanup_level(group)
        self.cleanup_status = WrappedLabel("")
        self.cleanup_status.setObjectName("homeStatus")
        group.addWidget(self.cleanup_status)
        paste = SettingsSwitch()
        self.paste_description = self.add_row(
            group,
            "Paste text automatically",
            "Insert the transcript into the active window.",
            paste,
        )
        self.bind(paste, GENERAL, "auto_paste_check", SettingsKey.AUTO_PASTE)
        self.toggle(
            group,
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
            self.accessibility_button.setObjectName("homeLink")
            self.accessibility_button.clicked.connect(self._setup_accessibility)
            group.addWidget(self.accessibility_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self._link(group, "All dictation settings", RECORDING)

    def _microphone(self, group):
        # A choice here becomes the first microphone in the Recording order.
        # The device list is the Recording page's, read on a worker once
        # this menu first opens, and keeps unavailable saved devices.
        combo = _MicrophoneCombo(self._load_microphones)
        self._style_combo(combo, expanding=True)
        self.add_row(group, "Microphone", "Used for dictation and meetings.", combo)
        self.bind(
            combo,
            RECORDING,
            "audio_device_combo",
            SettingsKey.AUDIO_INPUT_PRIORITY,
            settings_microphones.preferred_token,
        )

    def _load_microphones(self):
        if RECORDING not in self.dialog._built_pages:
            self.dialog.ensure_page(RECORDING)
            self._refresh_microphone(self.dialog._settings_snapshot())

    def _shortcut(self, group):
        holder = QWidget()
        holder.setObjectName("homeShortcut")
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.shortcut = ProfileHotkeyInput()
        # As wide as the Command Mode shortcut below it, which fits its placeholder.
        self.shortcut.setMinimumWidth(round(INPUT_MIN_WIDTH * current_ui_font_scale()))
        self.shortcut.setMaximumWidth(210)
        self.shortcut.setAccessibleName("Recording shortcut")
        self.shortcut.capture_changed.connect(self.dialog._on_profile_capture)
        self.shortcut.captured.connect(
            lambda hotkey: self.dialog._on_local_hotkey_captured("record_toggle", hotkey)
        )
        row.addWidget(self.shortcut)
        change = QPushButton("Change")
        change.setObjectName("settingsChangeShortcut")
        change.clicked.connect(self._begin_shortcut_capture)
        row.addWidget(change)
        self.shortcut_description = self.add_row(group, "Recording shortcut", "", holder)

    def _begin_shortcut_capture(self):
        self.shortcut.setFocus()
        self.shortcut.begin_capture()

    def _cleanup_level(self, group):
        # Not a bind projection: choosing a level can remove a custom prompt
        # (after asking), which the AI cleanup page's handler owns.
        combo = self.combo()
        combo.setAccessibleName("Cleanup level")
        combo.activated.connect(self._choose_cleanup_level)
        self.cleanup_level_combo = combo
        self.controls[SettingsKey.TRANSCRIPT_CLEANUP_LEVEL] = combo
        self.cleanup_level_detail = self.add_row(group, "Cleanup level", "", combo)

    def _choose_cleanup_level(self, index):
        level = self.cleanup_level_combo.itemData(index)
        if level != cleanup_levels.CUSTOM:
            self.dialog.choose_cleanup_level(level)
        # A declined confirmation saves nothing; show the saved choice again.
        self._refresh_cleanup_level(self.dialog._settings_snapshot())

    def _change_voice(self):
        self.dialog.models.choose_backend(self.voice_combo.currentData())
        self.refresh()

    def _setup_accessibility(self):
        self.dialog._open_accessibility_setup()
        self.refresh()

    def _build_personalize(self, group):
        # Page modules own their Home rows; each adds them only if it has any.
        for module in self.dialog.page_modules():
            home_rows = getattr(module, "home_rows", None)
            if home_rows is not None:
                home_rows(self, group)
        self._link(group, "Dictionary, snippets, styles and commands", DICTIONARY)

    def _build_meetings(self, group):
        self.meeting_voice = self._page_row(
            group, "Voice & speakers", "Change", "Change meeting voice and speakers", MEETING_VOICE
        )
        self.toggle(
            group,
            "Clean up the transcript",
            "Improve readability after the meeting. Requires AI insights.",
            MEETING_AFTER,
            "meeting_end_polish_check",
            SettingsKey.MEETING_END_POLISH,
            resolve_meeting_end_polish,
        )
        self.toggle(
            group,
            "Write a final report",
            "Create a summary and action items. Requires AI insights.",
            MEETING_AFTER,
            "meeting_end_report_check",
            SettingsKey.MEETING_END_REPORT,
            resolve_meeting_end_report,
        )
        self.meeting_intelligence = self._page_row(
            group, "AI insights", "Set up", "Set up AI insights", MEETING_INTELLIGENCE
        )
        self._link(group, "All meeting settings", MEETING_VOICE)

    def _build_app(self, group):
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
            combo = self.combo()
            for label, value in choices:
                combo.addItem(label, value)
            self.add_row(group, title, description, combo)
            self.bind(combo, APPEARANCE, attribute, key, resolver)
        tray = self.toggle(
            group,
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
        self.toggle(
            group,
            "Check for updates automatically",
            "Ask GitHub for the latest release at launch and every 4 hours. "
            "Sends the app version, no IDs.",
            GENERAL,
            "update_check_check",
            SettingsKey.UPDATE_CHECK_ENABLED,
            resolve_update_check_enabled,
        )
        self.update_notify = self.toggle(
            group,
            "Notify me about updates",
            "Show an Update button when a new version is available.",
            GENERAL,
            "update_notify_check",
            SettingsKey.UPDATE_NOTIFY_ENABLED,
            resolve_update_notify_enabled,
        )
        self.toggle(
            group,
            "Ask me to restart after an update",
            "Offer a restart when the app was updated while it was running.",
            GENERAL,
            "update_restart_prompt_check",
            SettingsKey.UPDATE_RESTART_PROMPT_ENABLED,
            resolve_update_restart_prompt_enabled,
        )
        self._link(group, "Overlay, hotkeys and all app settings", APPEARANCE)

    def _build_storage(self, card):
        group = card.rows
        self.storage_value = QLabel("")
        self.storage_value.setObjectName("homeStorageValue")
        group.addWidget(self.storage_value)
        self.storage_detail = WrappedLabel("")
        self.storage_detail.setObjectName("homeRowDescription")
        group.addWidget(self.storage_detail)
        group.addSpacing(6)
        self.storage_bar = StorageBar()
        group.addWidget(self.storage_bar)
        group.addSpacing(4)
        self.storage_legend = WrappedLabel("")
        self.storage_legend.setObjectName("homeLegend")
        self.storage_legend.setTextFormat(Qt.TextFormat.RichText)
        group.addWidget(self.storage_legend)
        group.addSpacing(8)
        self.components_label = WrappedLabel("")
        self.components_label.setObjectName("homeComponents")
        self.components_label.setTextFormat(Qt.TextFormat.RichText)
        group.addWidget(self.components_label)
        self._link(group, "Downloads", DOWNLOADS)

    def _build_where_strip(self) -> QWidget:
        strip = QFrame()
        strip.setObjectName("homeWhere")
        row = QHBoxLayout(strip)
        self._where_row = row
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.local_title, self.local_list = self._where_half(row, "Stays on this computer", "ok")
        divider = QFrame()
        self._where_divider = divider
        divider.setObjectName("homeWhereDivider")
        divider.setFixedWidth(1)
        row.addWidget(divider)
        self.cloud_title, self.cloud_list = self._where_half(
            row, "Leaves this computer, only when used", "warn"
        )
        return strip

    @staticmethod
    def _where_half(row: QHBoxLayout, title: str, tone: str):
        half = QWidget()
        half.setObjectName("homeWhereHalf")
        layout = QHBoxLayout(half)
        layout.setContentsMargins(16, 11, 16, 11)
        layout.setSpacing(11)
        dot = QLabel()
        dot.setObjectName("homeDot")
        dot.setProperty("tone", tone)
        dot.setFixedSize(8, 8)
        dot_column = QVBoxLayout()
        dot_column.setContentsMargins(0, 5, 0, 0)
        dot_column.addWidget(dot)
        dot_column.addStretch()
        layout.addLayout(dot_column)
        copy = QVBoxLayout()
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(2)
        title_label = WrappedLabel(title)
        title_label.setObjectName("homeWhereTitle")
        copy.addWidget(title_label)
        items = WrappedLabel("")
        items.setObjectName("homeWhereList")
        copy.addWidget(items)
        copy.addStretch()
        layout.addLayout(copy, stretch=1)
        row.addWidget(half, stretch=1)
        return title_label, items

    # ---- state ----

    def refresh(self):
        settings = self.dialog._settings_snapshot()
        self._refresh_microphone(settings)
        for control, resolve in self._bindings:
            if control is self.controls.get(SettingsKey.AUDIO_INPUT_PRIORITY):
                continue
            with QSignalBlocker(control):
                value = resolve(settings)
                if isinstance(control, SettingsSwitch):
                    control.setChecked(bool(value))
                else:
                    control.setCurrentIndex(max(0, control.findData(value)))
        for hook in self._refresh_hooks:
            hook(settings)
        models = self.dialog.models
        with QSignalBlocker(self.voice_combo):
            self.voice_combo.setCurrentIndex(
                max(0, self.voice_combo.findData(setting_value(SettingsKey.SELECTED_MODEL, settings)))
            )
        self.voice_detail.setText(
            models.voice_detail() if models.voice_is_remote() else "Audio is transcribed on this computer."
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
        self.cleanup_status.setText(self._cleanup_status(settings))
        self._refresh_cleanup_level(settings)
        self.refresh_shortcut(settings)
        self.update_notify.setEnabled(self.controls[SettingsKey.UPDATE_CHECK_ENABLED].isChecked())
        self.meeting_voice.setText(f"{models.meeting_model_label()} · {models.meeting_voice_detail()}")
        value, detail = models.meeting_intelligence_overview()
        self.meeting_intelligence.setText(
            " · ".join(part for part in (value, detail) if part)
            + ". Turn it on for each meeting when you start it."
        )

    def _refresh_microphone(self, settings):
        combo = self.controls[SettingsKey.AUDIO_INPUT_PRIORITY]
        source = self.dialog.__dict__.get("audio_device_combo")
        with QSignalBlocker(combo):
            combo.clear()
            if source is None:
                # Named as the Recording page's list names it before devices arrive.
                priority = settings_microphones.saved_priority(settings)
                combo.addItem(
                    display_name(priority[0]["name"]) if priority else "System default",
                    settings_microphones.preferred_token(settings),
                )
                return
            for index in range(source.count()):
                combo.addItem(source.itemText(index), source.itemData(index))
            combo.setCurrentIndex(
                max(0, combo.findData(settings_microphones.preferred_token(settings)))
            )

    @staticmethod
    def _cleanup_status(settings) -> str:
        provider = resolve_transcript_cleanup_provider(settings)
        profile = get_profile(provider, settings)
        name = profile_display_name(provider, settings)
        from services.settings import TranscriptCleanupProvider
        if provider in TranscriptCleanupProvider.INSTALLED:
            return f"{name} · Uses your installed agent's sign-in. Transcript text is sent to its model."
        if profile and profile.is_local:
            return f"{name} · Transcript text stays on this computer."
        configured = profile and (not profile.api_key_env or resolve_credential(profile.api_key_env))
        status = f"{name} · Transcript text goes to this provider when cleanup is on."
        return status if configured else status + " API key needed."

    def _refresh_cleanup_level(self, settings):
        combo = self.cleanup_level_combo
        custom = bool(cleanup_prompts.custom_prompt(settings))
        choice = cleanup_levels.CUSTOM if custom else resolve_transcript_cleanup_level(settings)
        with QSignalBlocker(combo):
            combo.clear()
            for value, title, _detail in cleanup_levels.SEGMENTS:
                if value != cleanup_levels.CUSTOM or custom:
                    combo.addItem(title, value)
            combo.setCurrentIndex(max(0, combo.findData(choice)))
        combo.setEnabled(cleanup_prompts.resolve_level(settings) != CleanupLevel.NONE)
        self.cleanup_level_detail.setText(cleanup_levels.SHORT_NOTES[choice])

    def refresh_shortcut(self, settings=None):
        if self.shortcut is None:
            return
        hotkey = self.dialog.current_hotkeys.get("record_toggle", "")
        if not self.shortcut._capturing:
            self.shortcut.set_hotkey(hotkey)
        settings = settings or self.dialog._settings_snapshot()
        if resolve_recording_trigger_mode(settings) != "push_hold":
            description = "Press to start or stop dictation."
        elif resolve_recording_hands_free_latch(settings):
            description = "Hold to record, or double-tap to keep recording hands-free."
        else:
            description = "Hold to record; release to stop and transcribe."
        note = mouse_shortcut_note(hotkey)
        self.shortcut_description.setText(f"{description} {note}" if note else description)

    def update_summary(self, summary: HomeSummary) -> None:
        """Repaint the where strip and the Storage card from one snapshot."""
        self.local_list.setText(" · ".join(summary.local_items) or "Nothing")
        self.cloud_list.setText(" · ".join(summary.cloud_items) or "Nothing")
        if summary.storage_checking:
            self.storage_value.setText("Checking…")
            self.storage_detail.setText("Scanning downloaded models")
            self.storage_bar.set_segments([])
            self.storage_legend.setText("")
        else:
            self.storage_value.setText(
                f"{format_size_bytes(summary.storage_bytes)} of speech models"
                if summary.storage_bytes else "No speech models downloaded yet"
            )
            self.storage_detail.setText(
                f"{summary.storage_downloaded} of {summary.storage_total} downloaded"
            )
            ordered = sorted(summary.storage_by_backend.items(), key=lambda item: -item[1])
            self.storage_bar.set_segments([size for _label, size in ordered])
            parts = []
            for index, (label, size) in enumerate(ordered):
                color = token_color(_BACKEND_TOKENS[index % len(_BACKEND_TOKENS)]).name()
                parts.append(
                    f'<span style="color:{color}">■</span>&nbsp;{label} {format_size_bytes(size)}'
                )
            self.storage_legend.setText("&nbsp;&nbsp;&nbsp;".join(parts))
            self.storage_legend.setVisible(bool(parts))
        installed = sum(1 for _name, ok in summary.components if ok)
        ok_color = token_color("success").name()
        off_color = token_color("slate-text-4").name()
        dots = "&nbsp;&nbsp; ".join(
            f'<span style="color:{ok_color if ok else off_color}">●</span>&nbsp;{name}'
            for name, ok in summary.components
        )
        heading = (
            f"<b>Components · {installed} of {len(summary.components)} installed</b>"
            if summary.components else "<b>Components</b> · none available"
        )
        self.components_label.setText(heading + ("<br>" + dots if dots else ""))
        self.footer_label.setText(summary.footer)

    def cancel_capture(self):
        if self.shortcut is not None:
            self.shortcut.cancel_capture()
