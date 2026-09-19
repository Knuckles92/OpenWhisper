"""A reusable library and editor for on-demand cleanup profiles."""

from uuid import uuid4

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from services.cleanup_profiles import (
    STARTER_PROFILES,
    CleanupProfile,
    delete_cleanup_profile,
    load_cleanup_profiles,
    save_cleanup_profile,
)
from services.hotkey_manager import format_hotkey_display
from services.settings import settings_manager
from ui_qt.widgets.buttons import Button, PrimaryButton
from ui_qt.widgets.profile_hotkey_input import ProfileHotkeyInput
from ui_qt.widgets.wrapped_label import WrappedLabel


class CleanupProfilesPanel(QWidget):
    profiles_changed = pyqtSignal()
    capture_changed = pyqtSignal(bool)
    model_requested = pyqtSignal()

    def __init__(self, parent=None, *, manager=None):
        super().__init__(parent)
        self.setObjectName("cleanupProfilesPanel")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.manager = manager or settings_manager
        self._profile_id = ""
        self._saved = None
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        intro = WrappedLabel(
            "Choose a profile on Quick Record, or give it a dedicated recording "
            "shortcut. Profiles always run AI cleanup using your configured text "
            "model. Standard dictation and uploads keep their usual settings."
        )
        intro.setObjectName("infoLabel")
        layout.addWidget(intro)

        root = layout
        columns = self._columns = QHBoxLayout()
        columns.setSpacing(18)
        root.addLayout(columns, 1)
        library = self._library = QWidget()
        library.setObjectName("cleanupProfileLibrary")
        library.setFixedWidth(180)
        library_layout = QVBoxLayout(library)
        library_layout.setContentsMargins(0, 0, 0, 0)
        library_layout.setSpacing(8)
        columns.addWidget(library)
        editor = self._editor = QWidget()
        editor.setObjectName("cleanupProfileEditor")
        layout = QVBoxLayout(editor)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        columns.addWidget(editor, 1)

        self.profile_list = QListWidget()
        self.profile_list.setObjectName("cleanupProfilesList")
        self.profile_list.setAccessibleName("Cleanup profiles")
        self.profile_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.profile_list.setMinimumHeight(95)
        self.profile_list.currentItemChanged.connect(self._selection_changed)
        library_layout.addWidget(self.profile_list, 1)
        actions = self._library_actions = QVBoxLayout()
        for text, callback in (
            ("New profile", self.new_profile),
            ("Duplicate", self.duplicate_profile),
            ("Delete", self.delete_profile),
        ):
            button = Button(text)
            button.set_base_minimum_size(80, 34)
            button.clicked.connect(callback)
            actions.addWidget(button)
            if text == "Delete":
                self.delete_button = button
            elif text == "Duplicate":
                self.duplicate_button = button
        library_layout.addLayout(actions)

        label = QLabel("Name")
        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(80)
        self.name_edit.setPlaceholderText("e.g. Support ticket, Email, Release notes")
        label.setBuddy(self.name_edit)
        layout.addWidget(label)
        layout.addWidget(self.name_edit)
        label = QLabel("Output instructions")
        self.instructions_edit = QTextEdit()
        self.instructions_edit.setAcceptRichText(False)
        self.instructions_edit.setMinimumHeight(135)
        self.instructions_edit.setPlaceholderText(
            "Describe the structure, tone, headings, and details you want in the finished text…"
        )
        label.setBuddy(self.instructions_edit)
        layout.addWidget(label)
        layout.addWidget(self.instructions_edit, 1)
        templates = QHBoxLayout()
        templates.addWidget(QLabel("Start from:"))
        for profile in STARTER_PROFILES:
            button = Button(profile.name)
            button.set_base_minimum_size(60, 30)
            button.clicked.connect(
                lambda _checked=False, p=profile: self.use_template(p)
            )
            templates.addWidget(button)
        templates.addStretch()
        layout.addLayout(templates)
        self.rules_check = QCheckBox("Also apply my learned rules")
        layout.addWidget(self.rules_check)

        shortcut_label = QLabel("Recording shortcut (optional)")
        self.hotkey_input = ProfileHotkeyInput()
        shortcut_label.setBuddy(self.hotkey_input)
        self.hotkey_input.capture_changed.connect(self.capture_changed)
        layout.addWidget(shortcut_label)
        shortcut = QHBoxLayout()
        shortcut.addWidget(self.hotkey_input, 1)
        clear = Button("Clear")
        clear.set_base_minimum_size(60, 34)
        clear.clicked.connect(lambda: self.hotkey_input.set_hotkey(""))
        shortcut.addWidget(clear)
        layout.addLayout(shortcut)
        hint = WrappedLabel(
            "Press once to start, again to stop. Profile shortcuts use toggle mode; "
            "the standard recording shortcut keeps its own mode."
        )
        hint.setObjectName("infoLabel")
        layout.addWidget(hint)
        self.message = WrappedLabel("")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setObjectName("infoLabel")
        root.addWidget(self.message)
        footer = QHBoxLayout()
        model = Button("Choose cleanup model…")
        model.clicked.connect(self.model_requested)
        footer.addWidget(model)
        footer.addStretch()
        self.save_button = PrimaryButton("Save profile")
        self.save_button.clicked.connect(self.save_profile)
        footer.addWidget(self.save_button)
        root.addLayout(footer)
        self.refresh()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not hasattr(self, "_editor"):
            return
        narrow = self.width() < 180 + self._columns.spacing() + self._editor.minimumSizeHint().width()
        self._columns.setDirection(
            QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight
        )
        self._library.setMinimumWidth(0 if narrow else 180)
        self._library.setMaximumWidth(16777215 if narrow else 180)
        self.profile_list.setMaximumHeight(160 if narrow else 16777215)
        self._library_actions.setDirection(
            QBoxLayout.Direction.LeftToRight if narrow else QBoxLayout.Direction.TopToBottom
        )

    def _draft(self) -> CleanupProfile:
        return CleanupProfile(
            self._profile_id,
            self.name_edit.text().strip(),
            self.instructions_edit.toPlainText().strip(),
            self.hotkey_input.hotkey,
            self.rules_check.isChecked(),
        )

    def _save_before_switch(self) -> bool:
        if self._saved is not None and self._draft() != self._saved:
            answer = QMessageBox.question(
                self,
                "Unsaved profile",
                "Save your changes to this profile?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if answer == QMessageBox.StandardButton.Save:
                return self.save_profile()
            return answer == QMessageBox.StandardButton.Discard
        return True

    def refresh(self, selected_id=None) -> None:
        # Raising Settings must not discard an in-progress draft.
        if (
            selected_id is None
            and self._saved is not None
            and self._draft() != self._saved
        ):
            return
        selected_id = self._profile_id if selected_id is None else selected_id
        self._loading = True
        self.profile_list.clear()
        profiles = load_cleanup_profiles(self.manager.load_all_settings())
        selected = None
        for profile in profiles:
            label = profile.name
            if profile.hotkey:
                label += f"\n{format_hotkey_display(profile.hotkey)}"
            item = QListWidgetItem(label)
            item.setToolTip(label)
            item.setData(Qt.ItemDataRole.UserRole, profile)
            self.profile_list.addItem(item)
            if profile.id == selected_id:
                selected = item
        if profiles:
            selected = selected or self.profile_list.item(0)
            self.profile_list.setCurrentItem(selected)
            self._show_profile(selected.data(Qt.ItemDataRole.UserRole))
        else:
            self._show_profile(CleanupProfile(uuid4().hex, "", ""))
        self._loading = False

    def _show_profile(self, profile: CleanupProfile) -> None:
        self.hotkey_input.cancel_capture()
        self._profile_id = profile.id
        self.name_edit.setText(profile.name)
        self.instructions_edit.setPlainText(profile.instructions)
        self.hotkey_input.set_hotkey(profile.hotkey)
        self.rules_check.setChecked(profile.use_learned_rules)
        self._saved = profile
        exists = any(
            p.id == profile.id
            for p in load_cleanup_profiles(self.manager.load_all_settings())
        )
        self.delete_button.setEnabled(exists)
        self.duplicate_button.setEnabled(bool(profile.name))
        self.message.clear()

    def _selection_changed(self, current, previous) -> None:
        if self._loading or current is None:
            return
        profile = current.data(Qt.ItemDataRole.UserRole)
        if not self._save_before_switch():
            self.profile_list.blockSignals(True)
            self.profile_list.setCurrentItem(previous)
            self.profile_list.blockSignals(False)
            return
        self.refresh(profile.id)

    def new_profile(self) -> None:
        if self._save_before_switch():
            self.profile_list.blockSignals(True)
            self.profile_list.setCurrentRow(-1)
            self.profile_list.blockSignals(False)
            self._show_profile(CleanupProfile(uuid4().hex, "", ""))
            self.name_edit.setFocus()

    def duplicate_profile(self) -> None:
        source = self._draft()
        if not self._save_before_switch():
            return
        names = {
            p.name.casefold()
            for p in load_cleanup_profiles(self.manager.load_all_settings())
        }
        name = f"{source.name[:65]} copy"
        suffix = 2
        while name.casefold() in names:
            name = f"{source.name[:65]} copy {suffix}"
            suffix += 1
        self.profile_list.blockSignals(True)
        self.profile_list.setCurrentRow(-1)
        self.profile_list.blockSignals(False)
        self._show_profile(
            CleanupProfile(
                uuid4().hex, name, source.instructions, "", source.use_learned_rules
            )
        )
        # A duplicate is a draft until saved.
        self._saved = CleanupProfile(self._profile_id, "", "")

    def use_template(self, profile: CleanupProfile) -> None:
        if self.instructions_edit.toPlainText().strip():
            answer = QMessageBox.question(
                self,
                "Use starter instructions?",
                "Replace the current output instructions?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if not self.name_edit.text().strip():
            self.name_edit.setText(profile.name)
        self.instructions_edit.setPlainText(profile.instructions)

    def save_profile(self) -> bool:
        self.hotkey_input.cancel_capture()
        profile = self._draft()
        try:
            self.manager.mutate_settings(
                lambda settings: save_cleanup_profile(settings, profile)
            )
        except Exception as exc:
            self.message.setText(str(exc))
            return False
        self._saved = profile
        self.refresh(profile.id)
        self.message.setText(f"Saved {profile.name}. Available on Quick Record.")
        self.profiles_changed.emit()
        return True

    def delete_profile(self) -> None:
        answer = QMessageBox.question(
            self,
            "Delete profile?",
            "Delete this profile and its recording shortcut?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.manager.mutate_settings(
                lambda settings: delete_cleanup_profile(settings, self._profile_id)
            )
        except Exception as exc:
            self.message.setText(str(exc))
            return
        self.refresh("")
        self.profiles_changed.emit()
