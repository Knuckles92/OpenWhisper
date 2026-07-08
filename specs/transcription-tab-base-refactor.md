# Transcription Tab Base Class Refactor

## Overview

`QuickRecordTab` and `UploadFileTab` duplicate ~200 lines of UI scaffolding,
signals, and behavior (model card, status label, collapsible transcription
card, stats widget, engine-settings/transcription collapse logic, model
selection sync helpers). `MainWindow` compounds this with pair-maintenance
glue: duplicated signal wiring and `sender()`-based "mirror onto the other
tab" handlers.

This refactor extracts a shared `TranscriptionTabBase` (template method
pattern) and makes `MainWindow` iterate a `transcription_tabs` tuple instead
of hard-coding the pair.

## Files

- **New:** `ui_qt/widgets/transcription_tab_base.py` — `TranscriptionTabBase(QWidget)`
  - Shared signals: `model_changed`, `engine_settings_changed`,
    `engine_settings_collapsed`, `transcription_collapsed`
  - Shared widgets: `model_combo`, `local_engine`, `status_label`,
    `transcription_card`, `transcript_text`, `stats_widget`
  - Subclass configuration via class attributes:
    `CONTENT_OBJECT_NAME`, `INITIAL_STATUS`, `TRANSCRIPT_PLACEHOLDER`
  - Subclass hooks called from `_setup_ui`:
    `_build_content_before_status(layout)` (upload: drop zone + file card),
    `_build_content_after_status(layout)` (quick record: control panel)
  - Subclasses extend `_connect_signals` via `super()._connect_signals()`
  - Shared methods: `_on_model_changed`, `set_status`, `set_device_info`,
    `set_local_engine_visible`, `_on_engine_settings_toggled`,
    `set_engine_settings_collapsed`, `_apply_transcription_stretch`,
    `_on_transcription_toggled`, `set_transcription_collapsed`,
    `is_transcription_collapsed`, `set_transcript`, `clear_transcription`,
    `set_transcription_stats`, `clear_transcription_stats`,
    `get_model_value`, `set_model_selection`

- **Modified:** `ui_qt/widgets/quick_record_tab.py` — subclass keeps only
  recording buttons/state, streaming partials, `update_hotkeys`. The unused
  `retranscribe_requested` signal is removed (nothing connects to it; the
  live signal of that name is on `HistorySidebar`).

- **Modified:** `ui_qt/widgets/upload_file_tab.py` — `DropZoneWidget` and
  `FileInfoCard` unchanged; `UploadFileTab` keeps only file lifecycle
  (`_on_file_selected`, `_on_transcribe`, `clear_file`, `set_file`,
  `open_file_browser`) and overrides `set_transcript` to also reset
  busy state.

- **Modified:** `ui_qt/main_window.py`
  - `self.transcription_tabs = (quick_record_tab, upload_file_tab)` after
    construction; shared signal wiring becomes a loop over the tuple
  - `_on_model_changed`, `_apply_local_engine_visibility`,
    `_on_engine_settings_changed`, `_load_saved_settings`,
    `set_device_info` iterate `transcription_tabs`
  - `_on_transcription_collapsed` / `_on_engine_settings_collapsed` mirror
    onto `[t for t in transcription_tabs if t is not self.sender()]`

- **Modified:** `ui_qt/widgets/__init__.py` — export `TranscriptionTabBase`.

## Notes

- PyQt6 forbids setting attributes on a `QWidget` before
  `super().__init__()`; subclass state (`is_recording`, `_partial_buffer`,
  `_audio_path`) is initialized after the base constructor, which is safe
  because `_setup_ui` never reads it and signals cannot fire during init.
- Layout order is preserved exactly: quick record's unique content is below
  the status label; upload's is above it.
- Public APIs of both tabs are unchanged (except the dead
  `retranscribe_requested` removal), so `UIController` and
  `ApplicationController` need no changes.

## Verification

1. Offscreen smoke test: instantiate both tabs under
   `QT_QPA_PLATFORM=offscreen`, check shared/unique widgets exist and
   signals connect.
2. `python -m pytest tests/` (venv).
3. Manual: model combo sync between tabs, engine-settings collapse mirror,
   transcription-card collapse mirror, upload flow, record flow.
