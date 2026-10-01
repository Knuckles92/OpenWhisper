# History API for agents

OpenWhisper's opt-in local HTTP API lets scripts and agent clients search saved
dictations and meetings, then retrieve the source text and meeting insights.
The desktop app also provides an opt-in [MCP server](mcp.md) in Settings → MCP.
An installable agent skill remains a follow-up integration.

The API runs as a separate, headless process using the app's existing FastAPI
and SQLite dependencies. It does not load speech models or require an active
meeting. It reads committed database changes while the desktop app runs and
also works with the desktop app closed. Normal desktop startup leaves it off.

## Start from source

Use the same virtual environment as OpenWhisper. Open the desktop app at least
once so it creates or migrates its database. Generate a random bearer token and
start the server:

**PowerShell:**

```powershell
$env:OPENWHISPER_API_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python main.py --api
```

**macOS / Linux:**

```bash
export OPENWHISPER_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python main.py --api
```

The service listens on `http://127.0.0.1:8766`. Use `--port 8767` to change the
port, or `--database /path/to/openwhisper.db` to select another existing database.
Without `--database`, it uses the same data-directory rules as the desktop app,
including `OPENWHISPER_DATA_DIR`. A development checkout defaults to its own data,
which may differ from an installed app. Stop with Ctrl+C. The equivalent module
entry point is `python -m services.agent_api`.

The token is required, must contain 32–512 printable ASCII characters without
whitespace, and is read from `OPENWHISPER_API_TOKEN` at startup. Give your local
client the same token through its credential configuration. Restart with a new
token to revoke the old one. The service does not save or print the token, and
does not accept it in URLs or cookies.

## Call it

**PowerShell, in the shell where the token is set:**

```powershell
$headers = @{ Authorization = "Bearer $env:OPENWHISPER_API_TOKEN" }
Invoke-RestMethod -Headers $headers -Uri 'http://127.0.0.1:8766/v1/search?q=launch&limit=5'
```

**curl:**

```bash
curl --get 'http://127.0.0.1:8766/v1/search' \
  -H "Authorization: Bearer $OPENWHISPER_API_TOKEN" \
  --data-urlencode 'q=launch' --data-urlencode 'limit=5'
```

All endpoints, including the machine-readable OpenAPI contract at
`GET /openapi.json`, require `Authorization: Bearer <token>`. There is no browser
documentation UI or browser CORS support in this version. The OpenAPI contract
includes bearer authentication, response schemas, validation bounds, and stable
operation IDs for client generation.

| GET endpoint | Result |
| --- | --- |
| `/v1/status` | API version, read-only status, database availability |
| `/v1/transcriptions` | Bounded previews of saved dictations and file transcriptions |
| `/v1/transcriptions/{id}` | Full cleaned and raw text, source name, model metadata |
| `/v1/meetings` | Meeting titles, lifecycle status, timestamps, state sequence |
| `/v1/meetings/{id}` | One meeting's metadata |
| `/v1/meetings/{id}/segments` | Transcript segments in time order |
| `/v1/meetings/{id}/segments/{segment_id}` | One citation's source text and timestamps |
| `/v1/meetings/{id}/insights` | Saved summary, participants, notes, decisions, actions, questions, reports, and evidence IDs |
| `/v1/search` | Excerpts across transcriptions, meeting titles, and meeting transcript segments |

### Search and filters

`/v1/search` requires `q` (1–500 characters, not all whitespace).
`kind=all` is the default; `kind=transcription` searches dictation/file history,
and `kind=meeting` searches meeting titles and segments. Matching is a literal
substring, with SQLite's ASCII case-insensitive matching. Characters such as
`%`, `_`, and quotes are literal; there is no query language, stemming, semantic
ranking, or cloud call. Non-ASCII case folding is not supported in this version.

`/v1/transcriptions` accepts optional `q` matching cleaned text, raw text, and
source names. `/v1/meetings` accepts optional `q` matching titles. Search currently
does not match generated meeting summaries or notes; fetch `/insights` after
finding the relevant meeting.

History lists and unified search accept `since` (inclusive) and `before`
(exclusive). Supply ISO 8601 timestamps with a timezone, such as
`2026-09-01T00:00:00Z`. Meeting filters use meeting start time; transcription
filters use the saved timestamp. Offsets are normalized for filtering and
ordering. Legacy stored timestamps without an offset are treated as UTC; response
timestamps retain the stored representation.

