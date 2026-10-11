# Actions, live events and hooks

OpenWhisper can do more with your words than paste them. An **action** takes
what you said and sends it somewhere: a new Claude or ChatGPT chat, your
coding agent, a running Claude Code session, a web address, a program, or
just your clipboard. Outside programs can follow what OpenWhisper does as
**live events**, start actions and recordings, and change dictated text
before it's pasted.

Everything runs on this computer. Nothing here is reachable from the
internet; if you want an event to go to a cloud service, a program or webhook
you set up decides that.

- [Actions](#actions): what a hotkey or a spoken name does with your words
- [Route by voice](#route-by-voice): "Claude, …" sends the words to that action
- [Hooks](#hooks): webhooks, programs and text filters (Settings → Automate → Hooks)
- [Live API](#live-api): events, actions and recording over local HTTP
- [Events](#events): every event and its fields
- [MCP](#mcp): tools for agents, and MCP Events
- [Claude Code channel](#claude-code-channel): dictate into a running Claude Code session
- [Privacy and safety](#privacy-and-safety)

## Actions

Open **Settings → Automate → Actions**. Add an action from a preset, give it a
shortcut, and press the shortcut anywhere: OpenWhisper records like the record
hotkey (press to start, press again to stop), then hands the words to the
action instead of pasting them. Press the shortcut again while it is
recording to stop.

| Kind | What it does |
| --- | --- |
| Open a link | Opens a link with your words in it. Presets: **Ask Claude** (`claude://claude.ai/new?q={text}`, the Claude desktop app), **ChatGPT app** (`codex://new?prompt={text}`), **ChatGPT on the web** (`https://chatgpt.com/?q={text}`, not documented by OpenAI, so it may stop working). `{text}` is replaced by your words, percent-encoded. These apps only fill in the message; you press send. |
| Agent | Hands your words to Claude Code, Codex or OpenCode installed on this computer. **Open a session** starts a visible session with your words: the agent's app fills them in for you to send, while a terminal session starts working on them right away. **Quick answer** asks the agent with no tools or files and shows the answer, in the Scratchpad by default. |
| Tell my Claude Code session | Sends your words into a running Claude Code session that has the [OpenWhisper channel](#claude-code-channel). |
| Webhook | POSTs your words to a web address, signed (see [Verifying signatures](#verifying-signatures)). The reply's `"text"`, if any, can go to an output. |
| Run a program | Runs a program with your words on its standard input. Its output can go to an output. |
| Copy only | Copies your words without pasting. |
| Send to my scripts | Does nothing itself: programs following the [live events](#live-api), a [webhook](#webhooks) or a [program hook](#programs) act on the `action.ran` event, which carries your words in `text` for this kind. If nothing is listening, your words are copied instead. |

**How the words are prepared** for an action: as you said them (**Raw**: the
dictionary and snippets apply, no AI), **Cleaned up** (your normal AI
cleanup, following its on/off switch), with a **cleanup profile**, or cleaned
up and then rewritten by a **transform** such as *Prompt engineer*.

**Where an answer goes** (webhook, program and Quick answer): nowhere (a short
"done" notice), **Paste**, the **Scratchpad**, a **notice** near the pointer,
or the **clipboard**. A paste only lands in the app you started in; if you
switched apps meanwhile, the answer is copied and a notice says so. A notice
shows the start of a long answer and copies all of it. If an action you
started fails, your words are copied so nothing is lost, and the notice says
why; if its transform can't run, it gets your cleaned-up words instead and
says so. Actions a program starts over the API never touch your clipboard or
type into your apps: an answer they would paste or copy goes to the
Scratchpad, a notice doesn't copy the rest of a long answer, and **Copy
only** puts the words in the Scratchpad.

Actions that take a while (an agent's answer, a webhook, a program) keep
running after the dictation is saved, 4 at a time with up to 4 more waiting.
**Cancel** while a recording or transcription is in progress cancels only
that. Press it once your words have gone to the action, or with nothing
recording, to cancel the actions still running:
those still waiting never start, and a Quick answer or a program is stopped.
One that has already started and can't be stopped (opening a link, posting
to a webhook, opening a session), or that had already finished, reports what
happened, but its answer is dropped, and the notice says it was too late to
stop it.

The **Agent hotkey** card at the top of the page sets up the action for your
installed agent in one place. **Try it** runs an action with sample words
without saving anything to History.

Every action you dictate is saved in History as `Action · <name>`, with the
original words alongside.

### Webhook and program actions

A webhook action POSTs:

```json
{"v": 1, "action": {"id": "…", "name": "Ask Claude"}, "job_id": "…",
 "text": "the prepared words", "raw_text": "before AI cleanup, or null",
 "language": "en", "app": {"id": "notepad.exe", "name": "Notepad", "category": "docs"}}
```

Reply `{"text": "…"}` to send an answer to the action's output.

A program action gets the prepared words (UTF-8) on standard input and
`OPENWHISPER_ACTION_ID`, `OPENWHISPER_ACTION_NAME` and `OPENWHISPER_JOB_ID` in
its environment; whatever it prints is the answer. It runs without a shell or
a window, with your environment minus the API keys OpenWhisper loaded from its
own `.env`, and is stopped with everything it started after its time limit.
Processes it starts keep running after it exits, but a second later
OpenWhisper stops reading their output and writing to their input, so send
their output somewhere else.

## Route by voice

Turn on **Route by voice** on the Actions page, then either:

- set the **Route by voice** shortcut (Settings → Hotkeys): record, and your
  words go to the action they name ("ask Claude what's the capital of Peru",
  "todo: buy milk"), or are pasted when they name none; or
- turn on **Also listen to normal dictations**: a dictation that *starts* with
  an action's name or one of its spoken names is routed too.

OpenWhisper first checks in code whether the words start with an action's
name (filler words like "hey" or "okay", a name heard as separate words
like "chat gpt" or "to do", and common mishearings such as "cloud" for
"Claude" are fine), then asks **Fast judgments** (TypeSafe's Jev
model, through TypeSafe or OpenRouter) one question: which action, and which
part of the words is for it. Only a confident answer routes; anything else,
including a failure or a 1.5 s timeout, pastes your words as usual. With Route
by voice, a notice says when no action matched, or when Fast judgments
couldn't be reached in time.

Routing needs Fast judgments set up (Settings → Meeting Mode → Fast
judgments). **What is sent:** only words recorded with Route by voice, or
dictations that start with an action's name, together with each candidate
action's name, spoken names, description and kind. An action's link, address,
program, folder or arguments are never sent. Give each action a short
description ("questions for Claude", "my shopping list"): routing is more
reliable with one.

Words a program changed, or a recording a program started, go to an Agent
hotkey or Claude Code session action only with **Send words to my coding
agents**; otherwise a notice says so and, if you started it, your words are
copied.

## Hooks

Open **Settings → Automate → Hooks**. Hooks are saved only in Settings; the
live API and agents can't create or change them.

### Webhooks

A webhook receives the events you choose (by default, finished dictations) as
JSON: the [event](#events) itself, one per request. Deliveries are signed
(below), retried after 1, 5 and 25 seconds on network errors, 408, 429 and 5xx
(honouring `Retry-After`), never retried on other 4xx, and never follow
redirects. Each webhook gets its events in order. **Send test** posts a
`webhook.test` event; the page lists the last deliveries.

### Programs

A program receives the event's JSON and a newline on standard input, with
`OPENWHISPER_EVENT_TYPE`, `OPENWHISPER_EVENT_ID` and `OPENWHISPER_EVENT_SEQ`
in its environment. It runs one event at a time (up to 20 more wait), without
a shell or a window, and is stopped with everything it started after its time
limit (30 s by default). Processes it starts keep running after it exits, but a second later
OpenWhisper stops reading their output and writing to their input, so send
their output somewhere else.

### Text filters

A text filter changes dictated words before they're pasted or sent to an
action: either **before AI cleanup** (the speech engine's words) or **after**
(the final text). It is a web address OpenWhisper POSTs to, or a program that
gets the same JSON on standard input and answers on standard output.

A filter registered over the API never sees words going to an Agent hotkey or
Claude Code session action unless you also allow **Send words to my coding
agents**. Words it changes count as a program's: they reach a coding agent
only with that permission, and Claude Code sees them as `origin="program"`.
With Route by voice, your saved filters run before the words are routed and
filters registered over the API after, so they see the action the words go
to (`mode` `action`, its `action_id`) and can't change which one it is.

Request:

```json
{"v": 1, "stage": "after_cleanup", "job_id": "…", "mode": "dictation",
 "action_id": null, "text": "the words", "language": "en", "app": {"id": "…", "name": "…", "category": "…"}}
```

Reply `{"text": "new words"}` to replace them, `{}` (or HTTP 204, or nothing)
to keep them, or `{"cancel": true}` to paste nothing (the words are still
saved in History). Each filter has its own time limit (1.5 s by default, at
most 5 s) and all the filters of a stage share 3 seconds; a filter that is
slow, fails or answers badly keeps the words and publishes `filter.failed`.
Filters apply to dictations and actions, never to Command Mode, transforms or
file transcriptions.

### Verifying signatures

Webhooks, webhook actions and MCP Events are signed with
[Standard Webhooks](https://www.standardwebhooks.com): headers `webhook-id`,
`webhook-timestamp` and `webhook-signature: v1,<base64>`, where the signature
is HMAC-SHA256 over `id.timestamp.body` keyed by the base64 part of your
`whsec_…` secret. Copy the secret from Settings. In Python:

```python
import base64, hashlib, hmac, time

def verify(secret: str, headers: dict, body: bytes, tolerance_s: int = 300) -> bool:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    msg_id, ts = headers["webhook-id"], headers["webhook-timestamp"]
    if abs(time.time() - int(ts)) > tolerance_s:
        return False
    expected = base64.b64encode(
        hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    ).decode()
    return any(
        part.startswith("v1,") and hmac.compare_digest(part[3:], expected)
        for part in headers["webhook-signature"].split()
    )
```

The [Standard Webhooks libraries](https://github.com/standard-webhooks/standard-webhooks)
do the same in other languages.

## Live API

The live API is served by the running app on the same listener as MCP. Turn
on **Settings → Automate → API & MCP**; the address is
`http://127.0.0.1:8767` by default and every request needs the page's access
token as `Authorization: Bearer <token>`. Browsers can't call it (requests with
an `Origin` header are refused). The headless `openwhisper --api` serves only
the read-only [History API](agent-api.md), not these routes.

**Permissions** (API & MCP page), checked on every request:

| Permission | Default | Allows |
| --- | --- | --- |
| Follow live events | on | `GET /v1/events`, MCP `recent_events` |
| Run actions | off | running actions, showing notices |
| Start recordings | off | starting, stopping and canceling recordings |
| Change dictated text | off | text filters registered over the API |
| Send words to my coding agents | off | with Run actions, Start recordings or Change dictated text: lets an Agent hotkey or Claude Code session action get words a program sent, a dictation it started, or words its text filter changed. Without it those words aren't sent; if you started the dictation, they're copied instead. |

| Route | Permission | Result |
| --- | --- | --- |
| `GET /v1/live/state` | token | `recording`, `busy` (until the job is saved in History), `meeting`, `mode` and `action_id` (the action Route by voice chose, once it chose one), `job_id`, `version`, `permissions`, `listeners` |
| `GET /v1/events?types=a,b&since=<seq>` | live events | Server-Sent Events, below |
| `GET /v1/actions` | token | `{"actions": [{id, name, kind, description, spoken_names, hotkey, shaping, enabled}]}` (never links, paths or folders) |
| `POST /v1/actions/{id}/run` `{"text": "…"}` | run actions (+ coding agents for agent and channel actions) | 202 `{"job_id"}`: the words take a dictation's path without recording; 409 `busy` while too many actions are still running |
| `POST /v1/recording/start` `{"action_id"?}` | start recordings | 202 `{"job_id"}`; the dictation isn't cleaned up with the text near your cursor, even when that's on |
| `POST /v1/recording/stop`, `/cancel` | start recordings | 202 `{}`; `/cancel` cancels the recording or transcription in progress, never actions you started (422 `refused` when there is none, including once a dictation's words have gone to its action) |
| `POST /v1/filters` `{"url", "stage", "timeout_ms"?}` | change text | 201 lease `{id, url, stage, timeout_ms, modes, expires_at, ttl_s}` |
| `POST /v1/filters/{id}/renew`, `DELETE /v1/filters/{id}` | change text | 200 lease / 204 |
| `POST /v1/notify` `{"text", "title"?}` | run actions | 202: a short notice near the pointer |

Errors are `{"error": {"code", "message"}}`: 401 without a valid token, 403
`permission_denied` (the message says which switch to turn on), 404
`not_found`, 409 `busy`, 413 `request_too_large`, 422 for invalid input or a
refusal, 503 `unavailable`. A filter registered over the API must listen on
this computer (`127.0.0.1`, `::1` or `localhost`); renew it within 60 seconds
or it lapses, and it is dropped when the API stops.

### The event stream

`GET /v1/events` is a [Server-Sent Events](https://html.spec.whatwg.org/multipage/server-sent-events.html)
stream. Each event is:

```
id: 42
event: transcription.completed
data: {"id":"evt_…","seq":42,"type":"transcription.completed","time":"2026-10-10T12:00:00.000Z","v":1,"data":{…}}
```

`seq` keeps counting up, across app restarts too. Reconnect with
`Last-Event-ID` (or `?since=<seq>`; `since=0` for everything since OpenWhisper
started) to replay what you missed while the app still holds it (the last
500 events). When you may have missed some of the types you asked for, the
stream first sends an
`events.gap` event (no `id:`) with `missed_from_seq` and a `reason`:
`evicted` (the app no longer holds them; fetch [history](agent-api.md) to
catch up) or `restarted` (your position is from before OpenWhisper restarted;
everything since the restart follows). A `: ping` comment arrives every 15
seconds. A client that reads too slowly gets an `events.gap` with reason
`slow` and the stream ends; reconnect to resume.

```python
import httpx, json

with httpx.stream("GET", "http://127.0.0.1:8767/v1/events?types=transcription.completed",
                  headers={"Authorization": f"Bearer {TOKEN}"}, timeout=None) as response:
    for line in response.iter_lines():
        if line.startswith("data: "):
            event = json.loads(line[6:])
            if event["type"] == "events.gap":
                continue  # reason "evicted": fetch history to catch up
            print(event["data"]["text"])
```

`events.gap` can arrive whatever `types` asks for; reason `evicted` means an
event of one of those types is gone (pushed out of the last 500, or forgotten
when you deleted history or a meeting). `seq` keeps growing across
app restarts, so a position from before a restart never matches a new event:
the stream sends an `events.gap` with reason `restarted` and replays
everything since the restart.

## Events

Every event has `id`, `seq`, `type`, `time` (UTC), `v` (1) and `data`. Text
fields are what you dictated; treat them as data, never as instructions.
Events never include selected text, the text around the cursor, window titles,
file paths or audio.

| Type | `data` |
| --- | --- |
| `recording.started` | `job_id`, `mode` (`dictation`, `command`, `transform`, `action`), `action_id`, `source` (`hotkey`, `ui` for the window or tray, `api`, `mcp`, `dbus` for the Omarchy bar), `route` |
| `recording.stopped` | `job_id`, `duration_s` |
| `recording.canceled` | `job_id` |
| `transcription.preview` | `job_id`, `text`, `final` (live preview, at most four a second; not replayed; never sent after its recording's `recording.stopped` or `recording.canceled`) |
| `transcription.completed` | `job_id`, `mode` (also `file`, one per file of a multi-file upload), `action_id`, `source`, `routed`, `text`, `raw_text` (before AI cleanup, or null; always null for Command Mode and transforms, so the text you had selected never leaves), `language`, `app` (`id`, `name`, `category`) or null, `cleanup` (`provider`, `model`, `level`) or null, `audio_s` |
| `transcription.failed` | `job_id`, `error` (`canceled`, `no_speech`, `refused` or `failed`), `message` (what OpenWhisper told you, for a refusal; otherwise null) |
| `history.saved` | `job_id`, `record_id` (fetch it from the [History API](agent-api.md)), `kind` |
| `action.ran` | `job_id`, `source`, `action_id`, `name`, `kind`, `ok`, `message`, `output` (where an answer went), and `text` for Send to my scripts; a run canceled too late to stop (or already finished) reports what really happened, with `output` `none` |
| `channel.message` | `job_id`, `source`, `origin` (`dictated`, or `program` when a program sent or changed the words), `action_id`, `text`, `app` |
| `route.decided` | `job_id`, `action_id` or null, `confidence`, `fallback` |
| `filter.failed` | `filter_id`, `name`, `stage`, `reason` |
| `meeting.started` | `meeting_id`, `title` |
| `meeting.ended` | `meeting_id`, `status` |
| `meeting.segments` | `meeting_id`, `items` (`segment_id`, `speaker`, `text`, `start_s`, `end_s`) |
| `meeting.voice_command` | `meeting_id`, `command` |

## MCP

Agents connected over [MCP](mcp.md) get these tools as well, gated by the same
permissions: `get_live_state`, `list_actions`, `run_action`,
`start_dictation`, `stop_dictation`, `cancel_dictation`, `recent_events` and
`notify_user`. `cancel_dictation` cancels only a recording or transcription
in progress (not once its words have gone to an action), never your running
actions.

**MCP Events** (preview, off by default; API & MCP page) implements the
[MCP Events draft](https://github.com/modelcontextprotocol/experimental-ext-triggers-events)
the way ChatGPT uses it: an agent lists events with `events/list`, subscribes
with `events/subscribe` (a webhook address and a `whsec_` secret it chooses)
and unsubscribes with `events/unsubscribe`. OpenWhisper checks the address
with a signed challenge before sending anything, then posts each matching
event signed with the agent's secret. Addresses must use https, or http on
this computer only. An agent on another computer (over Tailscale) can't send
events to this computer, including its Tailscale address, or to your local
network. Its events stop while **Allow agents over Tailscale** is off, and a
failed check or delivery tells it only that it failed (`challenge_failed`,
`delivery_failed`), not why. Subscriptions expire unless the agent refreshes them, and
**Clear** on the API & MCP page removes them all. Agents in the cloud can only
subscribe if they can reach this computer, which OpenWhisper never sets up.

With MCP Events on, a connected agent chooses the web address its events go
to, and that address receives what the events carry, including the words you
dictate. Turn it on only for agents you trust with your dictation.

## Claude Code channel

A [channel](https://code.claude.com/docs/en/channels) lets OpenWhisper send
your words into a Claude Code session that is already running (research
preview).

1. Turn on **API & MCP**. To let Claude answer near your pointer, also allow
   **Run actions**.
2. Copy the `.mcp.json` entry from **API & MCP → Claude Code channel** into
   the `.mcp.json` of the project you'll work in. It starts
   `OpenWhisper --channel` and holds no token. Add it only to projects you
   start with the channel flag: a session that loads it without the flag
   counts as listening but drops your messages.
3. In that project, run
   `claude --dangerously-load-development-channels server:openwhisper` and
   confirm the prompts.
4. Add an action of kind **Tell my Claude Code session** and give it a
   shortcut. Your words arrive in the session as a
   `<channel source="openwhisper" origin="dictated" …>` message; Claude can
   answer with its `reply` tool. A program can send words there too, through
   the live API or MCP, only if you also allow **Send words to my coding
   agents**; those arrive with `origin="program"`, so Claude knows they aren't
   your own request. So do your own words when a program's text filter
   changed them, and dictations a program started.

The channel reconnects by itself when OpenWhisper restarts. It needs a
claude.ai login or a Console API key, doesn't work with `claude -p`, and
Team or Enterprise organisations must allow channels.

## Privacy and safety

- Everything listens on `127.0.0.1` (plus your Tailscale address only if you
  turned that on for MCP), behind the access token.
- Running actions, starting recordings, changing dictated text and sending
  words to your coding agents are each off until you allow them, and are
  checked on every request. Following live events is on, the same trust as
  reading history.
- With MCP Events on, a connected agent chooses where its events are sent;
  see [MCP](#mcp).
- Deleting a dictation, clearing history or deleting a meeting also clears the
  events held for replay.
- Programs, webhooks, saved filters and actions are created only in Settings.
  The API and agents can list and run actions, but never add a program or
  webhook, change an action's address or turn on a permission.
- Dictated words reach programs on standard input, links percent-encoded,
  and terminals as a single, quoted argument, never through a shell. Events
  never carry the text you had selected for Command Mode or a transform.
- Actions and recordings a program starts never read the text near your
  cursor or the text you have selected; they only learn which app you're in.
- Programs, terminals and agents start with your own environment, without
  the API keys OpenWhisper loaded from its `.env` (however the file spells
  them, and even after you change or remove them there; a variable already
  set to the file's value when OpenWhisper starts counts as loaded from it,
  so only a value of your own is passed on) or the folders it adds to `PATH`
  for itself. On Windows, links open through Explorer, so their apps start
  as they would from a link you click. If Windows doesn't answer within 30
  seconds, the action says so and the link isn't opened.
- On Windows, OpenWhisper can't tell whether a link's app started: Explorer
  reports no errors back. An action reports the link as opened once Windows
  has taken it, even if the app then fails or you decline its elevation
  prompt; only a link no app is registered for is reported as missing, and
  web links always count as opened.
- Restoring a backup turns every webhook, program, filter, and webhook,
  program and agent action off, along with every permission except following
  live events, and MCP Events. Secrets live in your system's credential store, never in
  backups.
- Route by voice sends words to TypeSafe or OpenRouter only as described in
  [Route by voice](#route-by-voice).
