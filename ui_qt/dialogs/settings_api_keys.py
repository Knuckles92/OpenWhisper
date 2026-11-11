"""Settings → App → API keys: save, test and remove provider keys.

Keys go to the operating system's credential store, never to settings or
logs. Testing runs on a worker thread whose result comes back through a
session-lived relay, so the worker never holds or emits on the window.
"""

from __future__ import annotations

import itertools
import logging
import sys
import threading
from typing import Callable, Dict, Optional

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QMessageBox, QVBoxLayout, QWidget

from services.credentials import (
    MAX_API_KEY_LEN,
    CredentialSource,
    CredentialStoreError,
    credential_source,
    environment_shadowed,
    mask_key,
    resolve_credential,
    validate_api_key,
)
from services.credentials import store as credential_store
from services.text_llm import credential_label, list_profiles, verify_api_key
from services.typesafe import CREDENTIAL_ENV as TYPESAFE_CREDENTIAL_ENV
from services.typesafe import OPENROUTER_ROUTE as JEV_OPENROUTER_ROUTE
from services.typesafe import verify_key as typesafe_verify_key
from ui_qt.dialogs.settings_fields import settings_caption
from ui_qt.utils.icons import design_icon
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets import Button, DangerButton, ElidingComboBox, FieldTile, InfoTile, WrappedLabel
from ui_qt.widgets.buttons import compact_primary_button, fit_compact_button, neutral_button

logger = logging.getLogger(__name__)

#: The section's widgets the dialog also exposes by name, for deep links,
#: search reveals and tests (see settings_metadata.CONTROL_DESTINATIONS).
CONTROLS = (
    "api_key_combo",
    "api_key_credential_tile",
    "api_key_status_icon",
    "api_key_status",
    "api_key_uses_caption",
    "api_key_entry_tile",
    "api_key_store_caption",
    "api_key_edit",
    "api_key_show_button",
    "api_key_save_button",
    "api_key_test_button",
    "api_key_remove_button",
)


class _Relay(QObject):
    """Brings a key test's result to the Qt thread for whichever page exists then.

    It lives for the session, so the worker never holds or emits on a window.
    Qt drops a deleted page's connections.
    """

    api_key_verified = pyqtSignal(int, str, bool, str)


_relay: Optional[_Relay] = None
#: Request tokens, unique across windows because they share the relay.
_tokens = itertools.count(1)


def _thread_relay() -> _Relay:
    global _relay
    if _relay is None:
        _relay = _Relay()
    return _relay


def store_copy() -> str:
    """The entry tile's description of where saved keys go."""
    status = credential_store().status()
    if not status.available:
        # The error itself goes in the tooltip (store_detail).
        return (
            f"{status.backend_name} isn't available on this computer, so "
            "keys can't be saved from here. Set the variable in your "
            "environment or a .env file instead; OpenWhisper never falls "
            "back to an unprotected file."
        )
    copy = (
        f"Encrypted by {status.backend_name} for your account. "
        "Saved keys override environment variables and .env, and never "
        "enter settings or logs."
    )
    if sys.platform == "darwin":
        # "Allow" lasts one read, so macOS asks again every launch.
        copy += (
            " When macOS asks to use your Keychain, choose Always Allow so "
            "it doesn't ask every launch."
        )
    return copy


def store_detail() -> str:
    """The store's own error, for the caption's tooltip; "" when it works."""
    status = credential_store().status()
    if status.available or not status.reason:
        return ""
    return f"Details: {status.reason}"


def key_entries(settings: dict) -> list:
    """One row per distinct variable; profiles sharing a variable share it."""
    from services import speech_endpoints

    names: Dict[str, list] = {}
    owners = [(p.api_key_env, p.name) for p in list_profiles(settings) if p.api_key_env]
    for env_name, owner in owners + speech_endpoints.credential_owners(settings):
        if owner not in names.setdefault(env_name, []):
            names[env_name].append(owner)
    entries = [
        (env_name, f"{' / '.join(owners)} · {env_name}")
        for env_name, owners in names.items()
    ]
    # TypeSafe is a decision service rather than a text-model profile, so
    # it is not in ``list_profiles`` but still needs a home for its key.
    entries.append((TYPESAFE_CREDENTIAL_ENV, f"TypeSafe · {TYPESAFE_CREDENTIAL_ENV}"))
    return entries


def rail_value(settings: dict) -> str:
    """The rail's summary: how many of the listed keys are set."""
    entries = key_entries(settings)
    if not entries:
        return "None needed"
    available = sum(credential_source(name) != CredentialSource.NONE for name, _label in entries)
    return f"{available} of {len(entries)} set"


