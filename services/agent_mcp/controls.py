"""User-granted MCP mutations, separate from the read-only History API."""

import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, select, update
from sqlalchemy.pool import NullPool

from services.models import MeetingSession, TranscriptionHistory
from services.settings import (
    SETTING_DEFAULTS,
    MeetingLanguage,
    RecordingTriggerMode,
    SettingsKey,
    TranscriptCleanupReasoning,
    UiFontScale,
    UiTheme,
    resolve_ui_theme,
)

logger = logging.getLogger(__name__)


class ControlError(ValueError):
    """A public error that contains no credentials, paths, or source text."""


@dataclass(frozen=True)
class SettingControl:
    key: str
    label: str
    group: str
    kind: str = "boolean"
    choices: tuple = ()
    minimum: int | float | None = None
    maximum: int | float | None = None
    max_length: int = 0
    effect: str = "Applies to the next operation."
    resettable: bool = False

    def validate(self, value):
        if value is None and self.resettable:
            return None
        valid = False
        if self.kind == "boolean":
            valid = type(value) is bool
        elif self.kind == "integer":
            valid = type(value) is int
        elif self.kind == "number":
            valid = type(value) in (int, float) and math.isfinite(value)
        elif self.kind == "string":
            valid = isinstance(value, str) and len(value) <= self.max_length
        if not valid:
            raise ControlError(f"invalid_value: Invalid value for {self.key}.")
        if self.choices and value not in self.choices:
            raise ControlError(
                f"invalid_value: Choose a supported value for {self.key}."
            )
        if self.minimum is not None and value < self.minimum:
            raise ControlError(f"invalid_value: {self.key} is below its minimum.")
        if self.maximum is not None and value > self.maximum:
            raise ControlError(f"invalid_value: {self.key} is above its maximum.")
        return value

    def resolve(self, saved):
        if self.key == SettingsKey.UI_THEME:
            return resolve_ui_theme(saved)
        try:
            return self.validate(saved.get(self.key, SETTING_DEFAULTS[self.key]))
        except ControlError:
            return SETTING_DEFAULTS[self.key]

    def describe(self):
        result = {
            "key": self.key,
            "label": self.label,
            "group": self.group,
            "type": self.kind,
            "effect": self.effect,
        }
        if self.choices:
            result["choices"] = list(self.choices)
        if self.resettable:
            result["resettable"] = True
        for name in ("minimum", "maximum", "max_length"):
            value = getattr(self, name)
            if value is not None and value != 0:
                result[name] = value
        return result


