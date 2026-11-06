# Native desktop command API

The Rune desktop starts `python -m services.native_desktop` and communicates
through private newline-delimited JSON pipes. Every request has an `id`, a
registered `method`, and an optional object `params`. A response contains the
same `id` and either `result` or `error`. Service events are separate messages.
The native service imports no Qt widgets or GUI toolkit.

`services.native_desktop.commands.methods()` returns the complete command
allowlist. `dispatch(context, method, params)` validates inputs and delegates
to the same domain services used by recording, automation, and the saved
libraries. The context provides the application controller, native UI adapter,
event emitter, snapshot, and shutdown. Tests can inject isolated settings,
history, database, meeting repository, and capture roots.

## Results and refreshes

Lists normally return `items`, `total`, `offset`, `limit`, and `has_more`.
History and meeting search accept `query`, `offset` (0–1,000,000), and `limit`
(1–200). Walkthrough lists use the same pagination fields. Detail commands
accept `id`; mutations return `ok` and usually `refresh`, a list of methods
whose cached results should be fetched again. Collection refreshes retain the
current `collection` parameter. Meeting/history edits can also return `item`,
the updated detail. Events notify the frontend when background work changes.

## Settings and models

`settings.schema` returns category groups and typed fields with labels,
descriptions, defaults, current values, choices, and optional bounds. Arrays
marked `editor: "lines"` use one entry per line. `settings.values` returns safe
preference values. `settings.update` accepts `{key,value}` or `{changes:{...}}`
and validates the entire request before writing. Engine changes refuse while
capture/transcription is active. Lowering audio retention asks a native prompt
before deleting retained recordings.

`settings.coverage` inventories every `SettingsKey`, including the native
surface or the reason a key is internal, migrated, or frontend-owned. This is
the parity inventory; unknown saved settings are never surfaced automatically.

`collections.list/save/delete` accept `collection`. Saves take a typed `item`.
The schema advertises `can_add` and `can_delete`; built-in server, tone, agent,
and device rows are edited in place. Libraries include dictionary, snippets,
profiles, transforms, actions, hotkeys, text endpoints, transcription servers,
custom speech sources, app overrides/tones, agent models, engine devices,
write-up styles, webhooks, programs, and filters. Domain validators enforce
unique entries, safe endpoints, and shortcut conflicts.

`models.list` reports installed/selected models and live download byte counts
and progress (0–1). `models.select/download/remove/cancel` and batch model
operations call the controller's real model lifecycle. `devices.list/select`
and `devices.priority` use available microphones and stable device priorities.

## Credentials and automation helpers

Credential rows contain only `name`, `configured`, and a null value.
`credentials.set {name,value}` writes a built-in or configured endpoint's slot
to the credential backend; an empty value removes it. No credential is returned
or written to preference JSON. Configured text and transcription endpoints
can name custom credential slots.

`collections.secret.copy {collection,id,renew?}` copies an action/webhook
signing secret directly to the native clipboard. Replacing a secret requires
a product prompt. The secret is absent from results, events, and snapshots.
`collections.test` exercises an action or webhook; `collections.tests` and
the completion event expose delivery status. Provider catalog and key checks
run asynchronously through `text_models.catalog`, `speech_servers.catalog`,
`credentials.verify`, and `operations.list`.

## Capture and saved libraries

`record.start` optionally selects `profile_id` or `action_id`;
`record.stop/cancel` control the current recording. `file.enqueue` takes
existing absolute `paths`, `relation`, optional `instructions`, and `combine`.
It creates a real `BatchUploadRequest`; `file.queue` reports queued, running,
done, failed, or canceled items. Active batches must be canceled before their
immutable queue is removed or cleared.

`history.list/detail/update/delete/export/playback` support search, transcript
versions, title/text edits, retained audio, and TXT/Markdown/JSON/SRT export.
Detail/playback resolve only safe retained recording paths. Remote-owned
history refuses unsupported local edits.

`meeting.start` takes `capture_mode` (`mic` or `both`), optional
`cloud_enabled`, `system_audio_policy` (`disabled`, `auto`, `required`), and
`intent`/`title`. Runtime capture/privacy prompts remain authoritative.
Meeting pause/resume/end, background work, and finalization controls use the
real meeting runtime.

`meetings.list/detail/current/update/delete/export/retry` expose saved
transcripts, reports, finalization, audio chunks, cards, participants, and
review state. `meetings.patch {id,ops}` permits only host editing operations.
`meetings.review {id,op,...}` starts or answers insight review;
`meetings.report.request {id,request}` and `.remove {id,report_id}` manage
tailored reports. Active stores are shared with the runtime; saved native
stores preserve worker/subscriber ownership and refresh external changes.
Delete/retry refuse while report or review work is running. Public results
omit repository capability tokens and endpoint credentials.

Walkthrough capture controls include pause, undo, screenshot removal/move,
note hold, and region/window/screen/delayed modes. `walkthroughs.list/detail`
read safe saved manifests. `walkthroughs.review` takes matching `texts`,
`keep`, and optional zero-based `order` arrays and rebuilds saved documents.
Exports include TXT, JSON, Markdown, PDF, DOCX, and a portable ZIP. A Markdown
export with `path` copies referenced screenshots into a sibling assets folder
and rewrites image links; an export without `path` returns its asset manifest.

Exports use exclusive file creation so an accidental duplicate request cannot
overwrite an existing document. The frontend chooses another destination
when the proposed path already exists.

## System services and prompts

The registered system module owns remote pairing/hosting/device sync, backup
creation/inspection/restore/schedules, update verification/download/apply,
statistics, guided setup, MCP status/token copying, and native window helpers.
Large operations report state and progress asynchronously. Backup restore
requests a native restart through the transport lifecycle.

Frozen native archives are a separate update channel. They report versions and
release notes with `manual_update: true`; legacy Qt installer downloads and
handoffs are rejected. Replace the complete native archive until a signed native
update feed is published. Source and existing Qt install channels retain their
established behavior.

Native prompts emit an ID, kind, title, message, and allowed choices.
`prompt.resolve {id,value}` validates the choice while the owning operation
waits. The transport remains responsive to snapshots and other RPCs during a
prompt. `scratchpad.get/update/flush` persist the existing UTF-8 scratchpad file
with coalesced atomic writes. `app.quit` refuses to discard unsaved notes after a
save failure; the editor stays available for recovery.
