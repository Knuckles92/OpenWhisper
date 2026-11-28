"""Settings → Meeting Mode → Call detection: automatic call prompts and auto-stop."""

from __future__ import annotations

from typing import Callable, Dict

from PyQt6.QtCore import QObject, QSignalBlocker
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from meeting.call_detection.catalog import DEFAULT_CONFIGURABLE_PROVIDERS
from services.settings import (
    MeetingCallDetectionPolicy,
    SettingsKey,
    resolve_meeting_call_auto_stop,
    resolve_meeting_call_detection_apps,
    resolve_meeting_call_detection_policy,
)
from ui_qt.dialogs.settings_fields import settings_caption
from ui_qt.utils.icons import design_icon
from ui_qt.widgets import ElidingComboBox, FieldTile, SettingTile

CONTROLS = (
    "meeting_call_policy_tile",
    "meeting_call_policy_combo",
    "meeting_call_auto_stop_tile",
    "meeting_call_auto_stop_check",
    "meeting_call_apps_container",
)

_DEFAULT_CHOICES = (
    (MeetingCallDetectionPolicy.ASK, "Ask each time"),
    (MeetingCallDetectionPolicy.ALWAYS_RECORD, "Record automatically"),
    (MeetingCallDetectionPolicy.DONT_OFFER, "Don't offer"),
)

_APP_CHOICES = (
    ("default", "Use default"),
    (MeetingCallDetectionPolicy.ASK, "Ask each time"),
    (MeetingCallDetectionPolicy.ALWAYS_RECORD, "Record automatically"),
    (MeetingCallDetectionPolicy.DONT_OFFER, "Don't offer"),
)


def rail_value(settings: dict) -> str:
    policy = resolve_meeting_call_detection_policy(settings)
    if policy == MeetingCallDetectionPolicy.ALWAYS_RECORD:
        return "Record automatically"
    if policy == MeetingCallDetectionPolicy.DONT_OFFER:
        return "Off"
    return "Ask each time"


