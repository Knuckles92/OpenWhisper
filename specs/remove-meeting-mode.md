# Remove Meeting Mode

## Overview

Remove the experimental Meeting Mode feature completely to reduce application complexity. Meeting data does not need to be preserved; the SQLite migration can drop meeting-related tables.

## Files to Modify

- `services/application_controller.py`: remove meeting runtime/controller setup and wrapper methods.
- `services/runtime/__init__.py`: stop exporting meeting runtime.
- `services/runtime/meeting.py`: delete.
- `services/meeting_controller.py`: delete.
- `services/meeting_storage.py`: delete.
- `services/models.py`: remove `Meeting` and `MeetingChunk` models.
- `services/database.py`: remove meeting CRUD, JSON migration, and bump schema with table-drop migration.
- `services/settings.py`: remove meeting recording settings helpers.
- `config.py`: remove meeting-related constants.
- `ui_qt/main_window_qt.py`: remove Meeting Mode tab wiring and tab-specific sidebar switching.
- `ui_qt/ui_controller.py`: remove meeting callbacks and helper methods.
- `ui_qt/widgets/__init__.py`: remove meeting widget exports.
- `ui_qt/widgets/tabbed_content.py`: simplify to Quick Record only.
- `ui_qt/widgets/history_sidebar.py`: remove meeting sidebar mode and meeting item UI.
- `ui_qt/widgets/meeting_tab.py`: delete.
- `ui_qt/meeting/`: delete unused standalone meeting window package.
- `ui_qt/dialogs/settings_dialog.py`: remove meeting recording settings controls.
- `tests/test_database.py`: remove meeting table/CRUD/migration/storage tests and add drop-table migration coverage.
- `tests/test_application_controller.py`: remove meeting stubs and assertions.
- `README.md`: remove Meeting Mode feature bullet.

## Implementation Notes

- Existing meeting runtime data folders should not be deleted by code changes.
- Remove stale meeting keys from `openwhisper_settings.json` when present.
- Database migration should drop `meetings`, `meeting_chunks`, and any legacy `meeting_insights` artifacts.
- Keep Quick Record, transcription history, recordings, streaming, and upload behavior unchanged.

## Verification

- Run `./venv312/Scripts/python.exe -m pytest tests/test_database.py tests/test_application_controller.py`.
- Run `./venv312/Scripts/python.exe -m pytest tests/` if targeted tests pass.
- Search for remaining `meeting|Meeting|MEETING` references and verify only unrelated/generated artifacts remain.
