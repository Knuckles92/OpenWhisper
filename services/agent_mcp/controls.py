"""User-granted MCP mutations, separate from the read-only History API."""

import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, delete, func, insert, select, update
from sqlalchemy.pool import NullPool

from services import annotations as record_annotations
from services.automation import permissions
from services.models import MeetingSegment, MeetingSession, RecordAnnotation, TranscriptionHistory
from services.titles import normalize_title
from services.settings import (
    SETTING_DEFAULTS,
    WALKTHROUGH_CAPTURE_DELAYS,
    MeetingLanguage,
    RecordingOverlayLook,
    RecordingTriggerMode,
    SettingsKey,
    SnapRedactStyle,
    StreamingOverlayLayout,
    TranscriptCleanupLevel,
    TranscriptCleanupReasoning,
    UiFontScale,
    UiTheme,
    WalkthroughCopyFormat,
    WalkthroughPageSize,
    WalkthroughSnapMode,
    resolve_mcp_events_enabled,
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
        result: dict[str, Any] = {
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
    SettingControl(
        SettingsKey.RESTORE_CLIPBOARD, "Restore clipboard after paste", "Dictation"
    ),
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
        SettingsKey.RECORDING_HANDS_FREE_LATCH,
        "Hands-free latch on quick double-press",
        "Dictation",
    ),
    SettingControl(
        SettingsKey.VOICE_ROUTING_ENABLED,
        "Route by voice",
        "Dictation",
    ),
    SettingControl(
        SettingsKey.VOICE_ROUTING_ON_DICTATION,
        "Route normal dictations by spoken action name",
        "Dictation",
    ),
    SettingControl(
        SettingsKey.COMMAND_MODE_INSERT_WITHOUT_SELECTION,
        "Command Mode writes at cursor without selection",
        "Dictation",
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
        SettingsKey.STREAMING_OVERLAY_LAYOUT,
        "Preview layout",
        "Appearance",
        "string",
        StreamingOverlayLayout.ALL,
        max_length=10,
        effect=LIVE,
    ),
    SettingControl(
        SettingsKey.RECORDING_OVERLAY_LOOK,
        "Recording overlay look",
        "Appearance",
        "string",
        RecordingOverlayLook.ALL,
        max_length=10,
        effect=LIVE,
    ),
    SettingControl(
        SettingsKey.RECORDING_OVERLAY_DOT, "Recording overlay live dot", "Appearance", effect=LIVE
    ),
    SettingControl(
        SettingsKey.RECORDING_OVERLAY_TEXT, "Recording overlay label", "Appearance", effect=LIVE
    ),
    SettingControl(
        SettingsKey.RECORDING_OVERLAY_CLOCK, "Recording overlay clock", "Appearance", effect=LIVE
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
        SettingsKey.COMPACT_MODE,
        "Compact window mode",
        "Appearance",
        effect=LIVE,
    ),
    SettingControl(
        SettingsKey.SCRATCHPAD_ALWAYS_ON_TOP,
        "Keep Scratchpad always on top",
        "Appearance",
    ),
    SettingControl(
        SettingsKey.MINIMIZE_TRAY, "Minimize to tray on close", "Appearance"
    ),
    SettingControl(
        SettingsKey.APP_CONTEXT_ENABLED,
        "Detect focused app for dictation context",
        "Personalization",
    ),
    SettingControl(
        SettingsKey.APP_STYLES_ENABLED,
        "Adjust writing tone per app category",
        "Personalization",
    ),
    SettingControl(
        SettingsKey.DICTIONARY_LEARN_ENABLED,
        "Learn corrections into personal dictionary",
        "Personalization",
    ),
    SettingControl(
        SettingsKey.DICTIONARY_STEER_RECOGNITION,
        "Send dictionary terms as speech-recognition hints",
        "Personalization",
    ),
    SettingControl(
        SettingsKey.SNIPPETS_ENABLED,
        "Expand spoken snippets",
        "Personalization",
    ),
    SettingControl(
        SettingsKey.TRANSCRIPT_CLEANUP_ENABLED,
        "Transcript cleanup",
        "Cleanup",
        effect=LIVE,
    ),
    SettingControl(
        SettingsKey.TRANSCRIPT_CLEANUP_LEVEL,
        "Cleanup level",
        "Cleanup",
        "string",
        TranscriptCleanupLevel.ALL,
        max_length=10,
        effect=LIVE,
    ),
    # The custom cleanup prompt is left out on purpose: it is free-form
    # instructions for the model that shapes what agent actions send, so an
    # agent that could write it could steer words sent to the user's coding
    # agents while they still read as dictated.
    SettingControl(
        SettingsKey.TRANSCRIPT_CLEANUP_REASONING,
        "Cleanup reasoning effort",
        "Cleanup",
        "string",
        TranscriptCleanupReasoning.ALL,
        max_length=20,
    ),
    SettingControl(
        SettingsKey.WALKTHROUGH_AUTO_MODE,
        "Walkthrough Auto Mode (capture on click)",
        "Walkthroughs",
    ),
    SettingControl(
        SettingsKey.WALKTHROUGH_CAPTURE_DELAY,
        "Walkthrough Auto Mode capture delay (seconds)",
        "Walkthroughs",
        "integer",
        WALKTHROUGH_CAPTURE_DELAYS,
    ),
    SettingControl(
        SettingsKey.WALKTHROUGH_SNAP_MODE,
        "Walkthrough screenshot capture mode",
        "Walkthroughs",
        "string",
        WalkthroughSnapMode.ALL,
        max_length=20,
    ),
    SettingControl(
        SettingsKey.WALKTHROUGH_WRITE_UP,
        "AI write-up after walkthrough",
        "Walkthroughs",
    ),
    SettingControl(
        SettingsKey.WALKTHROUGH_COPY_FORMAT,
        "Walkthrough copy format on finish",
        "Walkthroughs",
        "string",
        WalkthroughCopyFormat.ALL,
        max_length=20,
    ),
    SettingControl(
        SettingsKey.WALKTHROUGH_MINIMIZE_TO_TRAY,
        "Minimize window to tray on walkthrough start",
        "Walkthroughs",
    ),
    SettingControl(
        SettingsKey.WALKTHROUGH_TITLE,
        "Walkthrough default document title",
        "Walkthroughs",
        "string",
        max_length=120,
    ),
    SettingControl(
        SettingsKey.WALKTHROUGH_PAGE_SIZE,
        "Walkthrough PDF page size",
        "Walkthroughs",
        "string",
        WalkthroughPageSize.ALL,
        max_length=10,
    ),
    SettingControl(
        SettingsKey.WALKTHROUGH_SHOW_DETAILS,
        "Show date, duration and screenshot count in walkthroughs",
        "Walkthroughs",
    ),
    SettingControl(
        SettingsKey.WALKTHROUGH_SHOW_ABOUT,
        "Show intro note in walkthroughs",
        "Walkthroughs",
    ),
    SettingControl(
        SettingsKey.WALKTHROUGH_SHOW_CAPTIONS,
        "Show screenshot captions in walkthroughs",
        "Walkthroughs",
    ),
    SettingControl(
        SettingsKey.WALKTHROUGH_KEEP_MARKS,
        "Keep drawing marks between walkthrough screenshots",
        "Walkthroughs",
    ),
    SettingControl(
        SettingsKey.SNAP_REDACT_STYLE,
        "Screenshot redaction style",
        "Walkthroughs",
        "string",
        SnapRedactStyle.ALL,
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
        SettingsKey.MEETING_END_REDECODE,
        "High-accuracy re-transcription when meeting ends",
        "Meetings",
    ),
    SettingControl(
        SettingsKey.MEETING_END_POLISH,
        "AI transcript polish when meeting ends",
        "Meetings",
    ),
    SettingControl(
        SettingsKey.MEETING_END_REPORT,
        "Generate final report when meeting ends",
        "Meetings",
    ),
    SettingControl(
        SettingsKey.MEETING_LIVE_THINKING,
        "Meeting live-pass thinking effort",
        "Meetings",
        "string",
        TranscriptCleanupReasoning.ALL,
        max_length=20,
    ),
    SettingControl(
        SettingsKey.MEETING_FINAL_THINKING,
        "Meeting final-report thinking effort",
        "Meetings",
        "string",
        TranscriptCleanupReasoning.ALL,
        max_length=20,
    ),
    SettingControl(
        SettingsKey.MEETING_PAST_RECALL_ENABLED,
        "Allow meeting agent to recall past meetings",
        "Meetings",
    ),
    SettingControl(
        SettingsKey.MEETING_SPEECH_SEPARATE,
        "Separate overlapping voices in meetings",
        "Meetings",
    ),
    SettingControl(
        SettingsKey.TYPESAFE_CITATIONS_ENABLED,
        "Meeting fast citation checks",
        "Meetings",
    ),
    SettingControl(
        SettingsKey.TYPESAFE_TOPIC_SHIFT_ENABLED,
        "Meeting automatic topic-shift detection",
        "Meetings",
    ),
    SettingControl(
        SettingsKey.TYPESAFE_QUESTION_RADAR_ENABLED,
        "Meeting live question radar",
        "Meetings",
    ),
    SettingControl(
        SettingsKey.TYPESAFE_HIGHLIGHTS_ENABLED,
        "Meeting live highlights",
        "Meetings",
    ),
    SettingControl(
        SettingsKey.TYPESAFE_VOICE_COMMANDS_ENABLED,
        "Meeting spoken note-taker commands",
        "Meetings",
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


#: Capabilities that let the token holder change something, rather than read.
#: Reading live events and walkthroughs is read access, like reading history.
WRITE_CAPABILITIES = (
    "retitle_transcriptions",
    "retitle_meetings",
    "manage_annotations",
    "writable_settings",
    permissions.PUBLIC_NAMES[permissions.RUN_ACTIONS],
    permissions.PUBLIC_NAMES[permissions.CONTROL_RECORDING],
    permissions.PUBLIC_NAMES[permissions.TEXT_FILTERS],
    permissions.PUBLIC_NAMES[permissions.AGENT_PROMPTS],
    permissions.PUBLIC_NAMES[permissions.CONTROL_MEETINGS],
    permissions.PUBLIC_NAMES[permissions.CONTROL_WALKTHROUGHS],
    permissions.PUBLIC_NAMES[permissions.MANAGE_PERSONALIZATION],
    permissions.PUBLIC_NAMES[permissions.CONTROL_APP],
)


def writable_settings(settings):
    granted = SettingsKey.MCP_WRITABLE_SETTINGS.read(settings)
    return {key for key in CONTROLS_BY_KEY if granted.get(key) is True}


def _validated_changes(changes):
    """``changes`` checked against SETTING_CONTROLS, or ControlError for the whole update."""
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
    return validated


def _save_changes(saved, validated):
    """Write ``validated`` into ``saved``; None removes a saved override."""
    for key, value in validated.items():
        if value is None:
            saved.pop(key, None)
        else:
            saved[key] = value
    if SettingsKey.STREAMING_ENABLED in validated:
        from services.settings import LEGACY_STREAMING_KEYS

        for key in LEGACY_STREAMING_KEYS:
            saved.pop(key, None)


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
                "permission_denied: Enable this action in Settings > API & MCP."
            )

    def capabilities(self):
        saved = self.settings.load_all_settings() if self.settings is not None else {}
        access = SettingsKey.MCP_SETTINGS_ACCESS.read(saved)
        return {
            "retitle_transcriptions": SettingsKey.MCP_RETITLE_TRANSCRIPTIONS.read(saved),
            "retitle_meetings": SettingsKey.MCP_RETITLE_MEETINGS.read(saved),
            "manage_annotations": SettingsKey.MCP_MANAGE_ANNOTATIONS.read(saved),
            "settings_access": access,
            "writable_settings": sorted(writable_settings(saved)) if access else [],
            # The live API and MCP live tools: live_events, run_actions,
            # control_recording, text_filters, agent_prompts.
            **permissions.describe(saved),
            "mcp_events": resolve_mcp_events_enabled(saved),
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
        validated = _validated_changes(changes)
        if self.settings is None:
            raise ControlError("permission_denied: Settings access is not enabled.")

        def commit(saved):
            # Permission checking and saving share the user's settings lock.
            self._require(saved, SettingsKey.MCP_SETTINGS_ACCESS)
            if not set(validated).issubset(writable_settings(saved)):
                raise ControlError(
                    "permission_denied: Enable each setting in Settings > API & MCP."
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
            _save_changes(saved, validated)
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
            except Exception as exc:
                # The change is already committed. A closed window must not
                # make an agent retry a successful write or expose source data.
                logger.warning("Could not refresh the desktop after an MCP change (%s).",
                               type(exc).__name__)

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
        try:
            title = normalize_title(title)
        except ValueError as exc:
            raise ControlError(f"invalid_title: {exc}") from exc
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

    # ---- Annotations, organization & history digests ------------------------

    def _check_enabled(self):
        if self.enabled is not None and not self.enabled():
            raise ControlError("disabled: MCP access is turned off.")

    def _ro_engine(self):
        def connect():
            conn = sqlite3.connect(
                self.database.as_uri() + "?mode=ro",
                uri=True,
                check_same_thread=False,
                timeout=5,
            )
            conn.execute("PRAGMA query_only=ON")
            return conn

        return create_engine("sqlite://", creator=connect, poolclass=NullPool)

    def _resolve_target(self, conn, record_kind: str, record_id: str, *, require_local: bool = True) -> str:
        """Validate ``(record_kind, record_id)`` and return the canonical ID."""
        try:
            kind = record_annotations.normalize_kind(record_kind)
        except record_annotations.AnnotationValidationError as exc:
            raise ControlError(f"invalid_parameters: {exc}") from None

        if kind == "walkthrough":
            if not isinstance(record_id, str) or not record_id.strip():
                raise ControlError("invalid_parameters: Walkthrough reference is required.")
            from services.walkthrough import library
            from services.walkthrough.storage import captures_root

            folder = library.resolve_folder(captures_root(), record_id.strip())
            if folder is None:
                raise ControlError("not_found: Saved walkthrough not found.")
            return folder.name

        try:
            rid = record_annotations.normalize_record_id(record_id)
        except record_annotations.AnnotationValidationError as exc:
            raise ControlError(f"invalid_parameters: {exc}") from None

        table = (
            TranscriptionHistory if kind == "transcription" else MeetingSession
        ).__table__
        stmt = select(table.c.id).where(table.c.id == rid)
        if require_local:
            stmt = stmt.where(table.c.origin_device_id.is_(None))
        row = conn.execute(stmt).first()
        if row is None:
            raise ControlError(
                "not_found: Local record not found."
                if require_local
                else "not_found: Record not found."
            )
        return rid

    def get_annotation(self, record_kind: str, record_id: str) -> dict[str, Any]:
        self._check_enabled()
        saved = self.settings.load_all_settings() if self.settings is not None else {}
        try:
            kind = record_annotations.normalize_kind(record_kind)
        except record_annotations.AnnotationValidationError as exc:
            raise ControlError(f"invalid_parameters: {exc}") from None
        if kind == "walkthrough" and not permissions.granted(saved, permissions.READ_WALKTHROUGHS):
            raise ControlError(
                "permission_denied: Allow reading saved walkthroughs in OpenWhisper Settings > API & MCP."
            )
        table = RecordAnnotation.__table__
        engine = self._ro_engine()
        try:
            with engine.connect() as conn:
                canonical_id = self._resolve_target(conn, kind, record_id, require_local=False)
                row = (
                    conn.execute(
                        select(table).where(
                            table.c.record_kind == kind,
                            table.c.record_id == canonical_id,
                        )
                    )
                    .mappings()
                    .first()
                )
        finally:
            engine.dispose()
        if row is None:
            return record_annotations.empty_annotation(kind, canonical_id)
        return record_annotations.row_to_dict(row)

    def _apply_annotation_row(
        self,
        conn,
        kind: str,
        canonical_id: str,
        fields: dict[str, Any],
        *,
        merge: bool,
    ) -> dict[str, Any]:
        table = RecordAnnotation.__table__
        existing_row = (
            conn.execute(
                select(table).where(
                    table.c.record_kind == kind,
                    table.c.record_id == canonical_id,
                )
            )
            .mappings()
            .first()
        )
        existing = (
            record_annotations.row_to_dict(existing_row)
            if existing_row is not None
            else record_annotations.empty_annotation(kind, canonical_id)
        )

        try:
            summary = (
                record_annotations.normalize_summary(fields["summary"])
                if "summary" in fields
                else (existing["summary"] if merge else None)
            )
            notes = (
                record_annotations.normalize_notes(fields["notes"])
                if "notes" in fields
                else (existing["notes"] if merge else None)
            )
            tags = (
                record_annotations.normalize_tags(fields["tags"])
                if "tags" in fields
                else (existing["tags"] if merge else [])
            )
            external_refs = (
                record_annotations.normalize_external_refs(fields["external_refs"])
                if "external_refs" in fields
                else (existing["external_refs"] if merge else [])
            )
            author = (
                record_annotations.normalize_author(fields["author"])
                if "author" in fields
                else (existing["author"] if merge else None)
            )
        except record_annotations.AnnotationValidationError as exc:
            raise ControlError(f"invalid_value: {exc}") from None

        if not (summary or notes or tags or external_refs):
            if existing_row is not None:
                conn.execute(
                    delete(table).where(
                        table.c.record_kind == kind,
                        table.c.record_id == canonical_id,
                    )
                )
            return record_annotations.empty_annotation(kind, canonical_id)

        now = record_annotations.utc_now_iso()
        values = {
            "summary": summary,
            "notes": notes,
            "tags_json": json.dumps(tags, ensure_ascii=False),
            "external_refs_json": json.dumps(external_refs, ensure_ascii=False),
            "author": author,
            "updated_at": now,
        }
        if existing_row is None:
            conn.execute(
                insert(table).values(
                    record_kind=kind,
                    record_id=canonical_id,
                    **values,
                )
            )
        else:
            conn.execute(
                update(table)
                .where(
                    table.c.record_kind == kind,
                    table.c.record_id == canonical_id,
                )
                .values(**values)
            )
        return {
            "exists": True,
            "record_kind": kind,
            "record_id": canonical_id,
            "summary": summary,
            "notes": notes,
            "tags": tags,
            "external_refs": external_refs,
            "author": author,
            "updated_at": now,
        }

    def set_annotation(
        self,
        record_kind: str,
        record_id: str,
        fields: dict[str, Any],
        *,
        merge: bool = True,
    ) -> dict[str, Any]:
        saved = self.settings.load_all_settings() if self.settings is not None else {}
        self._require(saved, SettingsKey.MCP_MANAGE_ANNOTATIONS)
        try:
            kind = record_annotations.normalize_kind(record_kind)
        except record_annotations.AnnotationValidationError as exc:
            raise ControlError(f"invalid_parameters: {exc}") from None
        if kind == "walkthrough" and not permissions.granted(saved, permissions.READ_WALKTHROUGHS):
            raise ControlError(
                "permission_denied: Allow reading saved walkthroughs in OpenWhisper Settings > API & MCP."
            )

        engine = self._engine()
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                canonical_id = self._resolve_target(conn, kind, record_id, require_local=True)
                result = self._apply_annotation_row(
                    conn, kind, canonical_id, fields, merge=merge
                )
        finally:
            engine.dispose()
        self._changed("annotation", {"record_kind": kind, "id": canonical_id})
        return result

    def delete_annotation(self, record_kind: str, record_id: str) -> dict[str, Any]:
        saved = self.settings.load_all_settings() if self.settings is not None else {}
        self._require(saved, SettingsKey.MCP_MANAGE_ANNOTATIONS)
        try:
            kind = record_annotations.normalize_kind(record_kind)
        except record_annotations.AnnotationValidationError as exc:
            raise ControlError(f"invalid_parameters: {exc}") from None
        if kind == "walkthrough" and not permissions.granted(saved, permissions.READ_WALKTHROUGHS):
            raise ControlError(
                "permission_denied: Allow reading saved walkthroughs in OpenWhisper Settings > API & MCP."
            )

        table = RecordAnnotation.__table__
        engine = self._engine()
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                canonical_id = self._resolve_target(conn, kind, record_id, require_local=True)
                res = conn.execute(
                    delete(table).where(
                        table.c.record_kind == kind,
                        table.c.record_id == canonical_id,
                    )
                )
                deleted = bool(res.rowcount)
        finally:
            engine.dispose()
        if deleted:
            self._changed("annotation", {"record_kind": kind, "id": canonical_id})
        return {
            "deleted": deleted,
            "record_kind": kind,
            "record_id": canonical_id,
        }

    def list_annotations(
        self,
        *,
        record_kind: str = "all",
        tag: str | None = None,
        external_system: str | None = None,
        q: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        self._check_enabled()
        saved = self.settings.load_all_settings() if self.settings is not None else {}
        allow_walkthroughs = permissions.granted(saved, permissions.READ_WALKTHROUGHS)
        if record_kind != "all":
            try:
                record_kind = record_annotations.normalize_kind(record_kind)
            except record_annotations.AnnotationValidationError as exc:
                raise ControlError(f"invalid_parameters: {exc}") from None
            if record_kind == "walkthrough" and not allow_walkthroughs:
                raise ControlError(
                    "permission_denied: Allow reading saved walkthroughs in OpenWhisper Settings > API & MCP."
                )

        table = RecordAnnotation.__table__
        stmt = select(table).order_by(table.c.updated_at.desc(), table.c.record_id.asc())
        if record_kind != "all":
            stmt = stmt.where(table.c.record_kind == record_kind)
        elif not allow_walkthroughs:
            stmt = stmt.where(table.c.record_kind != "walkthrough")

        engine = self._ro_engine()
        try:
            with engine.connect() as conn:
                rows = [dict(r) for r in conn.execute(stmt).mappings().all()]
        finally:
            engine.dispose()

        tag_needle = tag.strip().lstrip("#").casefold() if isinstance(tag, str) and tag.strip() else None
        sys_needle = external_system.strip().casefold() if isinstance(external_system, str) and external_system.strip() else None
        q_needle = q.strip().casefold() if isinstance(q, str) and q.strip() else None

        items: list[dict[str, Any]] = []
        for row in rows:
            entry = record_annotations.row_to_dict(row)
            if tag_needle is not None and not any(t.casefold() == tag_needle for t in entry["tags"]):
                continue
            if sys_needle is not None and not any(
                r["system"].casefold() == sys_needle for r in entry["external_refs"]
            ):
                continue
            if q_needle is not None:
                haystack = " ".join(
                    part
                    for part in (
                        entry["summary"] or "",
                        entry["notes"] or "",
                        " ".join(entry["tags"]),
                        " ".join(
                            f"{r.get('system', '')} {r.get('id', '')} {r.get('label', '')}"
                            for r in entry["external_refs"]
                        ),
                    )
                    if part
                ).casefold()
                if q_needle not in haystack:
                    continue
            items.append(entry)
            if len(items) >= limit:
                break
        return {"items": items, "count": len(items)}

    def list_tags(self, *, record_kind: str = "all") -> dict[str, Any]:
        self._check_enabled()
        saved = self.settings.load_all_settings() if self.settings is not None else {}
        allow_walkthroughs = permissions.granted(saved, permissions.READ_WALKTHROUGHS)
        if record_kind != "all":
            try:
                record_kind = record_annotations.normalize_kind(record_kind)
            except record_annotations.AnnotationValidationError as exc:
                raise ControlError(f"invalid_parameters: {exc}") from None
            if record_kind == "walkthrough" and not allow_walkthroughs:
                raise ControlError(
                    "permission_denied: Allow reading saved walkthroughs in OpenWhisper Settings > API & MCP."
                )

        table = RecordAnnotation.__table__
        stmt = select(table.c.tags_json)
        if record_kind != "all":
            stmt = stmt.where(table.c.record_kind == record_kind)
        elif not allow_walkthroughs:
            stmt = stmt.where(table.c.record_kind != "walkthrough")

        engine = self._ro_engine()
        try:
            with engine.connect() as conn:
                raw_lists = conn.execute(stmt).scalars().all()
        finally:
            engine.dispose()

        counts: dict[str, int] = {}
        canonical_names: dict[str, str] = {}
        for raw in raw_lists:
            for tag in record_annotations.parse_tags_json(raw):
                key = tag.casefold()
                canonical_names.setdefault(key, tag)
                counts[key] = counts.get(key, 0) + 1

        ordered = sorted(
            ({"tag": canonical_names[k], "count": c} for k, c in counts.items()),
            key=lambda item: (-item["count"], item["tag"].casefold()),
        )
        return {"tags": ordered}

    def organize_records(
        self, items: list[dict[str, Any]], *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Batch-retitle and/or annotate up to 25 local records in one validated call."""
        self._check_enabled()
        if not isinstance(items, list) or not 1 <= len(items) <= 25:
            raise ControlError("invalid_parameters: Supply between 1 and 25 items.")
        saved = self.settings.load_all_settings() if self.settings is not None else {}
        allow_walkthroughs = permissions.granted(saved, permissions.READ_WALKTHROUGHS)

        # Pre-check permissions & validate inputs before touching SQLite.
        prepared: list[dict[str, Any]] = []
        needs_ann = False
        needs_retitle_tx = False
        needs_retitle_mt = False
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                raise ControlError(f"invalid_parameters: items[{idx}] must be an object.")
            try:
                kind = record_annotations.normalize_kind(item.get("record_kind", ""))
            except record_annotations.AnnotationValidationError as exc:
                raise ControlError(f"invalid_parameters: items[{idx}]: {exc}") from None
            if kind == "walkthrough" and not allow_walkthroughs:
                raise ControlError(
                    "permission_denied: Allow reading saved walkthroughs in OpenWhisper Settings > API & MCP."
                )

            title = item.get("title")
            has_title = "title" in item and title is not None
            if has_title:
                if kind == "walkthrough":
                    raise ControlError(
                        "invalid_parameters: Walkthroughs cannot be retitled with organize_records; use summary/notes/tags instead."
                    )
                try:
                    title = normalize_title(title)
                except ValueError as exc:
                    raise ControlError(f"invalid_title: items[{idx}]: {exc}") from None
                if kind == "transcription":
                    needs_retitle_tx = True
                else:
                    needs_retitle_mt = True

            ann_fields = {
                key: item[key]
                for key in ("summary", "notes", "tags", "external_refs", "author")
                if key in item
            }
            if ann_fields:
                needs_ann = True
                try:
                    if "summary" in ann_fields:
                        record_annotations.normalize_summary(ann_fields["summary"])
                    if "notes" in ann_fields:
                        record_annotations.normalize_notes(ann_fields["notes"])
                    if "tags" in ann_fields:
                        record_annotations.normalize_tags(ann_fields["tags"])
                    if "external_refs" in ann_fields:
                        record_annotations.normalize_external_refs(ann_fields["external_refs"])
                    if "author" in ann_fields:
                        record_annotations.normalize_author(ann_fields["author"])
                except record_annotations.AnnotationValidationError as exc:
                    raise ControlError(f"invalid_value: items[{idx}]: {exc}") from None

            if not has_title and not ann_fields:
                raise ControlError(
                    f"invalid_parameters: items[{idx}] must specify title and/or annotation fields."
                )
            prepared.append(
                {
                    "kind": kind,
                    "record_id": item.get("record_id", ""),
                    "title": title if has_title else None,
                    "ann_fields": ann_fields,
                    "merge": bool(item.get("merge", True)),
                }
            )

        if needs_retitle_tx:
            self._require(saved, SettingsKey.MCP_RETITLE_TRANSCRIPTIONS)
        if needs_retitle_mt:
            self._require(saved, SettingsKey.MCP_RETITLE_MEETINGS)
        if needs_ann:
            self._require(saved, SettingsKey.MCP_MANAGE_ANNOTATIONS)

        results: list[dict[str, Any]] = []
        meeting_renames: list[tuple[str, str]] = []
        changed_events: list[tuple[str, dict[str, Any]]] = []

        engine = self._ro_engine() if dry_run else self._engine()
        try:
            ctx = engine.connect() if dry_run else engine.begin()
            with ctx as conn:
                if not dry_run:
                    conn.exec_driver_sql("BEGIN IMMEDIATE")
                for spec in prepared:
                    kind = spec["kind"]
                    canonical_id = self._resolve_target(conn, kind, spec["record_id"], require_local=True)
                    outcome: dict[str, Any] = {
                        "record_kind": kind,
                        "record_id": canonical_id,
                    }
                    if spec["title"] is not None:
                        table = (
                            TranscriptionHistory if kind == "transcription" else MeetingSession
                        ).__table__
                        cols = [table.c.title]
                        if kind == "meeting":
                            cols.extend([table.c.status, table.c.state_json])
                        row = (
                            conn.execute(select(*cols).where(table.c.id == canonical_id))
                            .mappings()
                            .first()
                        )
                        if kind == "meeting" and row["status"] not in {"ended", "failed"}:
                            raise ControlError(
                                "record_busy: Finish the meeting before changing its history title."
                            )
                        state = None
                        if kind == "meeting":
                            try:
                                state = json.loads(row["state_json"]) if row["state_json"] else {}
                                if not isinstance(state, dict):
                                    raise ValueError
                            except (ValueError, TypeError):
                                raise ControlError(
                                    "snapshot_unavailable: Saved meeting state cannot be updated."
                                ) from None
                        outcome["title"] = spec["title"]
                        outcome["previous_title"] = row["title"]
                        if not dry_run:
                            if kind == "meeting" and self.meeting_renamer is not None:
                                meeting_renames.append((canonical_id, spec["title"]))
                            else:
                                vals: dict[str, Any] = {"title": spec["title"]}
                                if kind == "meeting" and state is not None:
                                    state["title"] = spec["title"]
                                    vals["state_json"] = json.dumps(state, ensure_ascii=False)
                                conn.execute(
                                    update(table).where(table.c.id == canonical_id).values(**vals)
                                )
                            changed_events.append((kind, {"id": canonical_id, "title": spec["title"]}))

                    if spec["ann_fields"]:
                        if dry_run:
                            outcome["annotation_preview"] = {
                                **spec["ann_fields"],
                                "merge": spec["merge"],
                            }
                        else:
                            outcome["annotation"] = self._apply_annotation_row(
                                conn,
                                kind,
                                canonical_id,
                                spec["ann_fields"],
                                merge=spec["merge"],
                            )
                            changed_events.append(
                                ("annotation", {"record_kind": kind, "id": canonical_id})
                            )
                    results.append(outcome)
        finally:
            engine.dispose()

        if not dry_run:
            for meeting_id, title in meeting_renames:
                self.meeting_renamer(meeting_id, title)
            for event_kind, payload in changed_events:
                self._changed(event_kind, payload)

        return {
            "dry_run": dry_run,
            "count": len(results),
            "results": results,
        }

    def history_digest(
        self,
        *,
        since: str | None = None,
        before: str | None = None,
        kind: str = "all",
        limit: int = 30,
        include_text: bool = True,
        max_chars_per_item: int = 1500,
        include_annotations: bool = True,
        include_remote: bool = False,
        only_unannotated: bool = False,
    ) -> dict[str, Any]:
        """Return a bounded chronological digest of dictations, meetings, and walkthroughs."""
        self._check_enabled()
        saved = self.settings.load_all_settings() if self.settings is not None else {}
        allow_walkthroughs = permissions.granted(saved, permissions.READ_WALKTHROUGHS)
        if kind not in {"all", "transcription", "meeting", "walkthrough"}:
            raise ControlError("invalid_parameters: kind must be all, transcription, meeting, or walkthrough.")
        if kind == "walkthrough" and not allow_walkthroughs:
            raise ControlError(
                "permission_denied: Allow reading saved walkthroughs in OpenWhisper Settings > API & MCP."
            )

        H = TranscriptionHistory.__table__
        M = MeetingSession.__table__
        S = MeetingSegment.__table__
        A = RecordAnnotation.__table__

        def _clip(text: str | None) -> tuple[str, bool]:
            raw = (text or "").strip()
            if len(raw) <= max_chars_per_item:
                return raw, False
            return raw[:max_chars_per_item].rstrip() + "…", True

        engine = self._ro_engine()
        try:
            with engine.connect() as conn:
                ann_map: dict[tuple[str, str], dict[str, Any]] = {}
                for row in conn.execute(select(A)).mappings().all():
                    ann_map[(row["record_kind"], row["record_id"])] = record_annotations.row_to_dict(row)

                entries: list[dict[str, Any]] = []

                if kind in {"all", "transcription"}:
                    stmt = select(
                        H.c.id,
                        H.c.title,
                        H.c.source_name,
                        H.c.timestamp,
                        H.c.text,
                        H.c.raw_text,
                        H.c.model,
                        H.c.app_name,
                        H.c.entry_kind,
                        H.c.audio_duration,
                        H.c.origin_device_id,
                        H.c.origin_device_name,
                    ).order_by(func.coalesce(func.julianday(H.c.timestamp), 0.0).desc(), H.c.id.asc())
                    if not include_remote:
                        stmt = stmt.where(H.c.origin_device_id.is_(None))
                    if since is not None:
                        stmt = stmt.where(
                            func.coalesce(func.julianday(H.c.timestamp), 0.0) >= func.julianday(since)
                        )
                    if before is not None:
                        stmt = stmt.where(
                            func.coalesce(func.julianday(H.c.timestamp), 0.0) < func.julianday(before)
                        )
                    fetch_limit = limit * 4 if only_unannotated else limit
                    for row in conn.execute(stmt.limit(fetch_limit)).mappings().all():
                        ann = ann_map.get(("transcription", row["id"]))
                        if only_unannotated and ann is not None:
                            continue
                        item: dict[str, Any] = {
                            "record_kind": "transcription",
                            "record_id": row["id"],
                            "timestamp": row["timestamp"],
                            "title": row["title"],
                            "source_name": row["source_name"],
                            "entry_kind": row["entry_kind"],
                            "app_name": row["app_name"],
                            "audio_duration": row["audio_duration"],
                            "origin_device_id": row["origin_device_id"],
                            "origin_device_name": row["origin_device_name"],
                            "has_raw_diff": bool(row["raw_text"] and row["raw_text"] != row["text"]),
                        }
                        if include_text:
                            text_val, truncated = _clip(row["text"])
                            item["text"] = text_val
                            item["truncated"] = truncated
                        if include_annotations and ann is not None:
                            item["annotation"] = ann
                        entries.append(item)

                if kind in {"all", "meeting"}:
                    stmt = select(
                        M.c.id,
                        M.c.title,
                        M.c.status,
                        M.c.started_at,
                        M.c.ended_at,
                        M.c.state_json,
                        M.c.origin_device_id,
                        M.c.origin_device_name,
                    ).order_by(func.coalesce(func.julianday(M.c.started_at), 0.0).desc(), M.c.id.asc())
                    if not include_remote:
                        stmt = stmt.where(M.c.origin_device_id.is_(None))
                    if since is not None:
                        stmt = stmt.where(
                            func.coalesce(func.julianday(M.c.started_at), 0.0) >= func.julianday(since)
                        )
                    if before is not None:
                        stmt = stmt.where(
                            func.coalesce(func.julianday(M.c.started_at), 0.0) < func.julianday(before)
                        )
                    fetch_limit = limit * 4 if only_unannotated else limit
                    meeting_rows = list(conn.execute(stmt.limit(fetch_limit)).mappings().all())
                    meeting_ids = [r["id"] for r in meeting_rows]
                    seg_counts: dict[str, int] = {}
                    if meeting_ids:
                        for mid, cnt in conn.execute(
                            select(S.c.meeting_id, func.count(S.c.id))
                            .where(S.c.meeting_id.in_(meeting_ids))
                            .group_by(S.c.meeting_id)
                        ).all():
                            seg_counts[mid] = int(cnt)
                    for row in meeting_rows:
                        ann = ann_map.get(("meeting", row["id"]))
                        if only_unannotated and ann is not None:
                            continue
                        rolling = ""
                        if row["state_json"]:
                            try:
                                parsed = json.loads(row["state_json"])
                                if isinstance(parsed, dict):
                                    rolling = str(parsed.get("rolling_summary") or "")
                            except (ValueError, TypeError):
                                rolling = ""
                        item = {
                            "record_kind": "meeting",
                            "record_id": row["id"],
                            "timestamp": row["started_at"],
                            "ended_at": row["ended_at"],
                            "title": row["title"],
                            "status": row["status"],
                            "segment_count": seg_counts.get(row["id"], 0),
                            "origin_device_id": row["origin_device_id"],
                            "origin_device_name": row["origin_device_name"],
                        }
                        if include_text and rolling:
                            summary_val, truncated = _clip(rolling)
                            item["rolling_summary"] = summary_val
                            item["truncated"] = truncated
                        if include_annotations and ann is not None:
                            item["annotation"] = ann
                        entries.append(item)

                walkthroughs_included = False
                if kind == "walkthrough" or (kind == "all" and allow_walkthroughs):
                    walkthroughs_included = True
                    from services.walkthrough import library
                    from services.walkthrough.storage import captures_root

                    root = captures_root()
                    folders = library.entries(root) if root.is_dir() else []
                    numbers = library.folder_numbers(root) if folders else {}
                    for folder in folders[: limit * 2]:
                        ann = ann_map.get(("walkthrough", folder.name))
                        if only_unannotated and ann is not None:
                            continue
                        try:
                            doc = library.load_document(folder)
                        except Exception:
                            continue
                        ts = doc.started_at.isoformat()
                        if since is not None and ts < since:
                            continue
                        if before is not None and ts >= before:
                            continue
                        ref = library.format_number(doc.number or numbers.get(folder))
                        spoken = " ".join(s.text.strip() for s in doc.sections if s.text.strip())
                        item = {
                            "record_kind": "walkthrough",
                            "record_id": folder.name,
                            "ref": ref,
                            "timestamp": ts,
                            "duration_s": doc.duration_s,
                            "shot_count": sum(1 for s in doc.sections if s.shot),
                            "written_up": library.written_up(folder),
                        }
                        if include_text and spoken:
                            text_val, truncated = _clip(spoken)
                            item["text"] = text_val
                            item["truncated"] = truncated
                        if include_annotations and ann is not None:
                            item["annotation"] = ann
                        entries.append(item)
        finally:
            engine.dispose()

        entries.sort(key=lambda e: str(e.get("timestamp") or ""), reverse=True)
        has_more = len(entries) > limit
        entries = entries[:limit]
        return {
            "items": entries,
            "count": len(entries),
            "has_more": has_more,
            "walkthroughs_included": walkthroughs_included,
        }
