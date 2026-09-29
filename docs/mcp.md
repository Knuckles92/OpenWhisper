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

All tools are read-only. They reuse the [History API](agent-api.md), including
bounded cursor pagination, literal search, date filters, sanitized response
schemas, and exclusion of paired-computer records unless `include_remote=true`.
No tool starts recording, edits history, or exposes audio files. Any authorized
client can opt into remote records already saved in this database.

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
Setup formats follow the [Claude Code](https://code.claude.com/docs/en/mcp)
and [Cursor](https://cursor.com/docs/mcp) documentation.
