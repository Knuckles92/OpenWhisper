"""Six settings categories composed from the existing destination builders.

The destination keys, controls and save handlers remain the source of truth.
Only their presentation changes; closed sections do not build their contents.
"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QScrollArea, QVBoxLayout, QWidget

from ui_qt.dialogs.settings_destinations import (
    ADVANCED,
    API_KEYS,
    APP_GENERAL,
    APPEARANCE,
    BACKUP,
    CLEANUP,
    CLEANUP_PROFILES,
    CLEANUP_RULES,
    COMMANDS,
    DICTIONARY,
    DOWNLOADS,
    GENERAL,
    HOTKEYS,
    MCP,
    MEETING_AFTER,
    MEETING_DASHBOARD,
    MEETING_FAST,
    MEETING_INTELLIGENCE,
    MEETING_VOICE,
    OVERVIEW,
    RECORDING,
    REMOTE_ENGINE,
    RUNTIME,
    SNIPPETS,
    STYLES,
    VOICE_MODEL,
)
from ui_qt.widgets.settings_section import SettingsSection

CATEGORIES = (
    ("dictation", "Dictation", "Record, refine and insert your words.", "microphone-blue.svg",
     ((RECORDING, "Recording"), (CLEANUP, "AI cleanup"), (GENERAL, "Output"))),
    ("meetings", "Meetings", "Capture meetings and choose what happens after them.", "microphone-blue.svg",
     ((MEETING_VOICE, "Voice & speakers"), (MEETING_INTELLIGENCE, "AI insights"),
      (MEETING_AFTER, "After the meeting"), (MEETING_FAST, "Fast judgments"),
      (MEETING_DASHBOARD, "Dashboard access"))),
    ("personalize", "Personalize", "Make dictation match your words and your work.", "wand-purple.svg",
     ((DICTIONARY, "Dictionary"), (SNIPPETS, "Snippets"), (STYLES, "Styles"),
      (CLEANUP_RULES, "Learned rules"), (CLEANUP_PROFILES, "Profiles"), (COMMANDS, "Commands"))),
    ("models", "Models", "Manage downloaded speech models and local runtimes.", "box-blue.svg",
     ((DOWNLOADS, "Downloads"), (RUNTIME, "Runtime"), (OVERVIEW, "Status"))),
    ("connections", "Connections", "Manage providers, paired computers and assistant access.", "server-blue.svg",
     ((API_KEYS, "API keys"), (REMOTE_ENGINE, "Remote engine"), (MCP, "AI assistant access"))),
    ("app", "App", "Appearance, shortcuts, updates and local backups.", "palette-purple.svg",
     ((APPEARANCE, "Appearance"), (HOTKEYS, "Hotkeys"), (APP_GENERAL, "Window & updates"),
      (BACKUP, "Backup & restore"), (ADVANCED, "Developer options"))),
)


class SettingsSections:
    def __init__(self, dialog):
        self.dialog = dialog
        self.areas = {}
        self.sections = {}
        self.category_for = {}
        self.attached = set()
        self._navigating = False
        for category, title, _subtitle, _icon, destinations in CATEGORIES:
            page = QWidget()
            page.setObjectName("settingsCategoryPage")
            column = QVBoxLayout(page)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(12)
            area = QScrollArea()
            area.setObjectName("settingsCategoryScroll")
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.Shape.NoFrame)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            area.setWidget(page)
            self.areas[category] = area
            dialog.stack.addWidget(area)
            for key, label in destinations:
                section = SettingsSection(label)
                section.expanded_changed.connect(
                    lambda expanded, key=key: self._toggle(key, expanded)
                )
                self.sections[key] = section
                self.category_for[key] = category
                column.addWidget(section)
                dialog._rail_groups[key] = title
            column.addStretch()
        # Speech selection is a detail of Recording, not another sidebar item.
        speech = SettingsSection("Speech engine")
        speech.expanded_changed.connect(lambda expanded: self._toggle(VOICE_MODEL, expanded))
        self.sections[VOICE_MODEL] = speech
        self.category_for[VOICE_MODEL] = "dictation"
        self.sections[RECORDING].body_layout.addWidget(speech)

    def _attach(self, key):
        if key in self.attached:
            return
        dialog = self.dialog
        page = dialog.ensure_page(key)
        original = dialog._page_scrolls.get(key)
        if original is not None:
            original.takeWidget()
            dialog.stack.removeWidget(original)
            original.deleteLater()
        else:
            dialog.stack.removeWidget(page)
        section = self.sections[key]
        section.body_layout.insertWidget(0, page)
        page.show()
        if key == DOWNLOADS:
            # The catalog retains its own list/detail scroll panes.
            page.setMinimumHeight(520)
        dialog._page_scrolls[key] = self.areas[self.category_for[key]]
        self.attached.add(key)

    def _toggle(self, key, expanded):
        if self._navigating:
            return
        if expanded:
            self.dialog.select_destination(key)
        else:
            self.dialog._cancel_hotkey_capture()
            self.dialog._cancel_page_captures()
            if key == CLEANUP and key in self.dialog._built_pages:
                self.dialog._persist_cleanup_prompt()
            if not self.dialog._save_page_drafts(ask=True):
                self.sections[key].set_expanded(True)

    def show(self, key):
        category = self.category_for[key]
        self._navigating = True
        try:
            self._attach(key)
            if key == VOICE_MODEL:
                self._attach(RECORDING)
                self.sections[RECORDING].set_expanded(True)
            self.sections[key].set_expanded(True)
            self.dialog.category_rail.select(category)
            self.dialog.stack.setCurrentWidget(self.areas[category])
        finally:
            self._navigating = False
        _category, title, subtitle, _icon, _items = next(c for c in CATEGORIES if c[0] == category)
        self.dialog.page_title.setText(title)
        self.dialog.page_subtitle.setText(subtitle)
        self.areas[category].ensureWidgetVisible(self.sections[key].header, 0, 24)

    def refresh_summaries(self):
        for key, section in self.sections.items():
            section.set_summary(self.dialog.rail.value(key))
        self.sections[APP_GENERAL].set_summary("Window behavior and update preferences")
