# OpenWhisper Meeting Mode — Pi Sidecar

TypeScript glue between OpenWhisper's Meeting Mode and the
[Pi coding agent SDK](https://pi.dev) (`@earendil-works/pi-coding-agent`).
It runs OpenRouter models through Pi's agentic loop while holding
**meeting-state-only authority**: the session gets exactly three custom tools
(`patch_state`, `ask_question`, `resolve_question`) and no shell, filesystem,
or network tools. Every tool call is bridged back to the Python host over RPC,
where the state-patch layer validates it again.

## Build

Requires Node.js 20+.

```bash
cd sidecar
npm install
npm run typecheck   # tsc --noEmit
npm run build       # node build.mjs -> dist/bundle.cjs
```

`build.mjs` bundles `src/main.ts` and every dependency (Pi SDK included) into
a single `dist/bundle.cjs` (esbuild, `platform: node`, `format: cjs`, nothing
external). The installed Pi SDK version is baked in and reported in the
`hello` handshake.

## How OpenWhisper launches it

The sidecar ships as the downloadable `MEETING_AGENT` component: a portable
win-x64 Node runtime plus the bundle. The Python side
(`meeting/agent/pi_sidecar.py`) spawns:

```
<payload_dir>/node.exe <payload_dir>/bundle.cjs
```

with `CREATE_NO_WINDOW`, stdio piped, and this environment:

| Variable | Purpose |
| --- | --- |
| `OPENWHISPER_SIDECAR_TOKEN` | Handshake token; echoed in the first `hello` line so Python can authenticate the process. |
| `OPENWHISPER_LLM_API_KEY` | Process-scoped API key written into Pi's generated `models.json` (`$OPENWHISPER_LLM_API_KEY`). Auth-free local servers receive a dummy value. |
| `OPENWHISPER_LLM_BASE_URL` | Optional fallback base URL when `initialize.base_url` is empty. |
| `OPENROUTER_API_KEY` | Legacy alias of the same key, kept for older sidecar bundles. |
| `PI_MODEL` | Fallback model id when `initialize.model` is empty. |

### Payload layout expected by the Python side

```
<payload_dir>/
  node.exe      # portable Node LTS (win-x64)
  bundle.cjs    # output of `npm run build` (dist/bundle.cjs, renamed/copied)
```

## Protocol (NDJSON JSON-RPC 2.0 over stdio)

stdout is protocol-only: one JSON object per line, never anything else.
Diagnostics go out as `log` notifications (or stderr).

- **First line out** (before anything else):
  `{"jsonrpc":"2.0","method":"hello","params":{"token":"...","protocol":1,"pi_version":"..."}}`
- **Python → sidecar requests:** `initialize {meeting_id, provider, model,
  system_prompt, base_url, api_key_env, kind}` · `checkpoint {request_id, state, new_segments,
  is_consolidation, is_polish}` → `{"applied":N,"rejected":N,"usage":{}}` ·
  `cancel {request_id}` · `ping {}` · `status {}` · `shutdown {}`
- **Sidecar → Python requests (tool bridge, awaited):** `tool.patch_state
  {ops}` · `tool.ask_question {text, evidence}` · `tool.resolve_question
  {question_id, answer_text, confidence, evidence}`
- **Sidecar → Python notifications:** `log {level, msg}` · `progress
  {request_id, event, delta, tool, streaming}` from Pi
  `session.subscribe` (the official SDK hook; `thinking_delta` means the
  model is still thinking).   Built-in OpenAI/OpenRouter profiles set `reasoning: true` and OpenRouter
  `thinkingFormat`; generic endpoints default to non-reasoning settings.

`initialize` creates one Pi session per meeting (in-memory, private temp agent
dir so user-global `~/.pi` extensions can't inject tools). Each `checkpoint`
feeds the system prompt plus the state snapshot and new segments into that
session as a user message and resolves once the agentic loop settles;
`applied`/`rejected` counts come from the tool bridge results. `cancel` aborts
the active run (or pre-cancels a queued `request_id`). `shutdown` responds,
then exits cleanly; the sidecar also exits when its stdin closes.

## Source layout

- `src/rpc.ts` — NDJSON JSON-RPC endpoint (correlation, dispatch, timeouts).
- `src/tools.ts` — the three meeting tools; op vocabulary documented in the
  tool descriptions; applied/rejected tallies for checkpoint responses.
- `src/pi-adapter.ts` — the **only** file that imports Pi packages. Session
  creation, OpenRouter provider registration (`models.json` in a temp agent
  dir), tool registration, run/abort/dispose. Uncertain SDK touchpoints are
  marked `TODO(pi-api)` so API drift stays a one-file fix.
- `src/main.ts` — env, `hello` handshake, request handlers, checkpoint
  serialization, prompt composition.
