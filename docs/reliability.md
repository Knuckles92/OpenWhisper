# Recording and transcription reliability

## Recording failures

Quick Record refuses to report a successful transcription when capture lost
audio or recording storage did not finish. Recoverable journal data stays on
disk. When a partial WAV can be saved safely, the failure flow retains it in
history; otherwise the journal remains available for startup recovery.

Meetings record the first storage or queue failure in their saved capture state.
The desktop and meeting dashboard show that audio is incomplete. Ending a
meeting with a capture failure leaves it needing recovery, even if transcription
of the available chunks finished. Recovery can process saved audio; it cannot
reconstruct audio that capture dropped. After recovery finishes, such a meeting
keeps a failed status and its incomplete-audio warning.

A failed meeting recovery scan displays a warning with **Retry scan** and
**Later**. It does not mean there are no interrupted meetings. Fix unavailable
storage or database access and retry from the warning.

## Slow or unavailable transcription

The meeting ASR worker holds at most 64 chunk references, including the chunk
being decoded. Capture writes each WAV and registers it in SQLite first. When
the memory queue fills, additional chunks stay on disk and the worker loads
them in bounded pages. A status message explains that captions are catching up.
This limit bounds queued references; saved recordings still require disk space.

Transient decoding failures have three attempts, separated by 0.5 and 2 seconds.
Disconnected hosts use increasing delays from 5 to 60 seconds without spending
that decoding budget. A busy host also keeps the audio pending. Stopping the
meeting interrupts retry waits.

Missing or unreadable WAV files, unavailable local models, and exhausted decode
attempts require an explicit recovery retry after the cause is fixed. Restarting
the app alone does not reset the failed chunk's attempt count. The recovery
action resets the budget for that requested attempt.

## Late transcripts and shared hosts

Insights and automatic notes each store their own cursor in SQLite. New segments
receive an insertion sequence, independent of the time when their speech
occurred. The scheduler sends at most 300 segments in a checkpoint and advances
that consumer's cursor only after success. Failed checkpoints are retried and
may be delivered again; this is not an exactly-once guarantee for external AI
responses. Deliberately resuming intelligence starts at the current transcript
position, preserving the choice not to share the preceding period.

The shared speech host accepts at most 12 simultaneous WebSocket handlers and
admits at most 2 costly requests at once, with 4 waiting in FIFO order. A queued
request waits at most 30 seconds. Overload returns a `busy` error with a retry
delay; older clients still receive an ordinary protocol error. Pairing and
lightweight connection checks share the connection limit.

A decode that has already entered a synchronous speech engine runs until the
engine returns, even if its client disconnects. Its capacity slot remains held
for that work; disconnected requests still waiting in the queue are removed.

See [Backup and restore](backup-restore.md) for portable data protection and
[Release health](release-health.md) for automated and hardware release checks.