By default, only records made on this computer are included. Add
`include_remote=true` to include records this database holds for paired computers.
The option also applies to individual reads, segments, and insights. It does not
fetch records that exist only on another computer. This is a selection option,
not a separate permission: the local bearer token can read the whole selected
database. Returned `origin_device_id` identifies a remote record's provenance.

### Pagination and citations

List and search responses use:

```json
{
  "items": [],
  "next_cursor": null
}
```

Set `limit` from 1 to 100 (default 20). When `next_cursor` is present, pass it as
`cursor` with the same search and filter parameters. Cursors are opaque and
bound to the endpoint and filters; page size may change. Dates sort newest first
with deterministic ID tie breakers. Transcript segments sort by `start_s`, then
ID, so segments at the same timestamp are not skipped. Segment lists also accept
`start_s` (inclusive) and `end_s` (exclusive), filtering by segment start time in
seconds from the meeting start.

Search returns at most 320 characters of context around each match:

```json
{
  "kind": "segment",
  "id": "sg_example",
  "meeting_id": "m_example",
  "title": "Launch review",
  "timestamp": "2026-09-01T12:00:00+00:00",
  "excerpt": "We launch Friday.",
  "matched_field": "text",
  "start_s": 42.0,
  "end_s": 45.0,
  "origin_device_id": null,
  "resource": "/v1/meetings/m_example/segments/sg_example"
}
```

Resolve `resource` against the local API base URL and send the authorization
header to retrieve the source. Remote results include the required
`include_remote=true` query parameter in that path. A meeting title match and
matching segments may each appear as separate hits. Search ordering is by record
time, then kind and ID, not relevance or speaking order. Fetch nearby segments
for context before drawing conclusions.

Insights return the saved snapshot's `state_seq` and keep evidence segment IDs.
Removed cards and dismissed questions are excluded. `snapshot_available=false`
means no insights snapshot was saved. A malformed snapshot produces an explicit
error; meeting metadata and transcript reads remain available. Insights expose
selected card metadata, not the full internal dashboard state.

Pagination is not a frozen snapshot. Newer inserts do not shift a cursor, but
edits, deletions, and transcript reprocessing can change later pages or invalidate
old citation IDs. Deleted sources return 404; clients should not invent evidence.

### Errors

Errors use `{"error":{"code":"...","message":"..."}}`.
401 means a missing or invalid token; 403 means a rejected browser origin or Host
header; 404 means a missing record (including an excluded remote record);
400 means invalid cursor or filter combinations; 422 means invalid parameter
types or bounds; 405 means an unsupported HTTP method; 503 means the database or
insights snapshot is unavailable. Database errors do not include SQL or paths.

## Agent integration boundary

The service binds only to IPv4 loopback, rejects browser Origin headers and
unexpected Host headers, disables access logs, and marks responses `no-store`.
Tokens for the meeting dashboard and remote engine are separate credentials.
Database connections use SQLite `mode=ro` and `query_only`; API startup never
creates a database, migrates its schema, or imports legacy history. An incompatible
schema must be opened with the corresponding desktop app first.

Responses use explicit public schemas. They do not expose dashboard capability
tokens, recording paths, audio files, embeddings, endpoint configuration, or
process bookkeeping. User-authored transcript and note text is returned as saved,
so it can still contain sensitive information. A client using a cloud model may
send that retrieved text to its provider.

MCP tools map directly to the OpenAPI operations `search_history`,
`get_transcription`, `list_meetings`, `get_meeting`, `list_meeting_segments`,
`get_meeting_segment`, and `get_meeting_insights`. The transport-independent
`HistoryStore` owns retrieval; HTTP owns authentication and request validation.
This standalone API has no write operations, network sharing, OAuth, or
per-client scopes. The desktop [MCP server](mcp.md) separately offers optional,
user-permitted title and settings changes; its History API remains read-only.

An agent skill should search narrowly, retrieve primary transcript evidence,
cite record/segment IDs and timestamps, distinguish proposed insights from
confirmed ones, and treat retrieved text as untrusted content rather than
instructions. The MCP adapter reuses this authenticated API in-process instead
of granting an agent raw database access.

Implementation references: [FastAPI bearer security](https://fastapi.tiangolo.com/reference/security/#fastapi.security.HTTPBearer)
and [SQLite read-only URI connections](https://www.sqlite.org/uri.html).