LIVE = "Applies immediately in the desktop app."
SETTING_CONTROLS = (
    SettingControl(SettingsKey.AUTO_PASTE, "Paste after transcription", "Dictation"),
    SettingControl(SettingsKey.COPY_CLIPBOARD, "Copy to clipboard", "Dictation"),
    SettingControl(
        SettingsKey.RECORDING_TRIGGER_MODE,
        "Recording shortcut mode",
        "Dictation",
        "string",
        RecordingTriggerMode.ALL,
        max_length=20,
        effect=LIVE,
    ),
    SettingControl(
        SettingsKey.LOCAL_ASR_LANGUAGE,
        "Dictation language",
        "Dictation",
        "string",
        MeetingLanguage.ALL,
        max_length=10,
    ),
    SettingControl(
        SettingsKey.STREAMING_ENABLED, "Live preview", "Dictation", effect=LIVE
    ),
    SettingControl(
        SettingsKey.STREAMING_OVERLAY_FONT_SIZE,
        "Preview font size",
        "Appearance",
        "integer",
        minimum=10,
        maximum=48,
        effect=LIVE,
    ),
    SettingControl(
        SettingsKey.UI_THEME,
        "Color theme",
        "Appearance",
        "string",
        UiTheme.ALL,
        max_length=20,
        effect=LIVE,
        resettable=True,
    ),
    SettingControl(
        SettingsKey.UI_FONT_SCALE,
        "Interface text size",
        "Appearance",
        "integer",
        UiFontScale.ALL,
        effect=LIVE,
    ),
    SettingControl(
        SettingsKey.MINIMIZE_TRAY, "Minimize to tray on close", "Appearance"
    ),
    SettingControl(
        SettingsKey.TRANSCRIPT_CLEANUP_ENABLED,
        "Transcript cleanup",
        "Cleanup",
        effect=LIVE,
    ),
    SettingControl(
        SettingsKey.TRANSCRIPT_CLEANUP_PROMPT,
        "Cleanup instructions",
        "Cleanup",
        "string",
        max_length=16000,
    ),
    SettingControl(
        SettingsKey.TRANSCRIPT_CLEANUP_REASONING,
        "Cleanup reasoning effort",
        "Cleanup",
        "string",
        TranscriptCleanupReasoning.ALL,
        max_length=20,
    ),
    SettingControl(
        SettingsKey.MEETING_LANGUAGE,
        "Meeting language",
        "Meetings",
        "string",
        MeetingLanguage.ALL,
        max_length=10,
    ),
    SettingControl(
        SettingsKey.MEETING_REPORT_RIBBON, "Meeting report ribbon", "Meetings"
    ),
    SettingControl(SettingsKey.MEETING_REPORT_BRIEF, "Meeting brief view", "Meetings"),
    SettingControl(
        SettingsKey.MEETING_REPORT_SIGNAL, "Meeting signal view", "Meetings"
    ),
    SettingControl(
        SettingsKey.CONFIRM_HISTORY_ENTRY_DELETE, "Confirm history deletion", "History"
    ),
    SettingControl(
        SettingsKey.CONFIRM_MEETING_DELETE, "Confirm meeting deletion", "History"
    ),
    SettingControl(
        SettingsKey.UPDATE_CHECK_ENABLED, "Check for app updates", "Updates"
    ),
    SettingControl(
        SettingsKey.UPDATE_NOTIFY_ENABLED, "Notify about app updates", "Updates"
    ),
)
CONTROLS_BY_KEY = {control.key: control for control in SETTING_CONTROLS}


def writable_settings(settings):
    granted = settings.get(SettingsKey.MCP_WRITABLE_SETTINGS, {})
    if not isinstance(granted, dict):
        return set()
    return {key for key in CONTROLS_BY_KEY if granted.get(key) is True}