class ApiKeysSection(QObject):
    """Builds the API keys page's tiles and saves, tests and removes keys."""

    #: A line for the window's message bar.
    message = pyqtSignal(str)
    #: A key was saved or removed: the rail and the app should re-read keys.
    keys_changed = pyqtSignal()

    def __init__(self, parent=None, *, load_settings: Callable[[], dict],
                 is_loading: Callable[[], bool] = lambda: False):
        super().__init__(parent)
        self._load_settings = load_settings
        self._is_loading = is_loading
        self._token = 0
        self._testing = False
        self._source = CredentialSource.NONE
        _thread_relay().api_key_verified.connect(self._on_verified)

    @property
    def testing(self) -> bool:
        """A key test is running."""
        return self._testing

    # ---- construction ----

    def build(self, layout: QVBoxLayout, tile_group: Callable) -> None:
        """Add the page's tiles to ``layout``; ``tile_group`` lays out a group."""
        self.api_key_combo = ElidingComboBox()
        self.api_key_combo.setObjectName("apiKeyCredentialCombo")
        self.api_key_combo.setMinimumHeight(40)
        self.api_key_combo.currentIndexChanged.connect(self._on_credential_changed)
        self.api_key_credential_tile = FieldTile(
            "Credential",
            "Choose the provider whose key you want to save or test.",
            self.api_key_combo,
            design_icon("key-blue.svg"),
        )

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(8)
        self.api_key_status_icon = QLabel()
        self.api_key_status_icon.setObjectName("apiKeyStatusIcon")
        self.api_key_status_icon.setFixedSize(16, 16)
        self.api_key_status = WrappedLabel("")
        self.api_key_status.setObjectName("apiKeyStatus")
        status_row.addWidget(
            self.api_key_status_icon, alignment=Qt.AlignmentFlag.AlignTop
        )
        status_row.addWidget(self.api_key_status, stretch=1)
        self.api_key_credential_tile.add_body_layout(status_row)
        self.api_key_uses_caption = settings_caption("")
        self.api_key_credential_tile.add_body(self.api_key_uses_caption)
        tile_group(layout, "", [self.api_key_credential_tile], columns=1)

        self.api_key_entry_tile = InfoTile(
            "Add or replace a key",
            store_copy(),
            design_icon("plus-blue.svg"),
        )
        self.api_key_store_caption = self.api_key_entry_tile.description_label
        self.api_key_store_caption.setToolTip(store_detail())

        # Password echo also makes Qt refuse copy/cut from the field, and the
        # input-method hints keep IMEs and predictive text from retaining it.
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setObjectName("apiKeyInput")
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("Paste a key to save or test")
        self.api_key_edit.setMaxLength(MAX_API_KEY_LEN)
        self.api_key_edit.setInputMethodHints(
            Qt.InputMethodHint.ImhHiddenText
            | Qt.InputMethodHint.ImhSensitiveData
            | Qt.InputMethodHint.ImhNoAutoUppercase
            | Qt.InputMethodHint.ImhNoPredictiveText
        )
        self.api_key_edit.setMinimumHeight(40)
        # The key itself is set in a spaced monospace; the placeholder keeps
        # the normal field font (QSS can't style a placeholder on its own).
        self.api_key_edit.textChanged.connect(
            lambda text: set_style_property(self.api_key_edit, "hasText", bool(text))
        )
        self.api_key_edit.textChanged.connect(self.update_controls)
        self.api_key_edit.returnPressed.connect(self.save_key)
        self.api_key_show_button = neutral_button(Button("Show"))
        self.api_key_show_button.setObjectName("apiKeyShowButton")
        self.api_key_show_button.setCheckable(True)
        self.api_key_show_button.set_base_minimum_size(80, 40)
        self.api_key_show_button.setMinimumHeight(40)
        self.api_key_show_button.setMaximumHeight(40)
        self.api_key_show_button.toggled.connect(self._on_show_toggled)
        input_row = QWidget()
        input_row.setObjectName("modelManagerFieldGroup")
        input_layout = QHBoxLayout(input_row)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(8)
        input_layout.addWidget(self.api_key_edit, stretch=1)
        input_layout.addWidget(self.api_key_show_button)
        self.api_key_entry_tile.add_body(input_row)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(8)
        # A plain Button: PrimaryButton's own padding would make it taller than Test.
        self.api_key_save_button = Button("Save key")
        self.api_key_save_button.setObjectName("apiKeySaveButton")
        compact_primary_button(self.api_key_save_button)
        fit_compact_button(self.api_key_save_button, 120)
        self.api_key_save_button.clicked.connect(self.save_key)
        self.api_key_test_button = neutral_button(Button("Test"))
        self.api_key_test_button.setObjectName("apiKeyTestButton")
        fit_compact_button(self.api_key_test_button, 90)
        self.api_key_test_button.clicked.connect(self.test_key)
        self.api_key_remove_button = DangerButton("Remove saved key")
        self.api_key_remove_button.setProperty("buttonId", "apiKeyRemoveButton")
        fit_compact_button(self.api_key_remove_button, 160)
        self.api_key_remove_button.clicked.connect(self.remove_key)
        buttons.addWidget(self.api_key_save_button)
        buttons.addWidget(self.api_key_test_button)
        buttons.addStretch()
        buttons.addWidget(self.api_key_remove_button)
        self.api_key_entry_tile.add_body_layout(buttons)
        tile_group(layout, "", [self.api_key_entry_tile], columns=1)

    # ---- loading and focus ----

    def load(self, settings: dict) -> None:
        current = self.api_key_combo.currentData()
        self.api_key_combo.blockSignals(True)
        self.api_key_combo.clear()
        for env_name, label in key_entries(settings):
            self.api_key_combo.addItem(label, env_name)
        index = self.api_key_combo.findData(current) if current else -1
        self.api_key_combo.setCurrentIndex(max(0, index))
        self.api_key_combo.blockSignals(False)
        self._clear_input()
        self.api_key_store_caption.setText(store_copy())
        self.api_key_store_caption.setToolTip(store_detail())
        self._render_status()

    def focus(self, env_name: Optional[str] = None) -> None:
        """Select one credential, if given, and put the cursor in the key field."""
        if env_name:
            index = self.api_key_combo.findData(env_name)
            if index >= 0:
                self.api_key_combo.setCurrentIndex(index)
        self.api_key_edit.setFocus()

    def selected_name(self) -> str:
        return self.api_key_combo.currentData() or ""

    def profile_for(self, env_name: str):
        """The text endpoint, else the speech server, that sends ``env_name``."""
        from services import speech_endpoints

        settings = self._load_settings()
        speech = [
            speech_endpoints.configured_endpoint(endpoint_id, settings).connection
            for endpoint_id in speech_endpoints.ENDPOINT_IDS
        ]
        for profile in [*list_profiles(settings), *speech]:
            if profile.api_key_env == env_name:
                return profile
        return None

    def _label(self, env_name: str) -> str:
        if env_name == TYPESAFE_CREDENTIAL_ENV:
            return "TypeSafe API key"
        profile = self.profile_for(env_name)
        return credential_label(profile) if profile is not None else env_name

    def _clear_input(self) -> None:
        self.api_key_edit.clear()
        self.api_key_show_button.setChecked(False)

    # ---- rendering ----

    def _render_status(self) -> None:
        name = self.selected_name()
        status = credential_store().status()
        tone = "warning"
        if not name:
            self._source = CredentialSource.NONE
            text = "No endpoint needs an API key."
        else:
            source = credential_source(name)
            self._source = source
            suffix = mask_key(resolve_credential(name))
            if source == CredentialSource.STORED:
                text = f"Saved in {status.backend_name} · {suffix}"
                if environment_shadowed(name):
                    text += (
                        f". The {name} variable in your environment or .env "
                        "file is ignored while a key is saved here."
                    )
                tone = "success"
            elif source == CredentialSource.ENVIRONMENT:
                text = (
                    f"Using the {name} environment variable · {suffix}. "
                    "Save a key here to use it instead."
                )
                tone = "success"
            elif source == CredentialSource.DOTENV:
                text = (
                    f"Using the .env file · {suffix}. "
                    "Save a key here to use it instead."
                )
                tone = "success"
            else:
                text = "No key set."
        self.api_key_status.setText(text)
        icon = "check-green.svg" if tone == "success" else "info-warning.svg"
        self.api_key_status_icon.setPixmap(design_icon(icon).pixmap(16, 16))
        set_style_property(self.api_key_status, "tone", tone)
        self._render_uses(name)
        self.update_controls()

    def _render_uses(self, env_name: str) -> None:
        if env_name == "OPENAI_API_KEY":
            text = (
                "Used for API transcription with OpenAI, OpenAI transcript "
                "cleanup and meeting intelligence, and cloud speaker "
                "identification."
            )
        elif env_name == JEV_OPENROUTER_ROUTE.credential_env:
            text = (
                "Used for transcript cleanup and meeting intelligence through "
                "OpenRouter. It can also run fast judgments (Jev): choose "
                "OpenRouter under Meeting Mode → Fast judgments."
            )
        elif env_name == TYPESAFE_CREDENTIAL_ENV:
            text = (
                "Used for TypeSafe fast judgments: topic changes, spoken "
                "instructions, live highlights, the questions radar, citation "
                "checks, semantic search, and insight review. Enable them under "
                "Meeting Mode → Fast judgments, where an OpenRouter key can "
                "run the same model instead."
            )
        elif env_name:
            from services import speech_endpoints

            settings = self._load_settings()
            uses = []
            owners = [p.name for p in list_profiles(settings) if p.api_key_env == env_name]
            if owners:
                uses.append(f"to {' and '.join(owners)} for transcript cleanup and meeting intelligence")
            speech = [name for env, name in speech_endpoints.credential_owners(settings) if env == env_name]
            if speech:
                uses.append(f"to {' and '.join(speech)} for API transcription")
            text = f"Sent as the API key {', and '.join(uses)}."
        else:
            text = ""
        self.api_key_uses_caption.setText(text)
        self.api_key_uses_caption.setVisible(bool(text))

    def update_controls(self, *_args) -> None:
        name = self.selected_name()
        can_store = bool(name) and credential_store().status().available
        typed = bool(self.api_key_edit.text().strip())
        busy = self._testing
        self.api_key_edit.setEnabled(bool(name) and not busy)
        self.api_key_show_button.setEnabled(bool(name) and not busy)
        self.api_key_save_button.setEnabled(can_store and typed and not busy)
        self.api_key_test_button.setEnabled(
            bool(name)
            and not busy
            and (typed or self._source != CredentialSource.NONE)
        )
        self.api_key_remove_button.setEnabled(
            can_store
            and not busy
            and self._source == CredentialSource.STORED
        )

    # ---- actions ----

    def _on_credential_changed(self, _index: int = 0) -> None:
        if self._is_loading():
            return
        self._clear_input()
        self.message.emit("")
        self._render_status()

    def _on_show_toggled(self, checked: bool) -> None:
        self.api_key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        self.api_key_show_button.setText("Hide" if checked else "Show")

    def save_key(self) -> None:
        name = self.selected_name()
        if not name or not self.api_key_save_button.isEnabled():
            return
        try:
            key = validate_api_key(self.api_key_edit.text())
            credential_store().set(name, key)
        except (ValueError, CredentialStoreError) as exc:
            self.message.emit(str(exc))
            return
        self._clear_input()
        self._render_status()
        self.message.emit(f"{self._label(name)} saved.")
        self.keys_changed.emit()

    def remove_key(self) -> None:
        name = self.selected_name()
        if not name:
            return
        label = self._label(name)
        backend = credential_store().status().backend_name
        reply = QMessageBox.question(
            self.parent(),
            "Remove saved key",
            f"Remove the saved {label} from {backend}?\n\n"
            f"OpenWhisper will fall back to a {name} environment variable or "
            ".env entry if one exists; otherwise features that need this key "
            "become unavailable.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = credential_store().delete(name)
        except CredentialStoreError as exc:
            self.message.emit(str(exc))
            return
        self._clear_input()
        self._render_status()
        self.message.emit(
            f"{label} removed." if removed else f"No saved {label} to remove."
        )
        self.keys_changed.emit()

    def test_key(self) -> None:
        name = self.selected_name()
        if self._testing or not name:
            return
        # TypeSafe is not an OpenAI-compatible endpoint and so has no entry in
        # ``list_profiles``; it verifies through its own one-judgment probe.
        profile = self.profile_for(name)
        if profile is None and name != TYPESAFE_CREDENTIAL_ENV:
            return
        label = self._label(name)
        typed = self.api_key_edit.text()
        if typed.strip():
            try:
                key = validate_api_key(typed)
            except ValueError as exc:
                self.message.emit(str(exc))
                return
        else:
            key = resolve_credential(name)
            if not key:
                self.message.emit("Paste a key to test.")
                return
        self._testing = True
        self.update_controls()
        self.message.emit(f"Testing {label}…")
        self._token = token = next(_tokens)
        # Bound here: the worker must not reach back into the window.
        verify_typesafe = typesafe_verify_key
        verify = verify_api_key
        done = _thread_relay().api_key_verified

        def worker():
            if profile is None:
                ok, detail = verify_typesafe(key)
            else:
                ok, detail = verify(profile, key)
            done.emit(token, name, ok, detail)

        threading.Thread(target=worker, daemon=True, name="api-key-verify").start()

    def _on_verified(self, token: int, env_name: str, ok: bool, detail: str) -> None:
        if token != self._token:
            return
        self._testing = False
        self.update_controls()
        label = self._label(env_name)
        self.message.emit(
            f"{label} works: {detail}" if ok else f"{label} failed: {detail}"
        )
