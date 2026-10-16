# Optional guided setup

A first-run window that takes a new user from install to a working dictation:
a welcome page, three steps (Record → Clean up → Output), and a Ready page
with a box to try dictating in. Code: `ui_qt/dialogs/guided_setup.py`
(window), `services/guided_setup.py` (eligibility, state, model status),
and `UIController.offer_guided_setup` / `open_guided_setup`.

## When it appears

- Only a launch that started without a settings file *and* without prior
  user data (database, legacy history, scratchpad, recordings, meetings)
  is invited. `initialize()` runs in bootstrap before anything writes
  settings and seeds `guided_setup_state`:
  `pending`, `started`, `skipped`, `completed` or `existing`.
  Existing installs get `existing`, and corrupt settings files are left alone.
- After first paint, the controller waits 750 ms. It then keeps yielding
  every second while a modal dialog is up (model-download consent, meeting
  recovery) or the main window is hidden or minimized, as when the app
  starts in the tray. Update health-check launches never offer it.
- Only `pending` is offered, once. "Not now", closing the window, Escape,
  "Skip setup" and "All settings" save `skipped`, and reaching the Ready
  page saves `completed`. Reopening a completed setup never downgrades it.
  A failed save shows an error and leaves the window open to retry.
- File → Guided setup… reopens it at any time, straight to the first step.

## What each page does

- **Welcome** (auto-offer only): says what the three steps cover. The
  Settings window is not built until "Get started", so the offer costs
  nothing at startup.
- **Record:**
  - Microphone: the Settings inventory; saved backups are kept.
  - Shortcut: capture uses Settings' conflict checks. The help text follows
    the trigger mode and names the number pad for keypad keys.
  - Speech engine: says plainly whether the local model (and its runtime)
    is downloaded. "Get it now" opens the model page.
- **Clean up:**
  - The cleanup switch. It starts off on a new install, and setup never
    turns it on.
  - The privacy line (amber when text leaves the computer).
  - The provider with its key status. "Add key" opens API keys; a saved key
    is never reported as a tested connection.
  - Light / Medium / High with an illustrative example. A custom prompt
    shows no tile selected and asks before being replaced.
- **Output:** auto-paste, clipboard and live transcript, with the
  platform's paste caveats (Hyprland, macOS Accessibility).
- **Ready:** the shortcut as a keycap, a try-it box, and a line on where
  the text will land given the output choices. The window is marked as
  OpenWhisper's own (`openwhisperHotkeyWindow`), so the in-app shortcut
  fallback works there on macOS and Linux. A missing model is called out
  with "Get it now".

Every control is a projection: changes go through the Settings window's
own save handlers, confirmations and runtime callbacks, and a failed save
restores both the projection and its source. Setup never probes a
provider, sends sample text, downloads a model, or resets preferences.

## Tests

`tests/test_guided_setup.py` covers:
- eligibility, and the welcome page (dismissal, retry, lazy Settings);
- every step's save path and failure restore;
- privacy and key copy, model status, the Ready page and staying completed;
- layouts at 540 px / 130%;
- the controller's offer timing.
