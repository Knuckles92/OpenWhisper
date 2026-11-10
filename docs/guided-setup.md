# Optional guided setup

A first-run window that takes a new user from install to a working dictation:
a welcome page, three steps (Record → Clean up → Output), and a Ready page
with a box to try dictating in. Code: `ui_qt/dialogs/guided_setup.py`
(window), `ui_qt/widgets/microphone_check.py` (microphone test),
`services/guided_setup.py` (eligibility, state, model status), and
`UIController.offer_guided_setup` / `open_guided_setup`.

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
  After a finished setup the footer link reads "Close" instead of "Skip setup".

## Getting around

- The three step tabs are buttons: any step can be opened in any order.
- Enter presses Continue (or the focused button). Each page opens with
  focus on Continue, never on the header's "All settings" link.

## What each page does

- **Welcome** (auto-offer only): says what the three steps cover. The
  Settings window is not built until "Get started", so the offer costs
  nothing at startup.
- **Record:**
  - Microphone: the Settings inventory; saved backups are kept. "Test"
    opens the chosen microphone (`ui_qt/widgets/microphone_check.py`) and
    shows a level bar with a verdict: hearing you, nothing heard, or why it
    could not open. Nothing is kept, and the microphone closes on Stop, after
    15 seconds, or on leaving the step.
  - Shortcut: capture uses Settings' conflict checks. The help text follows
    the trigger mode and names the number pad for keypad keys, with a
    pointer to Change for keyboards without one.
  - Press or hold: the Hotkeys page's trigger mode (press to start and stop,
    or hold to talk).
  - Speech engine: says plainly whether the local model (and its runtime)
    is downloaded. "Download" fetches both without leaving setup, with a
    progress bar in the row; "Change" opens the model page.
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
  fallback works there on macOS and Linux.
  - What is still missing is listed above the box: a speech model that is
    not downloaded ("Download", with the same in-place progress; the page
    is then titled "Almost ready"),
    and cleanup that is on without its API key ("Add key").
  - The controller forwards `transcription_received` and
    `transcription_error_received`. A dictation made on this page turns the
    line under the box into "That worked" or the failure reason. If nothing
    was pasted into the box (auto-paste off, or blocked), the text is put
    there after a short wait.

Every control is a projection: changes go through the Settings window's
own save handlers, confirmations and runtime callbacks, and a failed save
restores both the projection and its source. Setup never probes a
provider, sends sample text, or resets preferences, and downloads nothing
until "Download" is pressed.

## The speech download

"Download" calls `ApplicationController.request_model_download` through the
controller, so the Hugging Face download policy, its consent dialog and the
runtime dialog (CPU or GPU) all still apply; declining either leaves setup
as it was. The controller forwards model and component progress to the
window. For a download started in setup it shows neither the pop-up
progress dialog nor Settings → Downloads, and downloads started anywhere
else are not claimed. An engine with no local model, or a build without
the downloader, falls back to opening the model page.

## Tests

`tests/test_guided_setup.py` covers:
- eligibility, and the welcome page (dismissal, retry, lazy Settings);
- every step's save path and failure restore;
- the microphone test, press or hold, step tabs and Enter;
- the in-place download: progress, the runtime, failure and retry;
- privacy and key copy, model status, the Ready page (what is missing,
  dictation feedback) and staying completed;
- layouts at 540 px / 130%;
- the controller's offer timing.
