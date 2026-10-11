# Actions, live API, events and hooks: design and work split

Status: foundation (2026-10-10), revision 1. This is the shared contract for
the parallel work streams that make OpenWhisper programmable: actions bound
to hotkeys or spoken names, a live local API with an event stream, webhooks,
programs and text filters, MCP tools and MCP Events, a Claude Code channel,
an agent hotkey, and spoken routing with Jev. Every stream reads this first.

The coordinator owns this file, CHANGELOG.md, README.md, docs/*.md user
guides, and the files marked **coordinator-owned** below. Streams report
user-visible changes and any needed change to a coordinator-owned file in
their final report instead of editing them.

## What we are building

One idea, an **action**: "take what I said and do X". A hotkey, a spoken
name, the local API or an MCP tool starts one. Everything that happens in
OpenWhisper is also published as an **event**, and outside programs can
watch events, trigger actions and recordings, and change dictated text.
Nothing is a plugin loaded into the app: extensions are separate programs
talking to a local API.

```
  action:<id> hotkey ─┐                    ┌─ paste (today's dictation)
  route_record hotkey ┤                    ├─ open a link (claude://, codex://, https)
  "Claude, …" (Jev) ──┼─► record → ASR ──► ├─ hand off to / ask an installed agent
  POST /v1/actions ───┤   filters, shape   ├─ Claude Code channel (running session)
  MCP run_action ─────┘   (cleanup/profile ├─ webhook · program · clipboard
                           /transform)     └─ "send to my scripts" (event only)
                                  │
                         EventBus ─┴─► SSE /v1/events · webhooks · programs ·
                                       MCP Events · channel bridge
```

## Principles

- **Local first.** Everything listens on the in-app listener (127.0.0.1,
  plus the existing opt-in Tailscale address with its peer check) behind the
  existing MCP token. Nothing needs to be reachable from the internet; the
  user's own service decides what to forward.
- **No surprises on upgrade.** With no actions, hooks or filters configured
  and routing off, dictation behaves exactly as today. Every pipeline stage
  returns its input unchanged when it has nothing to do.
- **Default-deny for power.** Reading live events uses the same trust as
  reading history (on). Running actions, starting the microphone and changing
  text are separate permissions, off by default, rechecked on every request.
- **The API cannot create code paths.** Programs, webhooks, actions and
  saved filters are created only in Settings. The API and MCP may list
  actions and run them; they can never add a program, change a URL, or turn a
  permission on. A restored backup comes back with every program, webhook,
  filter and powerful permission off.
- **Dictated text is untrusted data.** It reaches programs on stdin, URLs
  percent-encoded, terminals as one argv element never through a shell, and
  agents as a prompt. It is never logged. Events never carry selected text,
  caret context, window titles or audio.
- **Reliability first.** A slow, failing or missing webhook, program, filter,
  agent, judge or listener never delays a paste beyond its stated budget and
  never breaks a dictation. Filters time out to the original text; routing
  times out to a normal paste.
- **Warm, calm UI** from the existing Settings design system (SettingTile,
  FieldTile, SettingsSwitch, SegmentedBar, neutral_button,
  compact_primary_button, _PermissionRow-style cards). Honest status only:
  "Last delivery 200 · 120 ms", never fake progress.
- **Light at startup.** `services.automation.events`, `catalog`,
  `permissions` and `services.actions.model` are stdlib-only and imported at
  startup. Everything else is imported on first use. No new third-party
  dependencies (httpx, fastapi, uvicorn, mcp are already in).
- **Classic and Omarchy parity**; where a platform cannot do something
  (a URL handler is missing, no terminal is found), the UI says so.

## Product decisions

1. **Actions.** Saved under `actions`. Each has a name, a kind, kind
   parameters, an optional hotkey, how its text is shaped, spoken names, a
   description (what it is for, used by routing), and an enabled switch. Up
   to 50. An action hotkey records like a profile hotkey (always toggle: press
   to start, press again to stop). Kinds:
   - `open_link`: open a URL template; `{text}` is replaced by the
     percent-encoded text. Presets: Claude Desktop new chat
     `claude://claude.ai/new?q={text}`, ChatGPT desktop app
     `codex://new?prompt={text}`, ChatGPT web `https://chatgpt.com/?q={text}`
     (undocumented; labelled so). Schemes `file`, `javascript`, `data`,
     `vbscript` and empty are refused. Links only prefill: the user sends.
   - `agent`: the agent hotkey. `mode=handoff` opens a visible session of the
     user's installed Claude Code, Codex or OpenCode with the text (via the
     app's documented deep link when its handler is registered, else a new
     terminal running the CLI with the text as one argv element);
     `mode=ask` runs the agent headless with no tools and shows the answer
     (Scratchpad by default). Optional: include the selected text and the
     app name as context, a working folder, a model for ask mode.
   - `channel`: send the text into the Claude Code session(s) running the
     OpenWhisper channel. With no listener connected, copy the text and say
     how to start one.
   - `webhook`: POST a signed JSON payload; optional output from the reply.
   - `program`: run a program with the text on stdin; optional output.
   - `clipboard`: copy only.
   - `scripts`: "Send to my scripts": publish the event only; listeners act.
   Output for `webhook`, `program` and `agent`/ask: `none`, `paste`,
   `scratchpad`, `notice` or `clipboard`. A paste only lands in the app the
   job started in (else it is copied with a notice).
2. **Shaping** (what text the action receives): `raw` (ASR text after the
   dictionary and snippets, no AI), `cleanup` (normal cleanup, honouring the
   cleanup switch; the default), `profile:<id>` (a cleanup profile),
   `transform:<id>` (normal cleanup, then that transform's instruction
   applied to the dictated text).
3. **The agent hotkey** is an action of kind `agent`. The Actions page shows
   a featured "Agent hotkey" card at the top that creates or edits the action
   with id `agent` (detected agents only; Claude Code preferred, then Codex,
   then OpenCode).
4. **Spoken routing** (off by default; needs Fast judgments, i.e. a TypeSafe
   or OpenRouter key):
   - A new standard hotkey `route_record` ("Route by voice", unbound):
     record, then send the words to the action they name, or paste them.
   - Optional `voice_routing_on_dictation`: a normal dictation that *starts
     with* one of an action's spoken names is routed too.
   - A code gate first (spoken names/the action name at the start, after
     filler words), then one Jev request: a Choice over the candidate actions
     plus `none`, and a Choice over code-proposed payload spans, asked
     together. Below the confidence threshold, or on any judge failure or a
     1.5 s timeout, the text is pasted as a normal dictation with a notice.
   - Only text that passed the gate (or was spoken with the route hotkey) is
     sent to the judge. Copy states that routing sends it to TypeSafe or
     OpenRouter.
5. **Events**, published on an in-process bus and delivered by SSE,
   webhooks, programs, MCP Events and the channel bridge. Catalog below.
6. **Webhooks** (Settings → Hooks): URL, event types, enabled. Signed with
   Standard Webhooks (same scheme as MCP Events). http allowed (local
   services), with a note when the host is not loopback/private. 5 s timeout,
   3 retries with backoff on network errors, 408, 429, 5xx. No redirects.
   Up to 20.
7. **Programs** (Settings → Hooks): program path, arguments, event types,
   enabled. Event JSON on stdin; never a shell. 30 s timeout, killed with its
   process tree. One run at a time per program, 20 queued. Up to 20.
8. **Text filters** (Settings → Hooks, and leased over the API): change
   dictated text. Stage `before_cleanup` (raw ASR text, before the
   dictionary and snippets) or `after_cleanup` (final text before delivery).
   Kind `http` (POST JSON, reply `{"text": "…"}` to replace, `{}`/204 to keep,
   `{"cancel": true}` to deliver nothing) or `program` (stdin JSON, stdout
   JSON, same reply). Per-filter timeout (default 1500 ms, max 5000 ms) and a
   3 s budget per stage; a timeout or bad reply keeps the text and publishes
   `filter.failed`. Applies to live dictation and action jobs, never to
   Command Mode, transforms or file transcriptions. API leases: loopback URLs
   only, expire 60 s after the last renew. Up to 10 saved.
9. **Live API** (see Contracts): state, SSE events, actions list/run,
   recording start/stop/cancel, filter leases, notices. Permissions:
   `api_live_events` (on), `api_run_actions`, `api_control_recording`,
   `api_text_filters`, `api_agent_prompts` (off).
10. **MCP**: tools `get_live_state`, `list_actions`, `run_action`,
    `start_dictation`, `stop_dictation`, `cancel_dictation`, `recent_events`,
    `notify_user`, gated by the same permissions, plus the MCP Events draft
    extension (`events/list`, `events/subscribe`, `events/unsubscribe`, webhook
    delivery with challenge verification) behind `mcp_events_enabled` (off).
    Instructions change from "cannot record" to "can start recording only when
    the user allowed it".
11. **Claude Code channel**: `openwhisper --channel` (frozen exe or
    `python main.py --channel`) is a stdio MCP server Claude Code launches. It
    declares `experimental['claude/channel']`, reads the app's port and token,
    subscribes to `channel.message` over SSE, and forwards each as
    `notifications/claude/channel`. A `reply` tool posts to `/v1/notify` so
    Claude can answer near the pointer. Settings shows the `.mcp.json` entry and
    the `claude --dangerously-load-development-channels server:openwhisper`
    command, and how many sessions are listening. Research preview: labelled.
12. **Settings information architecture.** New rail group **Automate**
    between Personalize and Meeting Mode: **Actions** (`actions`), **Hooks**
    (`hooks`, "Webhooks, programs and text filters"), **API & MCP** (`mcp`,
    moved from App, retitled). Hotkeys page lists `route_record`.

## Architecture

```
hotkey action:<id> ─► controller.action_record_requested(id)
     └► toggle_action_recording(id): stop if recording, else
        TranscriptionRuntime.start_recording(profile_id=<from shaping>,
            mode=ACTION, action_id=id, source="hotkey")
hotkey route_record ─► controller.toggle_route_recording()
     └► start_recording(mode=DICTATION, route="hotkey")
API POST /v1/actions/{id}/run {text} ─► AppControl.run_action ─► Qt thread:
        TranscriptionRuntime.run_action_text(action_id, text, source)  (no ASR;
        begin_rewrite_job + submit_rewrite(work=_complete_action(text, job)))

worker after ASR (transcribe_audio_file):
   mode COMMAND/TRANSFORM → command_runtime.complete_recording   (unchanged)
   else → _complete_live(raw, job):
        text = filters.apply(BEFORE_CLEANUP, raw, job)        (stub: unchanged)
        canceled → CommandRefused-like quiet end ("Filter canceled delivery")
        if job.route or (DICTATION and voice_routing_on_dictation):
            decision = routing.decide(text, job, settings)    (stub: None)
            decision → job = replace(job, mode=ACTION, action_id=…, routed=True)
        ACTION → shape per action (raw / cleanup / profile / transform)
        DICTATION → _maybe_cleanup_transcript(text)            (unchanged)
        text = filters.apply(AFTER_CLEANUP, text, job)
   transcription_completed(text, raw_text, info) → on_transcription_complete
        publish transcription.completed
        _deliver: job.mode == ACTION → controller.action_runtime.deliver(job, text, …)
                  else scratchpad / paste exactly as today
        history entry_kind "action"; publish history.saved(record_id) after save
```

The bus never calls subscribers on the publisher's thread. Publishers (Qt
thread, workers, uvicorn) only append; one dispatch thread hands events to
subscriber callbacks, which must only enqueue.

## Contracts

The foundation commit creates every module below with fixed signatures.
**FULL** modules are implemented and tested by the foundation; streams may add
private helpers but not change them without telling the coordinator. **STUB**
modules have inert bodies the owning stream fills in. Streams may add new
public functions to their own modules.

### `services/automation/events.py` (FULL, stdlib only)

```python
SCHEMA_VERSION = 1
@dataclass(frozen=True)
class Event:
    id: str          # "evt_" + 24 hex chars, unique
    seq: int         # counts up; the app's bus starts each run after a per-boot base
    type: str        # catalog type
    time: str        # ISO-8601 UTC with milliseconds and "Z"
    data: Mapping[str, Any]   # JSON-safe
    def to_dict(self) -> dict      # {"id","seq","type","time","v","data"}
    def to_json(self) -> str       # compact, ensure_ascii=False
class EventBus:
    def __init__(self, *, buffer_size: int = 500, seq_base: int = 0) -> None
    def publish(self, event_type: str, data: Mapping[str, Any], *, replay: bool = True) -> Event
        # any thread, never blocks; replay=False (live preview) skips the replay buffer
    def subscribe(self, callback: Callable[[Event], None], *,
                  types: Optional[Iterable[str]] = None,
                  label: str = "") -> Subscription   # callback on the dispatch thread
    def since(self, seq: int, *, types=None) -> tuple[list[Event], bool]   # (events after seq, gap)
    def recent(self, limit: int = 50, *, types=None) -> list[Event]
    def subscribers(self, label: str = "") -> int  # live subscriptions, optionally by label
    def last_seq(self) -> int
    def clear(self) -> None                        # drop the replay buffer (history cleared)
    def close(self) -> None
class Subscription:
    def close(self) -> None
    @property
    def closed(self) -> bool
def bus() -> EventBus            # process-wide, created lazily
def publish(event_type, data, *, replay=True) -> Optional[Event]   # never raises, logs at debug
def set_bus(replacement: Optional[EventBus]) -> EventBus   # tests
```
A callback that raises is logged (type name only) and stays subscribed. A
callback slower than 1 s is logged once. `publish` validates that `data` is
JSON-serializable and encodes as UTF-8 (`TypeError` → logged, event dropped).

### `services/automation/catalog.py` (FULL)

Type constants and one builder per type; builders take app objects and
return the `data` dict, so every publisher produces the same shape.

| Type | data | Published by |
| --- | --- | --- |
| `recording.started` | `job_id, mode, action_id, source, route` | foundation (runtime) |
| `recording.stopped` | `job_id, duration_s` | foundation |
| `recording.canceled` | `job_id` | foundation |
| `transcription.preview` | `job_id, text, final` (≤ 4/s) | foundation |
| `transcription.completed` | `job_id, mode, action_id, source, routed, text, raw_text, language, app, cleanup, audio_s` | foundation |
| `transcription.failed` | `job_id, error, message` | foundation |
| `history.saved` | `job_id, record_id, kind` | foundation |
| `action.ran` | `job_id, action_id, name, kind, source, ok, message, output, text` | C |
| `channel.message` | `job_id, action_id, source, origin, text, app` | C |
| `route.decided` | `job_id, action_id, confidence, fallback` | F |
| `filter.failed` | `filter_id, name, stage, reason` | B |
| `meeting.started` | `meeting_id, title` | A (meeting_events) |
| `meeting.ended` | `meeting_id, status` | A |
| `meeting.segments` | `meeting_id, items: [{segment_id, speaker, text, start_s, end_s}]` | A |
| `meeting.voice_command` | `meeting_id, command` | A |
| `events.gap` | `missed_from_seq` (SSE only, synthetic) | A |

`mode` is `dictation | command | transform | action | file`. `app` is
`{"id","name","category"}` or null. `cleanup` is `{"provider","model","level"}`
or null. `source` is `hotkey | ui | api | mcp | dbus | routing`. `raw_text`
is null when cleanup did not change the text. `EVENT_TYPES: dict[str, str]`
maps every type to a one-line description (UI checklists, docs, events/list).
`PUBLIC_TYPES` excludes `events.gap`.

### `services/automation/permissions.py` (FULL)

```python
LIVE_EVENTS, RUN_ACTIONS, CONTROL_RECORDING, TEXT_FILTERS, AGENT_PROMPTS  # SettingsKey values
ALL: tuple[str, ...]
class PermissionDenied(Exception): permission: str; message: str
def granted(settings: Mapping, permission: str) -> bool
def require(settings: Mapping, permission: str) -> None   # raises PermissionDenied
def describe(settings) -> dict[str, bool]   # {"live_events": …, "run_actions": …, …}
```

### `services/automation/signing.py` (FULL) — Standard Webhooks

```python
def new_secret() -> str                       # "whsec_" + base64(24 random bytes)
def signature(secret: str, msg_id: str, timestamp: int, body: bytes) -> str   # "v1,<b64>"
def headers(secret: str, msg_id: str, body: bytes, *, timestamp: Optional[int] = None) -> dict
        # {"webhook-id","webhook-timestamp","webhook-signature"}
def verify(secret: str, headers: Mapping[str, str], body: bytes, *, tolerance_s: int = 300,
           now: Optional[int] = None) -> bool
```

### `services/automation/models.py` (FULL) — saved hooks

```python
@dataclass(frozen=True) class Webhook: id, name, url, events: tuple[str,...], enabled=True
@dataclass(frozen=True) class Program: id, name, program, args: tuple[str,...], events, enabled=True,
                                       timeout_s: int = 30
@dataclass(frozen=True) class TextFilter: id, name, kind ("http"|"program"), stage, url="",
                                       program="", args=(), timeout_ms=1500, enabled=True
load_webhooks(settings) / load_programs / load_filters -> list
save_webhook(settings, hook) / save_program / save_filter      # mutate_settings mutators; ValueError copy
delete_webhook(settings, id) / delete_program / delete_filter
validate_* (settings) raises ValueError with user-facing copy
webhook_secret_name(id) -> "OPENWHISPER_WEBHOOK_SECRET_<id>"
MAX_WEBHOOKS = 20; MAX_PROGRAMS = 20; MAX_FILTERS = 10
BEFORE_CLEANUP = "before_cleanup"; AFTER_CLEANUP = "after_cleanup"
```

### `services/automation/webhooks.py` (STUB → B)

```python
def start() -> None                 # begin delivering to enabled webhooks (idempotent)
def stop() -> None
def reload() -> None                # settings changed
def send_test(hook_id: str) -> Future   # Future[DeliveryRecord]; a "webhook.test" event
def deliveries(hook_id: str) -> list[DeliveryRecord]   # newest first, ≤ 50, memory only
def ensure_secret(hook_id: str) -> str
def rotate_secret(hook_id: str) -> str
def delete_secret(hook_id: str) -> None
@dataclass(frozen=True) class DeliveryRecord: event_id, event_type, at (ISO), ok: bool,
                                           status: int|None, elapsed_ms: int, error: str, attempt: int
def post_signed(url, payload: dict, secret: str, *, timeout_s=5.0) -> DeliveryRecord  # used by C and D
```

### `services/automation/programs.py` (STUB → B)

```python
def start() / stop() / reload()
def run_test(program_id: str) -> Future     # Future[RunRecord]
def runs(program_id: str) -> list[RunRecord]  # ≤ 20, memory only
@dataclass(frozen=True) class RunRecord: event_id, at, ok, exit_code: int|None, elapsed_ms, error
def run_once(argv: Sequence[str], stdin: bytes, *, timeout_s: float, cwd: str = "",
             env: Optional[Mapping[str, str]] = None) -> tuple[int|None, bytes, str]
        # (exit code, stdout ≤ 1 MiB, error); no shell, no window, tree killed on timeout
```

### `services/automation/filters.py` (STUB → B)

```python
@dataclass(frozen=True) class FilterOutcome: text: str; canceled: bool = False; changed: bool = False
def apply(stage: str, text: str, job, settings: Optional[Mapping] = None) -> FilterOutcome
        # worker thread; returns FilterOutcome(text) when no filter applies
def register_lease(url: str, stage: str, *, timeout_ms: int = 1500,
                   modes: Sequence[str] = ("dictation", "action")) -> Lease   # ValueError copy
def renew_lease(lease_id: str) -> Lease          # KeyError when expired/unknown
def drop_lease(lease_id: str) -> bool
def leases() -> list[Lease]
@dataclass(frozen=True) class Lease: id, url, stage, timeout_ms, modes, expires_at: float
LEASE_TTL_S = 60.0
```
Filter request body: `{"v":1,"stage","job_id","mode","action_id","text","language","app"}`.

### `services/automation/service.py` (FULL, coordinator-owned)

`start(controller)` after the window shows: `webhooks.start()`,
`programs.start()`, `services.agent_mcp.events_ext.start_delivery()`, each
in its own try/except; `stop()` on quit; `reload(kind)` from Settings
(`kind` in `webhooks`, `programs`, `filters`, `mcp_events`).

### `services/automation/control.py` (FULL protocol)

```python
class ControlError(Exception): code: str ("busy","not_found","unavailable","refused"); message: str
@dataclass(frozen=True) class LiveState: recording: bool; busy: bool; meeting: bool; mode: str;
        action_id: str; job_id: str; version: str
class AppControl(Protocol):
    def state(self) -> LiveState
    def list_actions(self) -> list[dict]            # Action.public_dict()
    def run_action(self, action_id: str, text: str, *, source: str) -> str      # job_id
    def start_recording(self, *, action_id: str = "", source: str) -> str       # job_id
    def stop_recording(self) -> None
    def cancel(self) -> None
    def notify(self, text: str, *, title: str = "") -> None
def set_app_control(control: Optional[AppControl]) -> None
def app_control() -> Optional[AppControl]
```
All methods are callable from any thread (the uvicorn thread) and return
within 2 s; `services/automation/app_control.py` (A) implements it with a Qt
relay created on the application thread (see the Qt worker relay pattern).

### `services/automation/api.py` (STUB → A)

```python
def create_live_router(settings) -> fastapi.APIRouter
```
Mounted by `services/agent_mcp/app.py` on the in-app listener only (never by
the headless `--api`). Routes (all JSON errors use the History API's
`{"error":{"code","message"}}` shape; auth is the app's existing middleware):

| Route | Permission | Result |
| --- | --- | --- |
| `GET /v1/live/state` | token | `LiveState` + `permissions` + `listeners` |
| `GET /v1/events?types=a,b&since=<seq>` | live_events | `text/event-stream`; `id: <seq>`, `event: <type>`, `data: <json>`; `Last-Event-ID` honoured; `: ping` every 15 s; `events.gap` first when the buffer no longer holds `since` |
| `GET /v1/actions` | token | `{"actions":[…]}` |
| `POST /v1/actions/{id}/run` `{"text"}` (1–20 000 chars) | run_actions | 202 `{"job_id"}`; 409 busy; 404 |
| `POST /v1/recording/start` `{"action_id"?}` | control_recording | 202 `{"job_id"}`; 409 |
| `POST /v1/recording/stop` / `/cancel` | control_recording | 202 |
| `POST /v1/filters` `{"url","stage","timeout_ms"?}` | text_filters | 201 `Lease` |
| `POST /v1/filters/{id}/renew`, `DELETE /v1/filters/{id}` | text_filters | 200 / 204 |
| `POST /v1/notify` `{"text","title"?}` (≤ 500 chars) | run_actions | 202 |

SSE streams end when MCP is turned off or the server stops, and when the
client disconnects. A client that sends `X-OpenWhisper-Client: claude-channel`
is counted as a channel listener (`bus().subscribers("claude-channel")`).

### `services/automation/meeting_events.py` (STUB → A)

`forward(kind: str, payload: Mapping, *, meeting_id: str = "") -> None` — called
on the Qt thread for every engine event of the current meeting (after
MeetingRuntime's own handling); maps to `meeting.*` and publishes. Payloads may
carry private `_`-prefixed keys; never forward them.

### `services/automation/channel_bridge.py` (STUB → E)

`main(argv: list[str]) -> int` — the `--channel` entry point (runs before Qt).
`services/agent_mcp/setup.py` (E) also provides `channel_command_line() -> list[str]`,
`claude_channel_config() -> str` (the `.mcp.json` entry) and
`claude_channel_command() -> str`; the foundation has working first versions.

### `services/agent_mcp/events_ext.py` (STUB → D)

`start_delivery()`, `stop_delivery()`, `reload()`, `subscription_count() -> int`,
`clear_subscriptions() -> int` (Settings shows the count and a Clear button).

### `services/actions/model.py` (FULL, light: settings and cleanup_profiles)

```python
class ActionKind: OPEN_LINK, AGENT, CHANNEL, WEBHOOK, PROGRAM, CLIPBOARD, SCRIPTS; ALL
class Output: NONE, PASTE, SCRATCHPAD, NOTICE, CLIPBOARD; ALL
SHAPING_RAW = "raw"; SHAPING_CLEANUP = "cleanup"  # also "profile:<id>", "transform:<id>"
@dataclass(frozen=True)
class Action:
    id: str; name: str; kind: str
    params: Mapping[str, Any] = {}          # per kind, see below
    hotkey: str = ""; shaping: str = "cleanup"
    spoken_names: tuple[str, ...] = (); description: str = ""; enabled: bool = True
    def to_dict(self) -> dict; def public_dict(self) -> dict   # no secrets, no program path
    def profile_id(self) -> str; def transform_id(self) -> str
load_actions(settings) -> list[Action]; find_action(settings, id) -> Action|None
save_action(settings, action); delete_action(settings, id)          # mutate_settings mutators
validate_action(action, settings) -> None                          # ValueError copy
action_hotkey_conflict(hotkey, settings, *, exclude_id="") -> str
expand_link(template: str, text: str) -> str                       # ValueError on refused schemes
PRESETS: tuple[Preset, ...]   # id, name, kind, params, shaping, description, icon
AGENT_ACTION_ID = "agent"; MAX_ACTIONS = 50
AGENTS = ("claude_code", "codex", "opencode_cli")   # services/installed_agents ids
Action.param(key) -> value or the kind's default
action_secret_name(id) -> "OPENWHISPER_ACTION_SECRET_<id>"
```
Params by kind (unknown keys are dropped on load):

| Kind | Params |
| --- | --- |
| open_link | `template` |
| agent | `agent` (claude_code/codex/opencode_cli), `mode` (handoff/ask), `surface` (auto/app/terminal), `folder`, `include_selection` (bool), `model`, `output` |
| channel | — |
| webhook | `url`, `output` |
| program | `program`, `args` (list of str), `output`, `timeout_s` |
| clipboard / scripts | — |

### `services/actions/runtime.py` (STUB → C)

```python
@dataclass(frozen=True) class ActionResult: ok: bool; message: str; output: Optional[str] = None
class ActionRuntime:
    def __init__(self, controller) -> None
    def deliver(self, job, text: str, *, raw_text: Optional[str], html: str) -> None
        # Qt thread, returns at once; runs the job's action, shows the outcome,
        # publishes action.ran (and channel.message for channel actions)
    def test(self, action, sample_text: str, done: Callable[[ActionResult], None]) -> None
        # Settings "Try it"; done on the Qt thread
    def close(self) -> None
```
The foundation stub copies the text to the clipboard and says "Copied —
actions aren't ready yet". `deliver` must call
`ui.discard_clipboard_prefetch()` (a routed dictation took a clipboard
snapshot for a paste that will not happen). A job's selected text, when an
agent action asked for it, is `job.snapshot().text_context` with
`selection_known` (UI Automation only, never a synthetic copy); it exists
only while app awareness is on. Answers go to the Scratchpad with
`ui.show_in_scratchpad(text)`; a later paste uses
`transcription_runtime.paste_text_now(text)` after checking
`dictation_pipeline.paste_target(job)`.

### `services/actions/agents.py` (STUB → C2) and `launch.py` (C2; `open_uri` FULL)

```python
# agents.py
def available() -> dict[str, bool]                 # cached scan, never blocks the Qt thread
def handoff(agent_id: str, text: str, *, folder: str = "", surface: str = "auto") -> ActionResult
def ask(agent_id: str, text: str, *, model: str = "", timeout_s: float = 120.0,
        cancel: Optional[threading.Event] = None) -> ActionResult    # output = the answer
# launch.py
class LaunchError(RuntimeError): ...
def open_uri(uri: str) -> None                     # os.startfile / open / xdg-open; LaunchError
def handler_available(scheme: str) -> Optional[bool]   # None = can't tell
def open_in_terminal(argv: Sequence[str], *, cwd: str = "") -> None   # LaunchError
```
Both blocking; callers use a worker. Text goes to `open_in_terminal` as one
argv element and must never pass through cmd.exe, a shell or AppleScript
source.

### `services/actions/routing.py` (STUB → F)

```python
@dataclass(frozen=True) class RouteDecision: action_id: str; payload: str; confidence: float
@dataclass(frozen=True) class RoutingStatus: enabled: bool; ready: bool; message: str
def decide(text: str, job, settings: Mapping, *, judge=None) -> Optional[RouteDecision]
        # worker thread; None = paste as a normal dictation. Publishes route.decided.
def status(settings: Mapping) -> RoutingStatus
ROUTE_TIMEOUT_S = 1.5
```

### Pipeline seams (foundation, coordinator-owned)

- `JobMode.ACTION = "action"`. `DictationJob` gains `id` (16 hex chars,
  default factory), `action_id: str = ""`, `source: str = ""`,
  `route: str = ""` (`"hotkey"` for route_record, `"auto"` when routing on
  dictation applies), `routed: bool = False`.
- `TranscriptionRuntime.start_recording(profile_id="", mode=…, *, selection=None,
  action_id="", source="hotkey", route="")`.
- `TranscriptionRuntime.run_action_text(action_id, text, *, source) -> str` (job id;
  raises `ControlError`). Qt thread.
- `dictation_pipeline.begin_job(mode, settings, *, selection=None, action_id="",
  source="", route="", include_selection=False)`.
- `CommandRuntime.rewrite_text(text, instruction) -> (text, raw, info)` (transform
  shaping; executor worker, job slot held).
- A text filter's `canceled` saves the words to history and delivers nothing
  (status `FILTER_CANCELED_MESSAGE`); so does a filter answer that is empty.
- `_maybe_cleanup_transcript(raw, …, profile=…)` takes the profile explicitly
  for routed jobs.
- `ApplicationController`: `action_record_requested = pyqtSignal(str)`,
  `toggle_action_recording(action_id)`, `toggle_route_recording()`,
  `self.action_runtime`, `on_settings_changed` kinds `actions`, `webhooks`,
  `programs`, `filters`, `mcp_events`.
- `UIController.show_in_scratchpad(text)` opens the Scratchpad and appends a block
  (`ui_qt.widgets.scratchpad.show_text`). Qt thread.
- Hotkeys: `DYNAMIC_NAMESPACES` gains `action`; `hotkey_conflicts.ACTION`;
  `PRESS_ACTIONS` gains `("route_record", "on_route_record")`;
  `config.DEFAULT_HOTKEYS["route_record"] = ""`.
- `services/runtime/meeting.py` calls `meeting_events.forward(kind, payload)`
  from `_route_engine_event`.
- `main.py`: `--channel` early CLI → `channel_bridge.main(argv)`.
- History `entry_kind` gains `action`; `source_name` is `Action · <name>`.

### Settings page modules (protocol from the personalize design)

The Actions page tries an action with `dialog.on_try_action(action, sample_text,
done)` (set by UIController from `ActionRuntime.test`; `done(ActionResult)` runs on
the Qt thread).

`ui_qt/dialogs/settings_actions.py` (G) and `ui_qt/dialogs/settings_hooks.py`
(H) implement TITLE, SUBTITLE, SEARCH_FIELDS, CONTROL_ATTRS, build, load,
rail_value, refresh, optional save_drafts. They save through
`settings_manager.mutate_settings` and then `dialog.notify_changed(kind)`.
The MCP page keeps its module (`settings_mcp.py`, H).

## Storage

| Key | Type / default | Owner |
| --- | --- | --- |
| `actions` | list of action dicts, [] | model (foundation), UI G |
| `voice_routing_enabled` | bool, False | F / G |
| `voice_routing_on_dictation` | bool, False | F / G |
| `api_live_events` | bool, True | A / H |
| `api_run_actions` | bool, False | A / H |
| `api_control_recording` | bool, False | A / H |
| `api_text_filters` | bool, False | A / H |
| `api_agent_prompts` | bool, False | review fixes / H |
| `automation_webhooks` | list, [] | B / H |
| `automation_programs` | list, [] | B / H |
| `automation_filters` | list, [] | B / H |
| `mcp_events_enabled` | bool, False | D / H |
| `hotkeys.route_record` | "" | foundation |

Secrets live in the credential store, never in settings:
`OPENWHISPER_WEBHOOK_SECRET_<id>`, `OPENWHISPER_ACTION_SECRET_<id>`,
`OPENWHISPER_MCP_EVENT_SECRET_<subscription id>`. MCP Event subscriptions
persist in `config.MCP_EVENT_SUBSCRIPTIONS_FILE` (JSON, data root).
Backup restore: every webhook, program, filter, and webhook/program/agent
action comes back disabled; `api_run_actions`, `api_control_recording`,
`api_text_filters`, `api_agent_prompts`, `mcp_events_enabled` false; `actions` is exempt from the
URL scrub (link templates need their query).

## Ownership

**Coordinator-owned after the foundation:** `services/runtime/transcription.py`,
`services/dictation_pipeline.py`, `services/application_controller.py`,
`services/runtime/hotkeys.py`, `services/runtime/meeting.py`,
`services/_hotkey_*.py`, `services/hotkey_conflicts.py`, `services/settings.py`,
`config.py`, `main.py`, `services/backup.py`, `ui_qt/ui_controller.py`,
`ui_qt/widgets/scratchpad.py`, `ui_qt/dialogs/settings_dialog.py`,
`ui_qt/dialogs/settings_destinations.py`, `ui_qt/dialogs/settings_metadata.py`,
the FULL modules above, `tests/conftest.py`, `tests/fakes/*`. A stream that
truly needs a change there makes the smallest possible edit in a separate
commit whose subject starts with `seam:` and lists it in its report.

**Shared with anchors:** `ui_qt/styles/theme.qss` has one marked block per UI
stream at the end (`/* automation:G begin */ … /* automation:G end */`, same
for H). `ui_qt/widgets/__init__.py` `_EXPORTS`: do not add entries.

| Stream | Owns |
| --- | --- |
| A Live API | `services/automation/api.py`, `app_control.py`, `meeting_events.py`, `tests/test_automation_api_*.py` |
| B Hooks engine | `services/automation/webhooks.py`, `programs.py`, `filters.py`, `tests/test_automation_hooks_*.py` |
| C Actions runtime | `services/actions/runtime.py`, `tests/test_automation_actions_*.py` |
| C2 Agents | `services/actions/agents.py`, `services/actions/launch.py`, `tests/test_automation_agents_*.py` |
| D MCP | `services/agent_mcp/app.py`, `controls.py`, `events_ext.py`, `tests/test_automation_mcp_*.py` |
| E Channel | `services/automation/channel_bridge.py`, `services/agent_mcp/setup.py`, `tests/test_automation_channel_*.py` |
| F Routing | `services/actions/routing.py`, `benchmarks/voice_routing/`, `tests/test_automation_routing_*.py` |
| G Actions UI | `ui_qt/dialogs/settings_actions.py`, `ui_qt/widgets/action_*.py`, theme block G, `tests/test_automation_ui_actions_*.py` |
| H Hooks & API UI | `ui_qt/dialogs/settings_hooks.py`, `ui_qt/dialogs/settings_mcp.py`, theme block H, `tests/test_automation_ui_hooks_*.py`, `tests/test_automation_ui_api_*.py` |

Cross-stream calls go only through the contracts above. A UI stream works
against the FULL models and the STUB signatures; it must render sensibly
when a stub returns nothing. Merge order: B, A, C2, C, D, E, F, H, G.

## Rules for every stream

- Your worktree must start at the foundation commit: check that
  `git merge-base --is-ancestor <foundation sha> HEAD` succeeds, else
  `git reset --hard <foundation sha>` before any work.
- Commit after each working piece with a clear message; never `git stash`;
  never merge or cherry-pick other branches; never push.
- Tests: new files only, named for your stream; edit existing tests only
  where your change legitimately alters behaviour they pin, and say so.
- Python: `D:/coding/whisper_local/.venv/Scripts/python.exe` with cwd = your
  worktree. Run your focused tests, `ruff check --select F .`, and once at the
  end the full suite `-m pytest tests -q -n auto`; compare against the
  baseline (about 11 failures, all `tests/test_app_update_macos.py`
  `os.statvfs`, plus rare load flakes — rerun a failing file alone first).
- Qt: no bare `QTimer.singleShot` lambdas (they abort pytest silently); use
  owned QTimers. Workers and listeners reach widgets only through a lazily
  created module relay on the application thread with request tokens
  (`ui_qt/widgets/agent_picker.py` `_ScanRelay`). Never create QObjects at
  module import time. Never call `setObjectName` on themed widgets; use a
  `tileId` property.
- Never touch the real clipboard, keyboard, microphone, credential store or
  network outside fakes; never spawn a real agent CLI in tests (use
  `tests/fakes/fake_agent_cli.py`-style fakes). Read-only probes of real CLIs
  (`--help`, `--version`) during development are fine.
- Never log dictated text, event data, URLs with queries, secrets, program
  arguments, or selections.
- Keep startup imports light: import fastapi/httpx/subprocess-heavy modules
  inside functions.
- UI: verify classic and Omarchy (`OPENWHISPER_UI`) at narrow widths and 130%
  font; render screenshots with the UI screenshot workflow and include paths
  in your report.
- Do not edit CHANGELOG, README, docs or this file. Report user-visible
  changes, contract gaps, and anything you could not verify.

## Changes during integration

Deviations the streams made, and why; the code is the reference.

- **Selection context** is `job.snapshot().text` (`TextContext.selected` with
  `selection_known`, present only when the job asked for it in `begin_job`),
  not `text_context`. A routed dictation, or any job
  with `route` set, records `target` even with app awareness off, so a later
  answer can be pasted into the app it started in.
- **Paste for answers**: `TranscriptionRuntime.paste_text_now(text, *, job=None)`
  lets an action's answer paste while its own job still holds the slot.
- **Webhooks** expose `post_signed_reply(...) -> (DeliveryRecord, body)`,
  `msg_id=`/`headers=` keywords, `SecretUnavailable`, `user_agent()`;
  `programs.child_env(extra)` (program hooks and program actions both use
  it; built on `process_env.external_env`, below);
  `filters.drop_all_leases()` (called when the listener stops). Leases need
  `api_text_filters` at dictation time, not only when registered, and
  `Lease.expires_at` is wall-clock.
- **Launch** detects URL handlers on Windows with `AssocQueryStringW` (MSIX
  apps declare protocols in their manifest, leaving a bare registry key);
  `open_in_terminal` takes `env=`, refuses `.cmd`/`.bat`/`.ps1`, and on macOS
  runs a one-shot `.command` script with every argument shell-quoted. Claude
  Code gets a lone word with a trailing space, because it reads a lone word
  naming one of its commands (`purge`, `update`) as that command even after
  `--`.
- **Agent ask**: Codex runs with `--ignore-user-config` (no user MCP servers,
  hooks or notify program; the configured default model is passed with `-m`);
  OpenCode asks over ACP with deny-all permissions.
- **Live API**: cancel when idle is `refused`; Meeting Mode during a start is
  `busy`; SSE data lines escape U+2028/U+2029/U+0085; `events.gap` has no
  `id:` and seq 0; Last-Event-ID wins over `since`.
- **MCP Events** follows ChatGPT's published shape: `capabilities.events`,
  verification challenge, delivery body `{eventId, name, timestamp, data,
  cursor}`, `X-MCP-Subscription-Id`, retries 2/10/60 s, error codes from the incubation sketch (CallbackEndpointError
  -32015 as ChatGPT documents). `events_ext` keeps its own pooled HTTP client.
- **Channel bridge** is a hand-rolled stdio server on the initialize handshake
  (2025-11-25 and older) because Claude Code registers channels only there; it
  answers `server/discover` with method-not-found, reads the Windows stdin pipe
  with PeekNamedPipe (synchronous pipe I/O serialises the process), and leaves
  `source` out of meta (Claude Code already adds it; `origin` says where the
  words came from). A session that loads the
  entry without the channel flag still counts as a listener.
- **Routing** threshold is 0.5 (measured; `benchmarks/voice_routing`), the
  gate tolerates fillers, lead-ins, name tails and sound-alike words, and Route
  by voice says "No action matched — pasted instead" when it falls back.

### After review round 1

Seven review lenses (security, concurrency, pipeline, protocol, actions
behaviour, UI, cross-platform) and six fix streams changed these contracts:

- **Event bus**: the app's bus numbers from a per-boot base,
  `int(time.time() * 1000)`, so a Last-Event-ID from before a restart reads
  as a gap instead of matching new events. Recipients are fixed when an event
  is published (a subscriber added later never sees it live; it replays
  instead). `events.clear()` empties the replay buffer and is called when
  history entries or meetings are deleted. `publish` raises TypeError for data that
  doesn't encode as UTF-8 (a lone surrogate), so no subscriber ever gets it.
- **Events**: `transcription.completed` carries `raw_text: null` for Command
  Mode and transforms (that text was the user's selection).
  `transcription.failed` has `error` (`canceled`, `no_speech`, `refused`,
  `failed`) and a user-facing `message`, never exception text. `action.ran`
  has `source` and, for Send to my scripts, `text`; `channel.message` has
  `source`. `catalog.USER_SOURCES` (`hotkey`, `ui`, `dbus`) are the sources a
  person started.
- **Permissions**: `api_agent_prompts` ("send words to your coding agents",
  off) is required, besides `api_run_actions` or `api_control_recording`,
  whenever a program runs or starts an agent or Claude Code channel action
  (`app_control._enabled_action`; the MCP tools go through the same path).
  Since round 2 it is also enforced at delivery, on where the words came
  from (below).
- **Channel bridge**: each message's meta has `origin` (`dictated`,
  `program`, `openwhisper`), and the instructions tell Claude Code to treat
  only `dictated` as the user's request. The bridge sends the MCP token only
  to a loopback `OPENWHISPER_URL`, and forwards `events.gap` as an
  `openwhisper` notice.
- **MCP**: the MCP app is routed at `/mcp` only; other paths reach the live
  API's JSON 404. MCP Events: any non-2xx answer, 410 included, fails that
  delivery only; a subscription ends when it is unsubscribed, expires or is
  cleared. The registry lock is never held across network I/O, and its file
  is replaced with a short retry on Windows (a reader holding it open).
  A Tailscale peer's subscriptions are delivered only while agents over
  Tailscale are allowed, and a peer can't name a callback on this computer or
  its network, by address or by a name resolving there (checked again before
  each delivery).
- **Webhooks**: one delivery lane (bounded queue + thread) per webhook, so a
  slow receiver only delays its own events. A delivery makes a missing secret
  only while the webhook is still saved (`_delivery_secret`), so a delivery in
  flight never recreates a deleted webhook's secret.
- **Programs**: `run_once(..., cancel=)`; canceling or timing out kills the
  whole process tree, while a clean exit leaves children it launched running.
  Program filters pass the dictation's cancel through.
- **Actions**: `ActionRuntime.deliver(job, text, *, raw_text, html,
  notice="")` shows the notice with the outcome; `cancel(*, tests_only=False)`
  drops queued runs and stops agent quick answers and programs; a run under
  way that can't be stopped finishes, `action.ran` reports its real outcome
  with `output: none`, and its answer goes nowhere. Unless `tests_only` it
  says what it canceled. `accepting()` is False at `MAX_PENDING` (8) runs.
  A shaping problem never loses the words: `_shape_action` falls back to the
  cleaned words with a notice, and only Command Mode's refusal drops audio.
  Answers from program-started runs never touch the clipboard.
- **Routing**: `decide` waits at most `ROUTE_TIMEOUT_S` of wall-clock time and
  returns `RouteDecision(failure="timeout"|"unreachable")` when the judge
  couldn't answer, so Route by voice says why it pasted; `RoutingStatus.fix`
  names what is missing (`fast_judgments`, `actions`, `trigger`). The gate no
  longer hears "could", "called" or "cold" as Claude.
- **Environment**: `services/process_env.py` records every change the app
  makes to its own environment (`set_for_app`, `prepend_for_app`);
  `external_env()` undoes them plus the frozen loader's paths and the app's
  virtualenv. `installed_agents.agent_child_env` and `programs.child_env`
  build on it, so links, terminals, agents, hooks and actions start in the
  user's environment.
- **Hotkeys**: `hotkey_conflicts.STANDARD_LABELS` is the one label map for
  standard shortcuts.

### After review round 2

- **Cancel**: `TranscriptionRuntime.cancel() -> bool` (False when nothing was
  recording, transcribing or holding the slot). `ApplicationController.cancel()`
  (the user's) reaches actions only then, via `actions_cancel_requested` onto
  the Qt thread. The live API's and MCP's cancel reach recordings and jobs only
  (`app_control._cancel` calls `transcription_runtime.cancel()`), refused when
  there is none. `ActionRuntime.running()` is gone.
- **Previews**: `TranscriptionRuntime.publish_preview(job, text, final)`
  publishes `transcription.preview` only for the recording in progress, under
  `_job_lock`, from after `recording.started` until before
  `recording.stopped`/`recording.canceled`. `PreviewEvents(publish)` only
  throttles.
- **Job slot**: each claimed job gets a fresh cancel Event (`_claim_job`; the
  cleanup client follows it), so a later job never un-cancels a filter or
  routing wait left by the previous one. `begin_rewrite_job` takes the capture
  lock without waiting; contention (a start, stop or cancel) is busy.
- **Where words came from**: `DictationJob.changed_by_program` and the
  `program_words` property (a program changed the words, or
  `catalog.user_started(source)` is false). `services.actions.runtime.
  program_words_allowed(action, settings)` with `AGENT_KINDS` is the one rule,
  used by `ActionRuntime.deliver` (refuses; copies for the user's runs),
  `run_action_text`, `app_control._enabled_action` and lease selection.
  `FilterOutcome.program_changed`; leases skip agent/channel action jobs
  without `api_agent_prompts`; `TranscriptionRuntime._run_filters` returns
  `(job, text)` and marks `changed_by_program`. `channel.message` carries
  `origin` (`catalog.ORIGIN_DICTATED` / `ORIGIN_PROGRAM`); the bridge reads it
  from the event, not the source.
- **Selection**: text readers return the selection only with
  `include_selection`; `begin_job` asks for it only for the user's jobs
  (Command Mode, a dictation with text near the cursor on, an action that
  wants it); `transcription._reads_selection(action)` takes the action only.
- **Routing gate**: a name heard as several words counts when a request
  follows ("chat gpt what's…", "to do buy milk", not "to do that…"), and a
  shortened name counts after lead-ins when it is typed or heard as a name
  (`_written_as_name`), so lowercase-typed names work. Benchmark: 146 cases.
