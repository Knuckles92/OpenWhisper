# Connect an agent to OpenWhisper

Open **Advanced Settings → Automate → API & MCP**
and turn on **Enable MCP**. When the status changes to **Running**, copy the setup
prompt into your agent, or choose a manual setup option. OpenWhisper includes the
MCP server; users of the installed desktop app
do not need to install Python or start another process.

Connection and permissions appear side by side, or stack in narrow windows.
The page follows the app's light, dark, system, or Omarchy theme and offers:

- **This computer / Another computer**, choosing where the assistant runs.
- **Advanced connection → Server URL**, normally `http://127.0.0.1:8767/mcp` for
  this computer, or the OpenWhisper host's Tailscale address when remote access
  is enabled.
- **Access token**, hidden on screen with a separate copy button.
- **Setup prompt**, which tells the agent how to register the server, load
  OpenWhisper's bundled Agent Skill, and verify it. Copy it directly or expand
  its preview. **Copy skill prompt** copies a one-paste prompt focused on
  installing both the MCP server and the `openwhisper` skill package.
- **Claude Code**, with a command ready to paste into a terminal.
- **Cursor**, with JSON to merge into the client's `mcpServers` configuration.
  Other clients may use a different config format.
- **ChatGPT**, with TOML to merge into the desktop app's `~/.codex/config.toml`.
  It uses the selected local or Tailscale address.

Every copy includes the access token, so there is nothing to fill in. The
on-screen preview shows `••••••••••••` in its place. If the token isn't
available, the copy has `<PASTE_TOKEN>` (or the prompt asks for the token) and
you paste it from **Access token**.
- **Permissions**, with separate switches for **Rename dictations**,
  **Rename finished meetings**, **Attach agent notes & tags**, and **Read app
  preferences**. Expand **Choose individual preferences** and its categories
  (with per-category **Allow all** toggles) to allow individual setting writes
  across Dictation, Appearance, Personalization, Cleanup, Walkthroughs,
  Meetings, History, and Updates.
  Reading preferences does not grant permission to change them. Permissions can
  be configured while MCP is off and apply to every assistant using the token.

The URL uses **Streamable HTTP**, not a web page. Agents must send
`Authorization: Bearer <token>`. Browser clients and cloud-hosted agents cannot
connect directly to a localhost endpoint. The agent must support Streamable HTTP
with custom headers.

## Agents on another computer

`127.0.0.1` always refers to the agent's own computer. Copying that URL from a
host such as `jed` into an agent on your Windows computer will not reach `jed`.
The same-computer prompt identifies its originating computer and tells the agent
to request remote setup instead of enabling an unrelated local instance.

On the computer running OpenWhisper, turn MCP off, expand **Advanced connection**,
enable **Allow agents over Tailscale**, then turn MCP on again. Choose
**Another computer** in Settings → API & MCP to copy its remote URL, prompt, or manual
configuration. Both computers must be connected to the same Tailscale network. Host mode displays
the Tailscale URL and its copy-prompt button uses that URL when enabled.

For example, a host with Tailscale address `100.82.22.3` offers
`http://100.82.22.3:8767/mcp`. Use the access token from **that host's** Settings
→ MCP. OpenWhisper must stay running on the host; it does not need to run on the
agent's computer. Localhost remains available for agents on the host itself.

Remote access is off by default. When enabled, MCP binds only to localhost and
the computer's current Tailscale IPv4 address. Requests require the same bearer
token, accept only configured host names, reject browser origins, and accept
only local or Tailscale peer addresses. Tailscale encrypts the connection between
computers. OpenWhisper does not bind to the LAN or all network interfaces. If
Tailscale is disconnected or its address changes, reconnect it and restart MCP
before copying a new remote URL. An SSH tunnel is an alternative for the local
listener; substituting a LAN address alone will not make it reachable.

## Access token and lifecycle

The token is generated once and saved in the operating system credential store
under service `OpenWhisper`, account `OPENWHISPER_MCP_TOKEN`. It is separate from
the headless History API token and never enters the settings JSON. If no store
is found, or it is locked, the page says so and **Retry** checks again.
Copying the token puts it on the system clipboard. Manual client configuration
may store it in that client's config file; share it only with trusted agents.

