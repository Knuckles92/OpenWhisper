# Optional guided setup

Guided setup lives in `ui_qt/dialogs/guided_setup.py`, separate from
`SettingsDialog` navigation. File → Guided setup… reopens the same setup
window after skipping or finishing. All settings opens the usual settings
home; Change links use stable destination keys.

## Integration

- `services/guided_setup.py` seeds `guided_setup_state` before the main window
  creates data. It uses `SettingsManager.is_new_install()` (the settings-file
  snapshot from before startup writes) plus a conservative prior-data check.
  Existing settings, a database, legacy history, a scratchpad, recordings, or
  meetings suppress the automatic invitation. Corrupt files are preserved.
- Bootstrap calls `initialize()` before `keep_unchosen_engine()` and offers
  setup after the main window is ready. Update health-check launches do not
  offer setup. The UI timer yields to startup modal dialogs and a minimized
  or hidden main window.
- State values are `pending`, `started`, `skipped`, `completed`, and `existing`.
  Only `pending` is invited. Starting, skipping, closing, and finishing save
  an outcome; failed saves remain visible and can be retried. Pending state
  survives unrelated first-launch writes and a restart.
- MainWindow adds a File menu action and signal. UIController owns the
  invitation/setup singletons and supplies its existing configured settings
  window. No SettingsDialog navigation or theme stylesheet changes are needed.
- Audio projects the original Recording microphone inventory and shortcut
  capture. Microphone backups, conflict handling, capture suspension, and
  runtime callbacks use the existing handlers. Engine selection is an explicit
  Change link into the existing model page.
- Text projects the cleanup switch and calls `choose_cleanup_level()` for
  presets, including the existing custom-prompt replacement confirmation.
  Off hides all cleanup-dependent controls. Custom prompts have no selected
  preset and no illustrative preview until the user explicitly replaces them.
- Output projects the existing auto-paste, clipboard, and streaming controls.
  Streaming uses the original legacy-key migration and runtime callback.
- Provider copy resolves the saved provider/model pair and actual endpoint
  locality. API-key presence is labelled as configuration, never as a tested
  connection. Setup does not issue network probes, send sample text, download
  models, enable cleanup on entry, or reset existing preferences.

## Validation

Run from the repository root with the project's Python environment active:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
python -m pytest tests/test_guided_setup.py tests/test_bootstrap.py tests/test_setting_defaults.py tests/test_main_entrypoint.py -q
```

Validation passed: 68 targeted tests, Ruff lint/format checks for the new
Python files, and `git diff --check`.

The focused tests cover fresh and established installs, missing/corrupt
settings, durable dismissal/completion, custom-prompt confirmations, failed
save retries, microphone backups and stale inventory, shortcut conflicts and
capture cancellation, endpoint-specific privacy, streaming migration,
reopening, and narrow layouts at 130% font size.

Qt renders are in `.tmp/guided-setup-previews/` (ignored development outputs):
Audio, Text, Output, cleanup off, narrow Text at 130%, light Text, and the
invitation. They use an isolated data root and empty audio/model inventories;
the displayed provider needs a key. No live recording, external provider
request, or model download is part of this validation.

The combined branch passed 149 integration tests and 3,387 layout subtests,
including the guided setup, bootstrap, defaults, entrypoint, unified settings,
hotkey settings, and settings-binder suites. The original feature commit
remains on `codex/guided-setup`.
For laptop development with both the unified settings and guided setup, use
`codex/settings-and-guided-setup`. It has not been merged into `main`.