class MeetingCallsSection(QObject):
    """Builds the Call detection page under Settings → Meeting Mode."""

    def __init__(
        self,
        parent=None,
        *,
        persist: Callable[[str, object], object],
        bindings,
        is_loading: Callable[[], bool],
        ensure_built: Callable[[], object] = lambda: None,
    ) -> None:
        super().__init__(parent)
        self._persist = persist
        self._bindings = bindings
        self._is_loading = is_loading
        self._ensure_built = ensure_built
        self._app_combos: Dict[str, ElidingComboBox] = {}
        self._app_labels: Dict[str, str] = {}
        self._apps_snapshot: Dict[str, dict] = {}
        self._apps_layout: QVBoxLayout | None = None

    def __getattr__(self, name):
        if name in CONTROLS:
            ensure = self.__dict__.get("_ensure_built")
            if ensure is not None:
                ensure()
                if name in self.__dict__:
                    return self.__dict__[name]
        raise AttributeError(name)

    def build(self, layout: QVBoxLayout, tile_group: Callable) -> None:
        self.meeting_call_policy_combo = ElidingComboBox()
        self.meeting_call_policy_combo.setObjectName("meetingCallPolicyCombo")
        for value, label in _DEFAULT_CHOICES:
            self.meeting_call_policy_combo.addItem(label, value)
        self.meeting_call_policy_combo.currentIndexChanged.connect(
            self._on_default_policy_changed
        )

        self.meeting_call_policy_tile = FieldTile(
            "When a call is detected",
            "Detects when a call app or browser tab starts using your microphone.",
            self.meeting_call_policy_combo,
            icon=design_icon("microphone-blue.svg"),
        )

        self.meeting_call_auto_stop_tile = SettingTile(
            "Stop recording when the call ends",
            "Starts a 30-second countdown once the call app stops using both the "
            "microphone and audio playback. Muting keeps recording.",
            design_icon("check-green.svg"),
        )
        self.meeting_call_auto_stop_check = self.meeting_call_auto_stop_tile.checkbox
        self.meeting_call_auto_stop_check.setObjectName("meetingCallAutoStopCheck")
        self._bindings.checkbox(
            self.meeting_call_auto_stop_check,
            SettingsKey.MEETING_CALL_AUTO_STOP,
            resolve_meeting_call_auto_stop,
        )

        tile_group(
            layout,
            "Automatic call detection",
            [self.meeting_call_policy_tile, self.meeting_call_auto_stop_tile],
        )

        self.meeting_call_apps_container = QWidget()
        self._apps_layout = QVBoxLayout(self.meeting_call_apps_container)
        self._apps_layout.setContentsMargins(0, 0, 0, 0)
        self._apps_layout.setSpacing(6)

        apps_hint = settings_caption(
            "Override the default behavior for individual call apps or websites. "
            "Apps detected on this computer are added here automatically."
        )
        tile_group(
            layout,
            "Apps & call sites",
            [apps_hint, self.meeting_call_apps_container],
        )
        self._rebuild_app_rows({})

    def load(self, settings: dict) -> None:
        policy = resolve_meeting_call_detection_policy(settings)
        with QSignalBlocker(self.meeting_call_policy_combo):
            idx = self.meeting_call_policy_combo.findData(policy)
            if idx >= 0:
                self.meeting_call_policy_combo.setCurrentIndex(idx)
        apps = resolve_meeting_call_detection_apps(settings)
        self._rebuild_app_rows(apps if isinstance(apps, dict) else {})

    def _rebuild_app_rows(self, saved_apps: dict) -> None:
        if self._apps_layout is None:
            return
        while self._apps_layout.count():
            item = self._apps_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._app_combos.clear()
        self._app_labels.clear()
        self._apps_snapshot = {}

        # Merge known default providers with any saved/seen providers.
        ordered: list[tuple[str, str, str]] = []
        seen_keys: set[str] = set()

        for key, default_label in DEFAULT_CONFIGURABLE_PROVIDERS:
            seen_keys.add(key)
            entry = saved_apps.get(key)
            policy = self._extract_policy(entry)
            label = self._extract_label(entry, default_label)
            ordered.append((key, label, policy))
            if key in saved_apps:
                self._apps_snapshot[key] = {"label": label, "policy": policy}

        for key in sorted(saved_apps):
            if key in seen_keys:
                continue
            entry = saved_apps[key]
            policy = self._extract_policy(entry)
            label = self._extract_label(entry, key)
            ordered.append((key, label, policy))
            self._apps_snapshot[key] = {"label": label, "policy": policy}

        for key, label, policy in ordered:
            row_widget = QWidget()
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(4, 2, 4, 2)
            row.setSpacing(12)

            name_label = QLabel(label)
            name_label.setToolTip(key)
            row.addWidget(name_label, 1)

            combo = ElidingComboBox()
            for val, text in _APP_CHOICES:
                combo.addItem(text, val)
            idx = combo.findData(policy)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.currentIndexChanged.connect(
                lambda _i, k=key, c=combo: self._on_app_policy_changed(k, c)
            )
            row.addWidget(combo, 0)

            self._apps_layout.addWidget(row_widget)
            self._app_combos[key] = combo
            self._app_labels[key] = label

    @staticmethod
    def _extract_policy(entry: object) -> str:
        raw = entry.get("policy") if isinstance(entry, dict) else entry
        text = str(raw or "").strip().lower()
        return text if text in MeetingCallDetectionPolicy.ALL else "default"

    @staticmethod
    def _extract_label(entry: object, fallback: str) -> str:
        if isinstance(entry, dict):
            label = str(entry.get("label") or "").strip()
            if label:
                return label
        return fallback

    def _on_default_policy_changed(self, _index: int) -> None:
        if self._is_loading():
            return
        value = self.meeting_call_policy_combo.currentData()
        if value in MeetingCallDetectionPolicy.ALL:
            self._persist(SettingsKey.MEETING_CALL_DETECTION_POLICY, value)

    def _on_app_policy_changed(self, key: str, combo: ElidingComboBox) -> None:
        if self._is_loading():
            return
        policy = str(combo.currentData() or "default")
        label = self._app_labels.get(key, key)
        updated = dict(self._apps_snapshot)
        if policy == "default" and key in {k for k, _ in DEFAULT_CONFIGURABLE_PROVIDERS}:
            updated.pop(key, None)
        else:
            updated[key] = {"label": label, "policy": policy}
        self._apps_snapshot = updated
        self._persist(SettingsKey.MEETING_CALL_DETECTION_APPS, updated)
