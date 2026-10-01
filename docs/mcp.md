# Connect an agent to OpenWhisper

Open **Settings → MCP** and turn on **Enable MCP**. When the status changes to
**Running**, copy the setup prompt into your agent, or choose a manual setup
option. OpenWhisper includes the MCP server; users of the installed desktop app
do not need to install Python or start another process.

The page offers:

- **Server URL**, normally `http://127.0.0.1:8767/mcp`.
- **Access token**, hidden on screen with a separate copy button.
- **Agent setup prompt**, which tells the agent how to register the server and
  verify it. The prompt asks for the token separately.
- **Claude Code command**, ready to copy after replacing `<PASTE_TOKEN>`.
- **Client JSON (Cursor)**, to merge into the client's `mcpServers` configuration
  after replacing `<PASTE_TOKEN>`. Other clients may use a different config format.
- **Agent permissions**, with separate switches for retitling transcription
  history, retitling saved meetings, and accessing settings. Settings access has
  individual checkboxes for every preference an agent may change.

The URL uses **Streamable HTTP**, not a web page. Agents must send
`Authorization: Bearer <token>`. Browser clients and cloud-hosted agents cannot
connect directly to this local endpoint. The agent must run on the same computer
and support Streamable HTTP with custom headers.

The token is generated once and saved in the operating system credential store
under service `OpenWhisper`, account `OPENWHISPER_MCP_TOKEN`. It is separate from
the headless History API token and never enters the settings JSON. If the store
is locked or unavailable, the page explains the failure and offers **Retry**.
Copying the token puts it on the system clipboard. Manual client configuration
may store it in that client's config file; share it only with trusted agents.

MCP is off by default. Once enabled, it starts with the desktop app. Closing
Settings leaves it running; quitting OpenWhisper stops it. Turning it off rejects
new requests immediately while requests already in progress finish. To change
the port, turn MCP off, edit **Local port**, then enable it and update your agent's
URL. A port conflict appears as an error instead of an incorrect Running status.

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

History retrieval tools remain read-only. They reuse the [History API](agent-api.md), including
bounded cursor pagination, literal search, date filters, sanitized response
schemas, and exclusion of paired-computer records unless `include_remote=true`.
No tool starts recording, deletes history, edits transcript text, or exposes audio files. Any authorized
client can opt into remote records already saved in this database.

## Optional changes

All new permissions are **off by default**. Configure them in Settings → MCP,
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

**Allow settings access** permits reading the supported preference catalog.
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
Appearance, live-preview controls, and recording shortcut mode refresh in the
running desktop; other preferences apply to the next relevant operation.
Permission to enable cleanup uses the user's existing provider configuration.

An agent using a cloud model may send retrieved text to its provider. Server
instructions tell agents to treat saved content as untrusted source material,
retrieve original evidence, and cite record/segment IDs and timestamps.

## Development

Install the project's updated requirements (or run `uv sync`), then launch
OpenWhisper normally and use Settings. The MCP SDK and HTTP client are bundled
with native builds. The independent `python main.py --api` entry point continues
to serve only the History API on its own port; it does not enable MCP.

Implementation uses the [official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
and [Streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).
The MCP adapter calls the authenticated History API in-process through ASGI,
preserving its validation and read-only data boundary without a second listener.
MCP mutations use a separate permission-checked control service; the standalone
History API remains read-only. The desktop migrates existing databases to add
transcription display titles. MCP never creates or migrates a database itself.
Setup formats follow the [Claude Code](https://code.claude.com/docs/en/mcp)
and [Cursor](https://cursor.com/docs/mcp) documentation.
