# Codebase Cleanup & Unification — July 2026

## Overview

Full-codebase audit (dead code, duplicated systems, documentation drift, naming
conventions) and the cleanup applied from it. Four parallel audits found the
architecture fundamentally sound — persistence (JSON settings / SQLite history)
is cleanly separated, `OverlayState` enum routing is canonical, class suffixes
follow policy, and all modules are live — but surfaced concentrated debris:

1. **Dead/orphaned config constants** — 7 constants from the deleted bar-style
   waveform visualizer; 2 more constants orphaned by hardcoded twins.
2. **Duplicated hotkey backend logic** — `_hotkey_keyboard.py` (Windows) and
   `_hotkey_pynput.py` (macOS/Linux) carry near-identical copies of hotkey
   parsing, formatting, and debounce logic.
3. **Duplicated formatting helpers** — timestamp formatting in two places,
   file-size formatting in two places.
4. **Naming violations** — British `_cancelled`, `transcription_text` for
   transcript widgets, inconsistent `audio_filename` param.
5. **Stale docs** — CLAUDE.md/AGENTS.md referencing `main_window_qt.py`,
   `overlay_qt.py`, nonexistent tests/docs; CHANGELOG frozen at Jan 2026.
6. **Junk files** — debug log leftovers, temp WAVs, messy .gitignore with
   duplicate entries and a `specs/` ignore rule contradicting CLAUDE.md.

## Files to Modify

### 1. Config constants (`config.py`, `ui_qt/overlays/waveform_overlay.py`, `services/settings.py`)

- `waveform_overlay.py:119` — `self.frame_rate = 30` → `config.WAVEFORM_FRAME_RATE`.
- `waveform_overlay.py:444` — `self.hidden_timer.start(1500)` → `config.OVERLAY_HIDE_DELAY_MS`.
- `config.py` — delete `WAVEFORM_BAR_COUNT`, `WAVEFORM_BAR_WIDTH`,
  `WAVEFORM_BAR_SPACING`, `WAVEFORM_BG_COLOR`, `WAVEFORM_ACCENT_COLOR`,
  `WAVEFORM_SECONDARY_COLOR`, `WAVEFORM_TEXT_COLOR` (legacy bar visualizer;
  particle style has its own colors in `WAVEFORM_STYLE_CONFIGS`).
- `services/settings.py:29` — delete unused `STREAMING_TYPING_DELAY` key.

### 2. Hotkey unification (`services/_hotkey_common.py` — new)

Extract from `_hotkey_keyboard.py` and `_hotkey_pynput.py`:

- `parse_hotkey()` / `format_hotkey()` — identical logic, parameterized by a
  modifier-alias map (pynput adds `command`/`option`/`opt` aliases).
- `format_hotkey_display()` — parameterized by display symbols
  (mac ⌘⌃⌥⇧ vs Ctrl/Alt/Shift).
- Debounce (`_should_trigger_record_toggle`) — literal duplicate → shared
  `Debouncer` class using `config.HOTKEY_DEBOUNCE_MS`.

Platform-specific event matching stays in each backend. The dispatcher in
`hotkey_manager.py` keeps re-exporting the selected backend's API unchanged.

### 3. Formatting helpers (`services/format_utils.py` — new)

- `format_timestamp(iso_str)` — shared by `services/models.py`
  (`TranscriptionHistory.formatted_timestamp`) and
  `services/history_manager.py` (`RecordingInfo.formatted_timestamp`).
- `format_file_size(bytes)` — shared by `history_manager.py` and
  `ui_qt/widgets/stats_display.py` (`_format_file_size`).

### 4. Naming fixes

- `ui_qt/dialogs/hotkey_dialog.py` — `_cancelled` → `_canceled` (5 sites).
- `ui_qt/widgets/quick_record_tab.py`, `ui_qt/widgets/upload_file_tab.py` —
  `self.transcription_text` → `self.transcript_text` (internal-only attribute,
  verified no external references).
- `services/database.py:246` — `update_history_audio_file(audio_filename)` →
  parameter renamed for consistency with its actual meaning.
- `transcriber/openai_backend.py:94,165` — file-handle variable `audio_file`
  → `f` to avoid clashing with the `audio_path` naming convention.

### 5. Junk removal / .gitignore

Delete (local, gitignored, ephemeral): `openwhisper.log.task2`,
`openwhisper.log.task3-debug`, `openwhisper.log.preverify`,
`openwhisper_test.log`, `res/tmpiqi7y5ui.wav`.

Kept for user confirmation (contain migrated user data):
`transcription_history.json.bak`, `meetings.json.bak`, `openwhisper.crash.log`.

`.gitignore`: dedupe (`meetings.json` ×2, `specs/` + `specs/*`,
`.claude/settings.local.json` variants), remove `specs/` ignore rules
(CLAUDE.md mandates specs be version-controlled).

### 6. Documentation

- CLAUDE.md + AGENTS.md: fix `main_window_qt.py` → `main_window.py`,
  `system_tray_qt.py` → `system_tray.py`, `overlay_qt.py` →
  `overlays/waveform_overlay.py`; remove references to nonexistent
  `test_cancellation_animations.py`, `PYQT6_REFACTOR_SUMMARY.md`,
  `GETTING_STARTED_QT.md`; clarify numpad (`kp *`) hotkey notation; add
  missing Minimize-to-tray hotkey in AGENTS.md; keep both files in sync.
- CHANGELOG.md: add `[Unreleased]` section covering Jan–Jun 2026 work.

## Explicitly NOT changed (audited, found sound)

- Persistence split (settings=JSON, history=SQLite) — clean, migration idempotent.
- `OverlayState` enum routing — canonical, no string parsing.
- Hotkey backend selection/dispatch architecture — correctly isolated.
- Streaming split (`StreamingTranscriber` / `StreamingRuntime` / controller) —
  proper single-responsibility separation.
- macOS-only code (`_hotkey_carbon.py`, accessibility helpers) — legitimate
  cross-platform support, properly guarded.
- Audio smoothing "duplication" — recorder (exponential, real-time UI) vs
  audio_processor (convolution, silence detection) serve different purposes.

## Verification

- `python -m pytest tests/` (venv active) — full suite green.
- `python -c "import app_qt"` — entry point imports cleanly.
- Grep confirms no remaining references to deleted constants/keys.