class AgentControls:
    def __init__(
        self, database, settings, *, enabled=None, on_change=None, meeting_renamer=None
    ):
        self.database = Path(database).expanduser().resolve()
        self.settings = settings
        self.enabled = enabled
        self.on_change = on_change
        self.meeting_renamer = meeting_renamer

    def _require(self, saved, permission):
        if self.enabled is not None and not self.enabled():
            raise ControlError("disabled: MCP access is turned off.")
        if self.settings is None or saved.get(permission) is not True:
            raise ControlError(
                "permission_denied: Enable this action in Settings > MCP."
            )

    def capabilities(self):
        saved = self.settings.load_all_settings() if self.settings is not None else {}
        access = saved.get(SettingsKey.MCP_SETTINGS_ACCESS) is True
        return {
            "retitle_transcriptions": saved.get(SettingsKey.MCP_RETITLE_TRANSCRIPTIONS)
            is True,
            "retitle_meetings": saved.get(SettingsKey.MCP_RETITLE_MEETINGS) is True,
            "settings_access": access,
            "writable_settings": sorted(writable_settings(saved)) if access else [],
        }

    def get_settings(self):
        saved = self.settings.load_all_settings() if self.settings is not None else {}
        self._require(saved, SettingsKey.MCP_SETTINGS_ACCESS)
        granted = writable_settings(saved)
        result = []
        for control in SETTING_CONTROLS:
            item = {
                **control.describe(),
                "value": control.resolve(saved),
                "writable": control.key in granted,
            }
            if control.resettable:
                item["inherited"] = control.key not in saved
            result.append(item)
        return {"settings": result}

    def update_settings(self, changes):
        if (
            not isinstance(changes, dict)
            or not changes
            or len(changes) > len(SETTING_CONTROLS)
        ):
            raise ControlError(
                "invalid_parameters: Supply a nonempty object of supported settings."
            )
        validated = {}
        for key, value in changes.items():
            control = CONTROLS_BY_KEY.get(key)
            if control is None:
                raise ControlError(
                    "unsupported_setting: This setting is not available to agents."
                )
            validated[key] = control.validate(value)
        if self.settings is None:
            raise ControlError("permission_denied: Settings access is not enabled.")

        def commit(saved):
            # Permission checking and saving share the user's settings lock.
            self._require(saved, SettingsKey.MCP_SETTINGS_ACCESS)
            if not set(validated).issubset(writable_settings(saved)):
                raise ControlError(
                    "permission_denied: Enable each setting in Settings > MCP."
                )
            previous = {
                key: CONTROLS_BY_KEY[key].resolve(saved) for key in validated
            }
            restore = {
                key: None
                if CONTROLS_BY_KEY[key].resettable and key not in saved
                else previous[key]
                for key in validated
            }
            for key, value in validated.items():
                if value is None:
                    saved.pop(key, None)
                else:
                    saved[key] = value
            if SettingsKey.STREAMING_ENABLED in validated:
                from services.settings import LEGACY_STREAMING_KEYS

                for key in LEGACY_STREAMING_KEYS:
                    saved.pop(key, None)
            updated = {
                key: CONTROLS_BY_KEY[key].resolve(saved) for key in validated
            }
            return {"updated": updated, "previous": previous, "restore": restore}

        result = self.settings.mutate_settings(commit)
        self._changed("settings", result["updated"])
        return result

    def _changed(self, kind, changes):
        if self.on_change is not None:
            try:
                self.on_change(kind, changes)
            except Exception:
                # The change is already committed. A closed window must not
                # make an agent retry a successful write or expose source data.
                logger.warning("Could not refresh the desktop after an MCP change.")

    def _engine(self):
        # mode=rw cannot create a missing database or run startup migrations.
        def connect():
            return sqlite3.connect(
                self.database.as_uri() + "?mode=rw",
                uri=True,
                check_same_thread=False,
                timeout=5,
            )

        return create_engine("sqlite://", creator=connect, poolclass=NullPool)

    def retitle(self, kind, record_id, title):
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in title)
        ):
            raise ControlError(
                "invalid_title: Supply a title of 1–200 characters without control characters."
            )
        title = title.strip()
        permission = (
            SettingsKey.MCP_RETITLE_TRANSCRIPTIONS
            if kind == "transcription"
            else SettingsKey.MCP_RETITLE_MEETINGS
        )
        saved = self.settings.load_all_settings() if self.settings is not None else {}
        self._require(saved, permission)
        table = (
            TranscriptionHistory if kind == "transcription" else MeetingSession
        ).__table__
        engine = self._engine()
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                columns = [table.c.title]
                if kind == "meeting":
                    columns.extend([table.c.status, table.c.state_json])
                row = (
                    conn.execute(
                        select(*columns).where(
                            table.c.id == record_id, table.c.origin_device_id.is_(None)
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise ControlError("not_found: Local record not found.")
                previous = row["title"]
                if kind == "meeting" and row["status"] not in {"ended", "failed"}:
                    raise ControlError(
                        "record_busy: Finish the meeting before changing its history title."
                    )
                if kind == "meeting":
                    try:
                        state = (
                            json.loads(row["state_json"]) if row["state_json"] else {}
                        )
                        if not isinstance(state, dict):
                            raise ValueError
                    except (ValueError, TypeError) as exc:
                        raise ControlError(
                            "snapshot_unavailable: Saved meeting state cannot be updated."
                        ) from exc
                if kind == "meeting" and self.meeting_renamer is not None:
                    # The owning dashboard must update its cached state as well.
                    # Release the database write lock before dispatching there.
                    conn.rollback()
                    self.meeting_renamer(record_id, title)
                else:
                    values = {"title": title}
                    if kind == "meeting":
                        state["title"] = title
                        values["state_json"] = json.dumps(state, ensure_ascii=False)
                    conn.execute(
                        update(table).where(table.c.id == record_id).values(**values)
                    )
        finally:
            engine.dispose()
        self._changed(kind, {"id": record_id, "title": title})
        return {"id": record_id, "title": title, "previous_title": previous}
