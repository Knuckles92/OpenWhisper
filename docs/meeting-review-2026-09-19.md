# Meeting review — September 19, 2026

Meeting: `m_4759e58bda39`, “Testing the new Meeting Mode: capture accuracy, loopback, and voice commands.” Started at 12:24:07 Pacific; recording duration approximately 150 seconds.

## Artifacts checked

- `openwhisper.db`, opened read-only: meeting metadata, final state, 19 transcript segments, stored cards, and relevant entries in the 79-event audit trail.
- `meetings/m_4759e58bda/`: all 59 WAV headers and all three JSON manifests validated. There are 28 microphone chunks, 28 loopback chunks, two session recordings, and one mixed playback recording. Both channels' 56 database chunks finished ASR processing with status `done`.
- The two session WAVs are approximately 150 seconds; mixed playback is 150.238 seconds. All are mono, 16 kHz. This verifies container/metadata integrity, not an independent listening assessment of the recordings.
- `openwhisper.log`: voice-command application, capture completion, and finalization. Finalization saved 19 segments / 114 words and completed at 12:28:19 Pacific.

## Note capture failure

The committed transcript separates the request into:

- 59.174–60.214: “Assistant.” (`sg_f93eac1c2ad418b67f72`)
- 60.874–66.074: “Add a note that we need to get a thousand dollars for budget A.” (`sg_e179dcd4a05372c3f1dc`)

The wake-name join and command classification worked. The listener classified `note_this` at confidence 0.66, above the existing 0.60 threshold. However, the content parser accepted “take a note” and “make a note” but omitted “add a note.” It therefore fell back to two preceding transcript segments.

Audit event 2 at 12:25:14 saved **“This is gonna work. Insanely good.”** as the requested key point. This was a wrong-content save, not a database failure. Event 10 later corrected it to the budget statement; event 40 rewrote it into a summary of the voice-command test.

Changes:

- Parse “add a note” and common polite request prefixes, including a preamble split from its content across ASR rows.
- Preserve the dictated wording and cite only the wake/dictation segments when explicit content is present.
- Protect system-authored spoken notes from agent updates/removals and both live/offline finalization cleanup. They remain proposed, user-editable, removable, and reversible.
- Label that protection in the compact agent context.

The exact transcript and original classification confidence reproduce the old failure in an offline regression test and now produce **“we need to get a thousand dollars for budget A.”** Existing unnamed-speech, deictic-command, consent, duplicate, and stale-preview checks continue to pass. No classifier/API change or live cloud request was needed.

## Pulse finding and replay controls

Pulse independently captured the right utterance: `pulse_1_number` points to `sg_e179dcd4a05372c3f1dc` at 60.874 seconds, with probability 0.81. Its classification/anchor path is unchanged.

Pulse previously started the audio element in the Conversation rail or near the bottom of the archive. The controls could be outside the user's viewport. Selecting a Pulse or report timestamp now opens a shared floating replay panel with:

- Selected highlight text, category, and starting timestamp.
- Play/pause, a keyboard-accessible seek bar, elapsed/total recording time, and ±10-second controls.
- Close/Escape that stops audio and cancels pending autoplay, including while metadata loads.
- Loading/error feedback and retry; Pulse controls are disabled when an archive has no recording.

The panel controls the same audio element as the original inline player. Its portal is outside translucent/container layout ancestors, which otherwise clipped fixed positioning. Replay continues through the recording from the selected timestamp; it is not an independently trimmed audio clip.

## Other observed log entry

A Windows asyncio `ConnectionResetError` / WinError 10054 occurred during socket shutdown at 12:26:39. It occurred after the incorrect note had already been saved. Transcript cleanup and finalization subsequently completed successfully; the available evidence does not connect it to either reported issue.

## Verification

- 171 Python tests passed across voice commands, state persistence/protection, notes, previews, fast features, agent rejection handling, and refinalization.
- 39 dashboard tests passed, including pending-playback cancellation, same-element reuse, skip bounds, retry, unmount cleanup, and unavailable recordings.
- TypeScript checks and production build passed after `npm ci`; committed `webui/dist` assets were rebuilt.
- Headless Edge exercised active, ended, and history replay at 1280, 390, and 320 pixels: exact initial seek, play/pause, keyboard seek, skipping, close, another highlight, Escape, and failure/retry. Screenshots were visually inspected.
- Existing layout checks passed across nine viewport widths and short-window dialogs.
- Ruff correctness checks on changed Python files and `git diff --check` passed.

The saved meeting database and original artifacts were inspected read-only. The verification replays the recorded command text offline and uses synthetic silent audio for browser tests; it does not replace a fresh end-to-end microphone/cloud run. Restart the source app to load the Python changes and refresh the dashboard to use its rebuilt bundle.


## Follow-up: direct display in Meeting Notes

Spoken `note_this` requests now write directly to `live_notes`, the Meeting Notes panel highlighted in the follow-up screenshot. They appear as a timestamped “Requested note” block with a “Spoken note” label. Decisions and action commands keep their respective destinations.

The listener saves and broadcasts the note as soon as its committed transcript has been classified. It does not wait for the periodic AI notes pass. Provisional ASR previews only acknowledge the request; they cannot create a durable note. Feedback now says “Added to Meeting Notes” after persistence and publication succeed. A real SQLite regression checks that ordering, and a mounted UI regression checks the immediate item patch, timestamp, source link, and duplicate suppression.
