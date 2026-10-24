# macOS walkthrough validation - 2026-10-23

Tested the source checkout based on `30c87dcfee7c4875bb2aa67a6d0c44a3529b7176`,
with the fixes described below. Environment: Apple Silicon, macOS 26.6.2,
Python 3.12.13, Qt 6.11, one Retina display at 3420 x 2214 physical pixels
(2x scaling). Parakeet MLX was installed and performed the live transcription.

The application ran with `OPENWHISPER_DATA_DIR` pointing to
`.tmp/walkthrough-mac-qa`. Existing walkthroughs, history, and preferences
were not used as test fixtures. This was source-app testing, not a packaged,
signed release qualification.

The follow-up focus check switched the actual macOS foreground app while a
walkthrough recorded. Both floating windows stayed visible, and keeping or
canceling a screenshot returned foreground focus to that app.

## Issues found and fixed

1. **Recording overlays could enter screenshots.** macOS accepted
   `NSWindowSharingNone` and the application remembered the window as excluded,
   but the real Qt screen grab still contained its magenta test pixels.
   Native API success was therefore insufficient evidence of exclusion.
   Mac Snap now uses the existing hide/capture/restore path. A native pixel
   check changed from the marker's `#ff00ff` to the background's `#194754`.
   A follow-up check with the real animated pill and translucent column
   caught a second capture condition: their old pixels could remain after
   both Qt and AppKit reported the windows hidden. Cocoa capture now waits
   300 ms for the composed frame to settle. Pixel checks at the old positions
   of both real widgets then matched the unobstructed background.
   The desktop smoke script now exercises the production exclusion decision
   and checks the actual translucent widgets before accepting the pixels.
   The guide and changelog no longer promise
   exclusion from other apps' screen recordings or shares.

   Apple also describes the flag as a legacy constant that macOS no longer
   uses: [NSWindow.SharingType.none](https://developer.apple.com/documentation/appkit/nswindow/sharingtype-swift.enum/none).

2. **The live column and ready card could disappear in another app.** Native
   inspection showed that these Tool windows had `hidesOnDeactivate = true`,
   unlike the recording pill. Both now set `WA_MacAlwaysShowToolWindow`.
   The native smoke check verifies that both resulting NSWindows have
   `hidesOnDeactivate = false`.

## Live computer-use checks

These used the actual application UI, mouse actions, keyboard events,
microphone recording, and installed speech engine, alongside read-only
diagnostics of the application's own floating windows.

| Flow | Observed result |
| --- | --- |
| Start and finish from Quick Record | Recording, transcription, saved documents, and History entry completed. |
| Configured shortcuts while OpenWhisper is active | Start, screenshot, keep, finish, and cancel worked. |
| Floating Snap and Done | Mouse clicks worked. Done kept the screenshot still being annotated and finished the recording. |
| Actual foreground-app switch during recording | Opened Preview's native Help command. macOS reported `com.apple.helpviewer` in front and OpenWhisper inactive, while both the pill and column remained natively visible. |
| Foreground focus after screenshots | Escape and Enter each returned foreground focus to Help Viewer; both floating windows stayed visible and recording continued. |
| Retina crop and annotation | Box, arrow, text, numbered step, pen, highlighter, and color controls were exercised; saved PNGs retained annotations. |
| Undo/redo | Control+Z and Control+Shift+Z worked. |
| Screenshot outcomes | Enter and pressing Screenshot again kept a shot; Escape discarded a shot while recording continued. |
| Cancel with a screenshot open | Annotation closed, recording canceled, and no completed walkthrough was added. |
| Finish with a screenshot open | The shot was kept before export. |
| Multiple screenshots | Two shots produced three ordered transcript sections; the canceled shot was absent. |
| Saved review | Edited transcript and omitted a screenshot; saving regenerated the documents and retained the omitted shot as a selectable item. |
| Unsaved review | Copy was blocked with a request to save changes. |
| Markdown clipboard | Pasted into TextEdit with corrected words and absolute local image paths. |
| PDF clipboard | Pasted into an isolated Finder folder as a file; its bytes matched the source PDF. |
| PDF opening and rendering | Opened in Preview; also rendered locally and checked for legible text and embedded images. |
| Document settings | Changed title, selected A4, and disabled the details, reading note, and captions; the resulting PDF matched those choices. |
| Interrupted recording | Force-stopped the isolated process during recording, restarted, and used Recover draft; audio and screenshot became a document. |
| Claude Code write-up | Real installed agent produced Markdown and a PDF with the screenshot. |
| Codex write-up | Real installed agent produced Markdown and a PDF with the screenshot. |
| Write-up cancellation | Cancel waited for safe termination, restored controls, and preserved the previous documents. |
| OpenCode failure | Reported that its model provider needs sign-in and preserved the existing documents. |
| Recording after exclusion fix | Keeping a screenshot continued recording; finishing generated a document. |
| Transcript-only walkthrough | Spoken words with zero screenshots still generated a PDF and Markdown document. |
| Standalone screenshot | Reported Screenshot copied and pasted into Finder as a PNG; TextEdit recognized graphics and offered RTFD conversion, which was canceled. |

Microphone capture included room audio as well as synthetic narration. These
checks validate the recording/transcription workflow, not recognition accuracy.

The focus check used a Mac Command-click on Snap to avoid the driver's
ordinary click path activating OpenWhisper before delivering the click.
Read-only tracing confirmed Help Viewer was the app remembered by the
production focus-handoff code, and confirmed that the OS foreground app
returned to Help Viewer after both screenshot outcomes. This establishes
the visibility and screenshot handoff results without assuming that targeted
keyboard input proves which app is in front.

## Automated and native checks

- Focused walkthrough, screenshot, recorder, and overlay suite: **422 passed**
  after the fixes.
- Relevant UI suite with `OPENWHISPER_UI=omarchy`: **86 passed**; the default
  Mac/Classic run also passed.
- Full suite after the final fixes: **9,885 passed, 48 skipped, one failed**,
  plus **4,724 passing subtests**. The same failure existed before the fix.
- The failure is
  `test_agent_mcp.py::test_tailscale_listener_keeps_local_access_and_releases_both_ports`.
  It assumes `127.0.0.2` can be bound. An independent socket probe on this
  Mac returns `OSError(49, "Can't assign requested address")`; `127.0.0.1`
  works. Network configuration was not changed to make this test pass.
- `scripts/check_walkthrough_desktop.py`: real Cocoa capture, pixel exclusion,
  annotation, keeping, review, and PDF generation passed. It additionally
  checks native non-activating panels, window levels, and the two cards'
  visibility policy. The pixel checks include the actual animated recording
  pill and translucent walkthrough column.
- Ruff's repository-wide `F` checks and `git diff --check` passed.

## Remaining qualification limits

- OpenCode's successful write-up requires signing in to its configured
  provider. No authentication settings were changed.
- Background global-shortcut delivery and an ordinary mouse click on Done
  without focus theft remain driver-limited: targeted keys do not exercise
  the global event path, and the ordinary click path activates the target
  app itself. Done completed the recording and export, but that driver path
  cannot certify the control's non-activation behavior. The foreground switch,
  floating visibility, and screenshot focus return were verified separately.
- Only one display was available. Multiple displays, mixed scaling, and
  another app's dedicated full-screen Space were not exercised interactively.
- Screen Recording permission was already granted. Permission-denied and
  request/settings routing have automated coverage; the existing grant was
  not revoked to recreate first-run permission prompts.
- File-chip dragging, destructive retention/deletion, backup restoration,
  other speech engines, and signed-app permission identity were not certified
  through computer use in this pass. Relevant unit coverage ran in the full
  suite.

During setup, an invalid backend ID (`local`, rather than `local_whisper`)
in the hand-written QA configuration caused Settings to abort with a KeyError.
The isolated configuration was corrected to the installed `parakeet_mlx`
backend. This is a separate invalid-settings robustness issue; the regular
backend picker does not write that value.
