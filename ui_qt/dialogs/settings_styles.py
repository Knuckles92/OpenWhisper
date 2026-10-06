"""Settings → Personalize → Styles: the app you dictate into and the tone it gets.

Settings imports this module when it registers its pages, before the page is
built, so it imports only modules Settings has already loaded.
"""

from PyQt6.QtCore import QSignalBlocker, Qt
from PyQt6.QtWidgets import (
    QBoxLayout,
    QComboBox,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services import app_styles
from services.app_styles import AppCategory, Tone
from services.focus_context import catalog, recent_apps, text_reading_supported
from services.settings import (
    SettingsKey,
    resolve_app_context_enabled,
    resolve_app_context_read_text,
    resolve_app_styles_enabled,
    resolve_transcript_cleanup_provider,
    setting_value,
)
from services.text_llm import get_profile, profile_display_name
from ui_qt.dialogs.settings_destinations import CLEANUP, STYLES
from ui_qt.dialogs.settings_fields import settings_caption
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.icons import design_icon
from ui_qt.widgets.buttons import Button, fit_compact_button, neutral_button
from ui_qt.widgets.no_wheel import ElidingComboBox
from ui_qt.widgets.searchable_combo import SearchableComboBox
from ui_qt.widgets.segmented_bar import SegmentedBar
from ui_qt.widgets.setting_tile import InfoTile, SettingTile
from ui_qt.widgets.settings_switch import SettingsSwitch
from ui_qt.widgets.wrapped_label import WrappedLabel

TITLE = "Apps & styles"
SUBTITLE = "Know which app you're dictating into, and write in a tone that fits it."

CATEGORY_LABELS = {
    AppCategory.EMAIL: "Email",
    AppCategory.WORK: "Work messages",
    AppCategory.PERSONAL: "Personal messages",
    AppCategory.OTHER: "Other",
}
TONES = ((Tone.FORMAL, "Formal"), (Tone.CASUAL, "Casual"), (Tone.VERY_CASUAL, "Very casual"))

#: One message per category, as each tone writes it.
EXAMPLES = {
    AppCategory.EMAIL: {
        Tone.FORMAL: "Hi Sam, thanks for the notes. I'll send the draft by Friday.",
        Tone.CASUAL: "Hi Sam, thanks for the notes! I'll send the draft by Friday",
        Tone.VERY_CASUAL: "hi sam thanks for the notes, i'll send the draft by friday",
    },
    AppCategory.WORK: {
        Tone.FORMAL: "The build is fixed. I'll deploy it after lunch.",
        Tone.CASUAL: "Build's fixed, I'll deploy it after lunch",
        Tone.VERY_CASUAL: "build's fixed i'll deploy it after lunch",
    },
    AppCategory.PERSONAL: {
        Tone.FORMAL: "Are you free for dinner on Saturday? It's my treat.",
        Tone.CASUAL: "Are you free for dinner Saturday? It's my treat",
        Tone.VERY_CASUAL: "are you free for dinner saturday? it's my treat",
    },
    AppCategory.OTHER: {
        Tone.FORMAL: "Remember to water the plants before we leave.",
        Tone.CASUAL: "Remember to water the plants before we leave",
        Tone.VERY_CASUAL: "remember to water the plants before we leave",
    },
}

_ICONS = {
    AppCategory.EMAIL: "notes-blue.svg",
    AppCategory.WORK: "layout-grid-blue.svg",
    AppCategory.PERSONAL: "text-plus-green.svg",
    AppCategory.OTHER: "world-blue.svg",
}

_APP_CONTEXT_COPY = (
    "Notices the app you're dictating into, like Outlook or Slack, so your "
    "style can fit it. Checked on this computer; nothing is sent anywhere."
)
_READ_TEXT_COPY = (
    "Reads a little of the text around your cursor, so dictation continues "
    "your sentence and spells names the way they're already written."
)
_EXCLUDED_COPY = (
    "Apps whose text is never read. Password fields, terminals and "
    "OpenWhisper itself are always skipped."
)
_STYLES_COPY = (
    "Writes in a tone that fits where you are: polished for email, relaxed "
    "for chat. A cleanup profile you pick still comes first."
)
_OVERRIDES_COPY = "Put an app or website in a different style, like Notion in Work messages."
_BASIC_COPY = "Formal for email, relaxed for chat."

SEARCH_FIELDS = [
    ("app_context_tile", "Know which app I'm dictating into", _APP_CONTEXT_COPY),
    ("read_text_tile", "Read text near the cursor", _READ_TEXT_COPY),
    ("excluded_apps_tile", "Never read from", _EXCLUDED_COPY),
    ("app_styles_tile", "Match my style to the app", _STYLES_COPY),
    ("style_email_tile", "Email", "Tone for Outlook, Gmail and other mail: formal, casual, very casual"),
    ("style_work_tile", "Work messages", "Tone for Slack, Teams and other work chat"),
    ("style_personal_tile", "Personal messages", "Tone for WhatsApp, Messages and texts to friends"),
    ("style_other_tile", "Other", "Tone for every other app: documents, notes, code"),
    ("style_overrides_tile", "Choose apps for each style", _OVERRIDES_COPY),
]
CONTROL_ATTRS = tuple(attr for attr, _title, _copy in SEARCH_FIELDS) + (
    "app_context_check",
    "read_text_check",
    "app_styles_check",
    "styles_gate_tile",
)

_MAX_EXCLUDED = 100


def cleanup_on(settings: dict) -> bool:
    return bool(setting_value(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, settings))


def reads_text_near_cursor(settings: dict) -> bool:
    """Whether dictations read text near the cursor with these settings here."""
    return bool(
        resolve_app_context_enabled(settings)
        and resolve_app_context_read_text(settings)
        and text_reading_supported()
    )


def styles_blocker(settings: dict) -> str:
    """Why styles cannot apply now, as a short sentence, or ""."""
    if not cleanup_on(settings):
        return "Styles need AI cleanup, which is off."
    if not resolve_app_context_enabled(settings):
        return "Styles need Know which app I'm dictating into."
    return ""


def rail_value(settings: dict) -> str:
    if not resolve_app_context_enabled(settings) or not resolve_app_styles_enabled(settings):
        return "Off"
    if not cleanup_on(settings):
        return "Needs AI cleanup"
    relaxed = sum(tone != Tone.FORMAL for tone in app_styles.resolve_tones(settings).values())
    return f"On · {relaxed} casual" if relaxed else "On · All formal"


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def apps_summary(category: str, overrides) -> str:
    """A few of the apps a category covers, the user's own choices first."""
    moved = {match.casefold(): chosen for match, chosen in overrides}
    names = [match for match, chosen in overrides if chosen == category]
    taken = {name.casefold() for name in names}
    names += [
        name for name in catalog.FEATURED[category]
        if name.casefold() not in taken and moved.get(name.casefold(), category) == category
    ]
    shown = names[:4]
    if category == AppCategory.OTHER:
        return f"Everything else, like {_join(shown)}." if shown else "Everything else."
    return f"{', '.join(shown)} and more." if shown else "Apps you add below."


def privacy_note(settings: dict) -> str:
    """Where text near the cursor goes, in the user's own setup."""
    if not text_reading_supported():
        return "Works on Windows for now. Here, OpenWhisper only knows which app you're in."
    if not resolve_app_context_enabled(settings):
        return "Turn on Know which app I'm dictating into first."
    if not cleanup_on(settings):
        return ("AI cleanup is off, so it only fixes spacing and capitals on this "
                "computer. Never saved.")
    provider = resolve_transcript_cleanup_provider(settings)
    name = profile_display_name(provider, settings) or "your provider"
    profile = get_profile(provider, settings)
    if profile is not None and profile.is_local:
        return f"Used only by your AI cleanup provider ({name}), on this computer. Never saved."
    return (f"Sent only to your AI cleanup provider ({name}) with the dictation. "
            "Never saved, and never sent to a speech engine.")


def _choices() -> list[str]:
    """Names for the app pickers: this session's apps first, then the catalogue."""
    names, seen = [], set()
    for identity in recent_apps():
        kind = catalog.classify(identity)
        name = kind.name if kind is not None else identity.name
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            names.append(name)
    for name in catalog.known_names():
        if name.casefold() not in seen:
            seen.add(name.casefold())
            names.append(name)
    return names


def _excluded(settings: dict) -> list[str]:
    raw = settings.get(SettingsKey.APP_CONTEXT_EXCLUDED_APPS)
    values, seen = [], set()
    for value in raw if isinstance(raw, list) else ():
        name = value.strip() if isinstance(value, str) else ""
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            values.append(name)
    return values


def build(dialog, layout) -> None:
    dialog.apps_styles_page = _Page(dialog, layout)


def load(dialog, settings: dict) -> None:
    dialog.apps_styles_page.sync(settings)


def refresh(dialog) -> None:
    dialog.apps_styles_page.sync(dialog._settings_snapshot())


def basic_rows(page, group) -> None:
    switch = SettingsSwitch()
    detail = page._row(group, "Match tone to each app", _BASIC_COPY, switch)
    page._bind(switch, STYLES, "app_styles_check", SettingsKey.APP_STYLES_ENABLED,
               resolve_app_styles_enabled)

    def update(settings):
        reason = styles_blocker(settings)
        switch.setEnabled(not reason)
        switch.setToolTip(reason)
        detail.setText(reason or _BASIC_COPY)

    page.add_refresh_hook(update)


def _small_button(text: str) -> Button:
    button = neutral_button(Button(text))
    fit_compact_button(button)
    return button


def _picker(placeholder: str) -> SearchableComboBox:
    picker = SearchableComboBox()
    picker.setAccessibleName(placeholder)
    picker.lineEdit().setPlaceholderText(placeholder)
    # Sized for a name, not for the longest catalogue entry.
    picker.setMinimumContentsLength(10)
    picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    picker.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return picker


def _fill_picker(picker: SearchableComboBox, names: list[str]) -> None:
    if [picker.itemText(index) for index in range(picker.count())] == names:
        return
    text = picker.currentText()
    with QSignalBlocker(picker):
        picker.clear()
        picker.addItems(names)
        picker.setEditText(text)


class _FitRow(QWidget):
    """Controls on one line while they fit, else one under another."""

    def __init__(self, widgets: list):
        super().__init__()
        self.setObjectName("appStylesAddRow")
        self._widgets = widgets
        self._line = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._line.setContentsMargins(0, 0, 0, 0)
        self._line.setSpacing(8)
        for index, widget in enumerate(widgets):
            self._line.addWidget(widget, 1 if index == 0 else 0)

    def _needed(self) -> int:
        widths = [widget.minimumSizeHint().width() for widget in self._widgets]
        # The app picker wants room for a name, not just its arrow.
        widths[0] = max(widths[0], round(180 * current_ui_font_scale()))
        return sum(widths) + self._line.spacing() * (len(widths) - 1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        stacked = self.width() < self._needed()
        direction = QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight
        if self._line.direction() != direction:
            self._line.setDirection(direction)
            self._line.setAlignment(
                self._widgets[-1],
                Qt.AlignmentFlag.AlignLeft if stacked else Qt.AlignmentFlag.AlignVCenter,
            )

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(max(widget.minimumSizeHint().width() for widget in self._widgets))
        return hint


class _Rows(QWidget):
    """App names, each with a Remove button, or an empty-state line."""

    def __init__(self, empty_text: str, on_remove):
        super().__init__()
        self.setObjectName("appStylesRows")
        self._on_remove = on_remove
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._column.setSpacing(6)
        self._empty = WrappedLabel(empty_text)
        self._empty.setObjectName("infoLabel")
        self._column.addWidget(self._empty)
        self.rows: list[QWidget] = []
        self._shown: list = []

    def set_rows(self, entries: list[tuple[str, str]]) -> None:
        """Show ``(value, label)`` rows; Remove reports the value."""
        if entries == self._shown:
            return
        self._shown = list(entries)
        for row in self.rows:
            self._column.removeWidget(row)
            row.deleteLater()
        self.rows = []
        self._empty.setVisible(not entries)
        for value, label in entries:
            row = QWidget()
            row.setObjectName("appStylesRow")
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(8)
            text = WrappedLabel(label)
            text.setObjectName("appStylesRowLabel")
            line.addWidget(text, 1)
            remove = _small_button("Remove")
            remove.setAccessibleName(f"Remove {value}")
            remove.clicked.connect(lambda _checked=False, value=value: self._on_remove(value))
            line.addWidget(remove)
            self._column.addWidget(row)
            self.rows.append(row)


class _StyleCard:
    """One category's tone picker with a live example of that tone."""

    def __init__(self, category: str, on_tone):
        self.category = category
        self._on_tone = on_tone
        self.tile = InfoTile(CATEGORY_LABELS[category], "", design_icon(_ICONS[category]))
        self.tile.setProperty("tileId", f"style_{category}")
        self.bar = SegmentedBar(tuple((label, "") for _tone, label in TONES), compact=True)
        self.bar.setAccessibleName(f"{CATEGORY_LABELS[category]} tone")
        self.bar.activated.connect(self._picked)
        self.example = WrappedLabel("")
        self.example.setObjectName("appStyleExample")
        # Cards in a row share its height; the spare room goes below the
        # example instead of between the bar's buttons.
        body = QWidget()
        body.setObjectName("appStyleCardBody")
        column = QVBoxLayout(body)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        column.addWidget(self.bar)
        column.addWidget(self.example)
        column.addStretch(1)
        self.tile.add_body(body)

    def _picked(self, index: int) -> None:
        tone = TONES[index][0]
        self.example.setText(EXAMPLES[self.category][tone])
        self._on_tone(self.category, tone)

    def show(self, tone: str, summary: str) -> None:
        self.bar.setCurrentIndex([value for value, _label in TONES].index(tone))
        self.example.setText(EXAMPLES[self.category][tone])
        self.tile.set_description(summary)


class _Page:
    """The built page: its tiles, and how the settings show on them."""

    def __init__(self, dialog, layout):
        self.dialog = dialog
        self._syncing = False

        dialog.app_context_tile = SettingTile(
            "Know which app I'm dictating into", _APP_CONTEXT_COPY,
            design_icon("layout-grid-blue.svg"))
        dialog.app_context_check = dialog.app_context_tile.checkbox
        dialog.app_context_check.toggled.connect(
            lambda on: self._save_switch(SettingsKey.APP_CONTEXT_ENABLED, on))

        dialog.read_text_tile = SettingTile(
            "Read text near the cursor", _READ_TEXT_COPY, design_icon("typography-blue.svg"))
        dialog.read_text_check = dialog.read_text_tile.checkbox
        dialog.read_text_check.toggled.connect(
            lambda on: self._save_switch(SettingsKey.APP_CONTEXT_READ_TEXT, on))
        self.privacy = settings_caption("")
        dialog.read_text_tile.add_body(self.privacy)

        dialog.excluded_apps_tile = InfoTile(
            "Never read from", _EXCLUDED_COPY, design_icon("info-blue.svg"))
        self.excluded_rows = _Rows("No apps excluded.", self._remove_excluded)
        dialog.excluded_apps_tile.add_body(self.excluded_rows)
        self.excluded_picker = _picker("App to skip")
        self.excluded_add = _small_button("Add")
        self.excluded_add.setToolTip("Never read text from this app")
        self.excluded_add.clicked.connect(self._add_excluded)
        self.excluded_picker.lineEdit().returnPressed.connect(self._add_excluded)
        dialog.excluded_apps_tile.add_body(_FitRow([self.excluded_picker, self.excluded_add]))

        dialog._tile_group(layout, "App awareness", [dialog.app_context_tile, dialog.read_text_tile])
        dialog._tile_group(layout, "", [dialog.excluded_apps_tile], columns=1)

        dialog.styles_gate_tile = InfoTile("", "", design_icon("info-warning.svg"))
        dialog.styles_gate_tile.setProperty("kind", "notice")
        dialog.styles_gate_tile.setProperty("tileId", "stylesGate")
        self.gate_button = _small_button("Open AI cleanup")
        self.gate_button.clicked.connect(self._resolve_gate)
        # Under the text rather than beside it, so it fits a narrow window.
        gate_row = QHBoxLayout()
        gate_row.setContentsMargins(0, 0, 0, 0)
        gate_row.addWidget(self.gate_button)
        gate_row.addStretch(1)
        dialog.styles_gate_tile.add_body_layout(gate_row)

        dialog.app_styles_tile = SettingTile(
            "Match my style to the app", _STYLES_COPY, design_icon("palette-purple.svg"))
        dialog.app_styles_check = dialog.app_styles_tile.checkbox
        dialog.app_styles_check.toggled.connect(
            lambda on: self._save_switch(SettingsKey.APP_STYLES_ENABLED, on))
        dialog._tile_group(layout, "Styles", [dialog.styles_gate_tile], columns=1)
        dialog._tile_group(layout, "", [dialog.app_styles_tile], columns=1)

        self.cards = {category: _StyleCard(category, self._save_tone) for category in AppCategory.ALL}
        for category, card in self.cards.items():
            setattr(dialog, f"style_{category}_tile", card.tile)
        dialog._tile_group(layout, "", [card.tile for card in self.cards.values()])

        dialog.style_overrides_tile = InfoTile(
            "Choose apps for each style", _OVERRIDES_COPY, design_icon("wand-purple.svg"))
        self.override_rows = _Rows("No apps moved yet.", self._remove_override)
        dialog.style_overrides_tile.add_body(self.override_rows)
        self.override_picker = _picker("App or website")
        self.override_category = ElidingComboBox()
        self.override_category.setAccessibleName("Style for this app")
        for category in AppCategory.ALL:
            self.override_category.addItem(CATEGORY_LABELS[category], category)
        self.override_add = _small_button("Add")
        self.override_add.setToolTip("Use this style for the app")
        self.override_add.clicked.connect(self._add_override)
        self.override_picker.lineEdit().returnPressed.connect(self._add_override)
        dialog.style_overrides_tile.add_body(
            _FitRow([self.override_picker, self.override_category, self.override_add]))
        dialog._tile_group(layout, "", [dialog.style_overrides_tile], columns=1)

    def sync(self, settings: dict) -> None:
        dialog = self.dialog
        awareness = resolve_app_context_enabled(settings)
        read_text = resolve_app_context_read_text(settings)
        supported = text_reading_supported()
        self._syncing = True
        try:
            dialog.app_context_check.setChecked(awareness)
            dialog.read_text_check.setChecked(read_text)
            dialog.app_styles_check.setChecked(resolve_app_styles_enabled(settings))
        finally:
            self._syncing = False
        dialog.read_text_tile.setEnabled(awareness and supported)
        self.privacy.setText(privacy_note(settings))
        dialog.excluded_apps_tile.setEnabled(awareness and supported and read_text)
        self.excluded_rows.set_rows([(name, name) for name in _excluded(settings)])

        gate = "cleanup" if not cleanup_on(settings) else "" if awareness else "awareness"
        if gate == "cleanup":
            dialog.styles_gate_tile.title_label.setText("Styles need AI cleanup")
            dialog.styles_gate_tile.set_description(
                "Styles change how AI cleanup writes, so they only apply while it's on.")
            self.gate_button.setText("Open AI cleanup")
        elif gate == "awareness":
            dialog.styles_gate_tile.title_label.setText("Styles need app awareness")
            dialog.styles_gate_tile.set_description(
                "Turn on Know which app I'm dictating into so your style can fit each app.")
            self.gate_button.setText("Turn on")
        fit_compact_button(self.gate_button)
        dialog.styles_gate_tile.setVisible(bool(gate))
        dialog.app_styles_tile.setEnabled(not gate)
        active = not gate and dialog.app_styles_check.isChecked()
        tones = app_styles.resolve_tones(settings)
        overrides = app_styles.resolve_overrides(settings)
        for category, card in self.cards.items():
            card.show(tones[category], apps_summary(category, overrides))
            card.tile.setEnabled(active)
        dialog.style_overrides_tile.setEnabled(active)
        self.override_rows.set_rows([
            (match, f"{match}  →  {CATEGORY_LABELS[category]}")
            for match, category in overrides
        ])
        names = _choices()
        _fill_picker(self.excluded_picker, names)
        _fill_picker(self.override_picker, names)

    def _saved(self, key: str, value) -> None:
        if self.dialog._persist(key, value):
            self.dialog.notify_changed("styles")
        self.sync(self.dialog._settings_snapshot())

    def _save_switch(self, key: str, on: bool) -> None:
        if not self._syncing and not self.dialog._loading:
            self._saved(key, bool(on))

    def _save_tone(self, category: str, tone: str) -> None:
        settings = dict(self.dialog._settings_snapshot())
        app_styles.set_tone(settings, category, tone)
        self._saved(SettingsKey.APP_STYLE_TONES, settings[SettingsKey.APP_STYLE_TONES])

    def _add_excluded(self) -> None:
        name = self.excluded_picker.currentText().strip()[:catalog.MAX_MATCH_CHARS]
        if not name:
            self.excluded_picker.setFocus()
            return
        values = _excluded(self.dialog._settings_snapshot())
        if name.casefold() not in {value.casefold() for value in values}:
            values.append(name)
        self.excluded_picker.setEditText("")
        self._saved(SettingsKey.APP_CONTEXT_EXCLUDED_APPS, values[-_MAX_EXCLUDED:])

    def _remove_excluded(self, name: str) -> None:
        values = [value for value in _excluded(self.dialog._settings_snapshot())
                  if value.casefold() != name.casefold()]
        self._saved(SettingsKey.APP_CONTEXT_EXCLUDED_APPS, values)

    def _add_override(self) -> None:
        name = self.override_picker.currentText().strip()
        if not name:
            self.override_picker.setFocus()
            return
        settings = dict(self.dialog._settings_snapshot())
        app_styles.set_override(settings, name, self.override_category.currentData())
        self.override_picker.setEditText("")
        self._saved(SettingsKey.APP_STYLE_OVERRIDES, settings[SettingsKey.APP_STYLE_OVERRIDES])

    def _remove_override(self, match: str) -> None:
        settings = dict(self.dialog._settings_snapshot())
        app_styles.remove_override(settings, match)
        self._saved(SettingsKey.APP_STYLE_OVERRIDES, settings[SettingsKey.APP_STYLE_OVERRIDES])

    def _resolve_gate(self) -> None:
        if not cleanup_on(self.dialog._settings_snapshot()):
            self.dialog.select_destination(CLEANUP)
        else:
            self.dialog.app_context_check.setChecked(True)