MCP is off by default. Once enabled, it starts with the desktop app. Closing
Settings leaves it running; quitting OpenWhisper stops it. Turning it off rejects
new requests immediately while requests already in progress finish. To change
the port, turn MCP off, expand **Advanced connection**, edit **Local port**, then
enable it and update your agent's URL. A port conflict appears as an error instead
of an incorrect Running status.

## Available tools

| Tool | Purpose |
| --- | --- |
| `get_status` | Verify that saved history is available |
| `search_history` | Search dictations, meeting titles, and transcript segments |
| `list_transcriptions` | Browse saved dictations and file transcriptions |
| `get_transcription` | Read the original and cleaned text |
| `list_meetings` | Browse meetings, optionally by title |
| `get_meeting` | Read meeting metadata |
| `list_meeting_segments` | Read transcript segments in time order |
| `get_meeting_segment` | Resolve one evidence segment to source text |
| `get_meeting_insights` | Read saved summary, notes, actions, decisions, and reports |
| `get_capabilities` | Check current action permissions and writable setting keys |
| `get_settings` | Read supported preferences, valid values, effects, and write permissions |
| `update_settings` | Atomically change individually permitted preferences |
| `retitle_transcription` | Set a local transcription's display title |
| `retitle_meeting` | Set a finished local meeting's title |
| `get_history_digest` | Fetch a bounded chronological bundle of dictations, meetings, and (when allowed) walkthroughs with text and annotations (`only_unannotated=true` for incremental organizing) |
| `get_record_annotation`, `list_annotations`, `list_tags` | Read saved agent summaries, notes, tags, and `external_refs` on transcriptions, meetings, and walkthroughs |
| `set_record_annotation`, `delete_record_annotation` | Attach, merge, or clear agent summaries, notes, tags, and `external_refs` on local records without changing transcript text; needs **Attach agent notes & tags** |
| `organize_records` | Batch-retitle and/or annotate up to 25 local records in one call (supports `dry_run=true`) |
| `get_live_state` | Whether OpenWhisper is recording, in a meeting, or in a walkthrough, plus live permissions |
| `list_actions`, `list_profiles`, `list_transforms` | Your saved [actions](automation.md#actions), cleanup profiles, and text transforms |
| `recent_events` | Recent [live events](automation.md#events), with a gap flag; needs **Follow live events** |
| `run_action`, `run_transform` | Run a saved action or text transform on text; needs **Run actions** |
| `notify_user` | Show a short notice near the pointer; needs **Run actions** |
| `start_dictation`, `stop_dictation`, `cancel_dictation` | Record for dictation, an action, a cleanup profile, or voice routing; needs **Start recordings** |
| `get_meeting_live_state`, `get_meeting_dashboard_state`, `start_meeting`, `pause_meeting`, `resume_meeting`, `end_meeting`, `background_meeting`, `retry_meeting_finalization`, `manage_recovered_meeting`, `open_meeting_view`, `update_live_meeting`, `manage_meeting_card_item`, `manage_meeting_question`, `update_meeting_transcript_or_speaker`, `request_meeting_note_adjustment`, `request_meeting_custom_report`, `undo_live_meeting_action` | Inspect live Meeting Mode status, duration, job progress (scheduler, finalization steps, background jobs, running reports), and full dashboard state; control lifecycle, live metadata/AI toggle, cards, Q&A, speakers/segments, note adjustments, custom reports, and undo; mutations need **Control Meeting Mode** |
| `list_walkthroughs`, `get_walkthrough` | Browse and read saved walkthroughs by `#001` reference, `latest`, or folder ID (with optional screenshots); needs **Read saved walkthroughs** |
| `start_walkthrough`, `finish_walkthrough`, `cancel_walkthrough`, `set_walkthrough_paused`, `snap_walkthrough`, `choose_walkthrough_snap_mode`, `edit_walkthrough_shots`, `run_walkthrough_writeup`, `take_screenshot` | Control walkthrough recording, screenshots, shot order, and AI write-ups; needs **Control walkthroughs & screenshots** |
| `get_personalization_summary`, `list_dictionary`, `add_dictionary_term`, `bulk_add_dictionary_terms`, `remove_dictionary_term`, `list_snippets`, `upsert_snippet`, `bulk_upsert_snippets`, `delete_snippet`, `get_app_styles`, `set_app_style_tone`, `set_app_style_override`, `remove_app_style_override`, `list_walkthrough_styles`, `upsert_walkthrough_style`, `delete_walkthrough_style`, `list_languages`, `set_active_language`, `set_quick_record_profile` | Read a full personalization snapshot and manage dictionary terms (including bulk seed + `dry_run`), snippets, app style tones/overrides, walkthrough write-up styles, languages, and default Quick Record profile; mutations need **Manage dictionary, snippets & styles**. Built-in MCP prompts `personalize_from_context` and `personalize_from_history` guide assistants to seed personalization from past chats, projects, or dictation history. |
| `get_engine_status`, `list_models`, `select_model`, `download_model`, `list_audio_devices`, `select_audio_device`, `get_remote_status`, `reconnect_remote`, `transcribe_audio_file`, `control_window` | Inspect and control speech engines/models, microphones, remote host connection, local audio file transcription, and desktop windows/Scratchpad; mutations need **Control models, devices, files & windows** |

History retrieval tools remain read-only. They reuse the [History API](agent-api.md), including
bounded cursor pagination, literal search, date filters, sanitized response
schemas, and exclusion of paired-computer records unless `include_remote=true`.
No tool deletes history, edits transcript text, or exposes raw audio files.
`recent_events` follows **Follow live events**, which is on by default like
reading history: it returns recent dictations, actions and meeting lines.
Running actions and showing notices need **Run actions**, recording needs
**Start recordings**, and running an Agent hotkey or Claude Code session
action, or a recording routed to one, with a program's words (sent, or
changed by its text filter) also needs **Send words to my coding agents**; all three are off by
default (see [Actions, live events and hooks](automation.md#live-api)). Any authorized
client can opt into remote records already saved in this database.

## Bundled Agent Skill (SEP-2640 & Installable Package)

OpenWhisper ships an [Agent Skills](https://agentskills.io)-compatible skill
package (`skills/openwhisper/`) directly with the desktop app and serves it
over MCP so assistants know how OpenWhisper works, how to organize and annotate
history, and how to use every tool safely:

- **MCP Skills extension (SEP-2640)**: `initialize` and `server/discover`
  advertise `capabilities.extensions["io.modelcontextprotocol/skills"]` alongside
  `capabilities.resources`. Clients call `skills/list` and `skills/get` to read
  the parsed frontmatter and SHA-256 file manifest, then fetch files via
  `resources/read`.
- **Standard MCP Resources**: every skill file is also exposed as a read-only
  MCP resource (`skill://openwhisper/SKILL.md`,
  `skill://openwhisper/references/tools.md`,
  `skill://openwhisper/references/organizing-and-notes.md`,
  `skill://openwhisper/references/workflows.md`, plus `skill://index.json`).
- **One-step ZIP package**: authenticated clients can download the packaged
  skill directory from `GET /v1/skills/openwhisper.zip` (and list metadata at
  `GET /v1/skills`) using the same Bearer token. **Settings → API & MCP → Copy
  skill prompt** copies a ready-to-paste prompt that connects MCP and installs
  the skill automatically.

## Agent Notes, Tags & External Links

Enabling **Attach agent notes & tags** lets an assistant attach structured
metadata (`summary`, `notes`, `tags`, `external_refs`, `author`) to any local
transcription, meeting, or walkthrough without modifying original speech.
In the desktop UI, annotated records show a subtle **✦ Note** pill and an
**Agent Notes** button inside the past transcription dialog (or via right-click
**Agent Notes…** on any past transcription, meeting, or walkthrough).
Unannotated records remain visually unchanged. Agents that maintain their own
external database (such as Obsidian, Notion, or a local knowledge store) can
store lightweight `{system, id, label?, url?}` pointers in `external_refs`
keyed to OpenWhisper's immutable record IDs.

## Query paired clients directly

On each client, enable **Settings → API & MCP → Share this computer's history through
the paired host's MCP**. This control stays available on either MCP tab, including
when remote MCP administration is disabled. It is off by default and is independent
of the storage choice and **Allow paired computers to manage MCP**. Existing history
sharing preferences are preserved. Both the host and client need a version with this feature.
The client opens a separate, authenticated connection to its paired host, so
history queries can run alongside dictation and meetings without opening an
inbound port on the client.

Use `include_clients: true` on `search_history`, `list_transcriptions`, or
`list_meetings` to combine host history with live client results. Include
`include_remote: true` to search copies already stored on the host as well.
The same record stored by **Both** and returned by its client appears once.
An optional `device_id` restricts a list or search to one client.

Live results include `device_id`; pass it to `get_transcription`, `get_meeting`,
`list_meeting_segments`, `get_meeting_segment`, and `get_meeting_insights` when
retrieving that client's originals. Search resources already include this
routing parameter. With `include_remote: true`, a matching stored host copy
can answer a device-specific read while the client is offline.

`get_status` and federated list/search pages include `clients`, with each
device's name, ID, and status: `online`, `sharing_disabled`, or `unavailable`.
Offline clients and timeouts yield partial results with explicit availability,
so an empty page does not imply that every client's history was searched.
Turning off the client's permission or forgetting the host cuts off new live
queries. Agents cannot enable that permission themselves.

Direct queries need the client app to be running and reachable. Choose **Both**
under **Where records are kept**, and **Copy existing records** for earlier
history, to keep copies searchable when the client is offline. Live queries
read transcripts and saved insights; they do not transfer audio or save records
to the host's database. Federated cursors retain their original client set,
filters, and page size for ten minutes; restart the query after they expire.

## Optional changes

These permissions are **off by default**. Configure them in Settings → API & MCP,
even while the server is off. The server checks the saved permissions on every
call, so changing them does not require restarting or reconnecting. Revoking a
permission blocks subsequent calls; a change already in progress may finish.
Every client using the token shares these permissions. Agents cannot change
their permissions, the MCP connection, credentials, cloud-consent decisions,
network sharing, or recording retention.

Retitling transcriptions preserves the original source filename, transcript,
and raw text. The separate title appears in History and is searchable through
both the desktop and MCP. Meeting retitling updates the saved state and any
open dashboard. Titles must contain 1–200 characters without control characters.
Only local records can be changed; active, paused, recovering, or finalizing
meetings must finish first.

**Read app preferences** permits reading the supported preference catalog.
Each preference has its own checkbox granting write access. The initial catalog
covers dictation output, recording shortcut mode, language, live preview,
appearance, cleanup instructions and reasoning, meeting report views, deletion
confirmation, and update notifications. `get_settings` returns exact keys,
choices, bounds, current values, and when changes apply. Credentials and internal
settings are excluded.

For example, after the user permits changes to clipboard copying and theme:

```json
{"changes": {"copy_clipboard": false, "ui_theme": "light"}}
```

`update_settings` validates and authorizes every key before committing a single
settings transaction. An invalid or disallowed key rejects the whole request.
Successful responses include the changed values and their previous values.
Theme values are resolved preferences: an unset theme follows the desktop default
(`omarchy` on Omarchy, otherwise `dark`). The theme entry in `get_settings` also
reports `inherited` (no saved override) and `resettable: true`. Only `ui_theme`
accepts `null` to remove its override; other preferences still require a value.
For example, `{"changes": {"ui_theme": null}}` resumes the desktop default.
Responses include a `restore` object to pass back as `changes`: it uses `null`
for an inherited theme, preserving inheritance instead of pinning the resolved
theme. Theme values in `updated` and `previous` are resolved preferences.
Appearance, live-preview controls, and recording shortcut mode refresh in the
running desktop; other preferences apply to the next relevant operation.
Permission to enable cleanup uses the user's existing provider configuration.

An agent using a cloud model may send retrieved text to its provider. Server
instructions tell agents to treat saved content as untrusted source material,
retrieve original evidence, and cite record/segment IDs and timestamps.

## Development

Run `uv sync --locked`, then launch OpenWhisper normally and use Settings.
The MCP SDK and HTTP client are bundled with native builds.
The independent `uv run --locked python main.py --api` entry point continues
to serve only the History API on its own port; it does not enable MCP.

Implementation uses the [official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
and [Streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).
The MCP adapter calls the authenticated History API in-process through ASGI,
preserving its validation and read-only data boundary without a second listener.
MCP mutations use a separate permission-checked control service; the standalone
History API remains read-only. The desktop migrates existing databases to add
transcription display titles. MCP never creates or migrates a database itself.
Setup formats follow the [Claude Code](https://code.claude.com/docs/en/mcp),
[Cursor](https://cursor.com/docs/mcp), and
[OpenAI MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) documentation.
