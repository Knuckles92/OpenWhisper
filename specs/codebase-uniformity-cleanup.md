# Codebase Uniformity Cleanup

## Overview

Apply a focused organization cleanup after the Meeting Mode removal work. The goal is to reduce repository noise, make future diffs reviewable, and fix the visible Quick Record cancel path.

## Files to Modify

- `.gitattributes`: define line-ending policy.
- `.gitignore`: stop ignoring implementation plans and keep runtime artifacts ignored.
- `pyproject.toml`: add minimal pytest, Black, and Ruff configuration.
- `ui_qt/widgets/quick_record_tab.py`: expose a cancel signal from the visible Cancel button.
- `ui_qt/main_window_qt.py`: bridge Quick Record cancel events to the main-window signal layer.
- `ui_qt/ui_controller.py`: bridge main-window cancel events to application callbacks.
- `openwhisper_settings.json`: remove stale Meeting Mode insight keys from the local settings file.
- `CLAUDE.md` and `README.md`: align docs with current `services/` package layout and available waveform styles.

## Implementation Notes

- Keep behavior changes minimal; the only intentional runtime behavior change is making the visible Cancel button call the existing cancel flow.
- Preserve database migration references to removed meeting tables because they are needed for users upgrading from older versions.
- Normalize line endings after code edits to prevent future whole-file churn.

## Verification

- Run `git diff --check`.
- Run targeted tests for bootstrap, controller, database, and hotkey behavior.
- Search for stale Meeting Mode references and confirm only migration/test/spec/history text remains.
