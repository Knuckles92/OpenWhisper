# Personalization features: design and work split

Status: implemented (2026-10-06), revision 3. Revision 2 followed a four-lens
design review; revision 3 records the contract changes made while fixing the
verified findings of the post-merge review (see "Changes after review" at the
end). This is the shared contract for the parallel work streams that add
personalization features to OpenWhisper. Every stream reads this first.

The coordinator owns this file, CHANGELOG.md, README.md, docs/*.md user
guides, and the files marked **coordinator-owned** below. Streams report
user-visible changes and any needed change to a coordinator-owned file in
their final report instead of editing them.

## Principles

- Clean, calm UI built from the existing Settings design system (SettingTile,
  FieldTile, InfoTile, SettingsSwitch, SegmentedBar, neutral_button,
  compact_primary_button). Warm, honest motion only on real signals.
- Reliability first. A slow, missing or failing accessibility read, model,
  device, hook or hint never delays or breaks a dictation. Timeouts mean "no
  context", never an error. Every pipeline stage returns its input unchanged
  when it has nothing to do, so the default path behaves exactly as today.
- Local first and private by default. Knowing *which app* is native, local
  and on by default (no accessibility tree walks). Reading *text from other
  apps* is opt-in, never logged, never stored in history, and only sent to
  the AI cleanup provider the user already configured. It is never sent to a
  speech engine or remote host.
- No new third-party dependencies (Windows ctypes, the pyobjc frameworks
  pynput already brings, hyprctl / python-xlib).
- Classic and Omarchy offer the same features (CONTRIBUTING, Desktop
  feature parity). Where a platform cannot do something, the UI says so.
- No surprises on upgrade: existing users keep today's output unless they
  turn something on. New installs get the casual chat tones.

## Product decisions

**1 Active app.** `app_context_enabled` (default on) captures the foreground
app at record start with native calls only: Windows
GetForegroundWindow → pid → QueryFullProcessImageNameW + class + window title
(title kept in memory only), macOS NSWorkspace, Hyprland `hyprctl
activewindow`, X11 `_NET_ACTIVE_WINDOW`, else unknown. Browser sites come from
window-title heuristics (Gmail, Outlook, Slack, Teams, Discord, WhatsApp,
Messenger, LinkedIn, Notion, Google Docs…). `app_context_read_text` (default
off) additionally reads up to 1000 characters before and 200 after the caret,
plus the selection, through UI Automation TextPattern on Windows. macOS AX
text reading stays behind a code flag (off) until tested on a Mac. Never for
password fields, secure input, OpenWhisper's own windows, excluded apps or
terminals (identity only).

**1 Use of context.** Text before the caret feeds (a) the cleanup prompt as
delimited data followed by an injection guard, and (b) a deterministic join
step that fixes the leading space and first-letter case when continuing
mid-sentence. In code editors the prompt asks to keep identifiers verbatim;
in terminals it forbids line breaks. Other apps' text is never sent to the
speech model.

**2 Styles.** Categories Email, Work messages, Personal messages, Other.
Tones Formal, Casual, Very casual. `app_styles_enabled` default on. Existing
installs: every category Formal, which is today's output. New installs are
seeded with Email Formal, Work Casual, Personal Casual, Other Formal. Styles
only change output when AI cleanup is on, and the page shows the same
"AI cleanup is off · Turn on (Medium)" gate as Learned rules. Built-in
app/site catalogue plus user overrides. An explicit cleanup profile always
wins over a style.

**3 Command Mode.** A `command_mode` shortcut (no default; set on Settings →
Commands or Hotkeys). It follows the recording trigger mode: hold to speak in
push-and-hold, press to start and press again in toggle. The selection is
read through UI Automation first (no keystrokes); only when that is unknown,
and after the hotkey's modifiers are released, through a synthetic copy that
restores the user's clipboard. Never send a copy to terminals. In editors that
copy the whole line on an empty selection, a clipboard-only result counts as
no selection. Selected text is rewritten in place. With no selection the
instruction generates text at the cursor (`command_mode_insert_without_selection`,
default on); an instruction that refers to existing text ("make this shorter")
with nothing selected shows "Select the text to change first" and pastes
nothing. Uses the AI cleanup provider/model even when the cleanup level is
None; without a provider it says so. Always pastes (that is its purpose),
after checking the foreground app is unchanged; otherwise it copies and says
so. Results go to history (entry_kind command) with the original selection as
raw text. The Commands page says rewritten text is saved in History.

**3 Transforms.** Saved rewrite instructions with optional shortcuts
(`transform:<id>` family). Starters: Polish, Make concise, Fix grammar,
Prompt engineer. A shortcut rewrites the current selection; the Scratchpad can
apply them too.

**4 Dictionary.** Terms with a star for priority, optional user-entered
"sounds like" variants, and a Learned flag. Applied (a) to the speech model
where the engine supports it and it is verified: faster-whisper `hotwords`,
OpenAI `keywords` (gpt-transcribe) or `prompt` (other models), remote hosts
that advertise `recognition_hints`, Nemotron word boosting only if verified
with the pinned model; any hint failure retries once without hints; (b)
deterministically ("sounds like" → term) to every transcript; (c) in the
cleanup prompt. The page says per engine whether the dictionary reaches the
speech model, from a capability flag. Learning (default on, but shown as
"Off · needs Read text near the cursor [Open Apps & styles]" until that is on): after a
paste the field is re-read once a few seconds later on the capture service
thread; a single-word correction of a pasted word, seen twice and not on a
common-word list, becomes a Learned term (no "sounds like" variant, so it only
steers the model and the prompt). New terms get a "New" badge on the page and
in the rail value ("12 words · 2 new"), with Undo there.

**5 Snippets.** Trigger phrase → exact text, no AI. A whole-utterance trigger
skips cleanup entirely. Triggers inside a sentence become protected
placeholders before cleanup and are expanded after; if cleanup loses a
placeholder the uncleaned text with expansions is used and the cleanup is
reported as failed. Optional "Keep formatting": light Markdown rendered to
HTML and pasted as rich text with a plain-text alternative. Live dictation
only.

**6 Corrections and lists.** In the Medium and High presets (and the profile
preamble, corrections only): explicit retractions keep only the corrected
version; spoken enumerations become lists, except in terminals or unknown
apps, where items stay inline. Light never restructures.

**7 Levels.** The AI cleanup switch stays the master on/off (None = off).
`transcript_cleanup_level` light/medium/high, default medium. A saved custom
prompt still replaces the preset and the level control then shows "Custom";
picking a preset asks before discarding the custom prompt. A saved prompt
equal to the old default counts as no custom prompt. Reset removes the key.

**7 Undo AI edit.** Never swap fields. `raw_text` is the text before AI
cleanup (after dictionary and snippet steps), stored only when AI changed the
text; `cleaned_text` is the AI output, stored at the same time (legacy rows:
filled on the first undo); `text` is whichever version the user chose. The
entry dialog's toggle becomes "Original / AI" with "Use this version"; the
card menu gets "Undo AI edit" / "Use AI version". Local entries only; edits
re-sync to the host. An optional `paste_last_original` shortcut pastes the
last dictation's original at the caret once its modifiers are released and
marks the entry as original; the tray's "Copy original of last dictation"
copies it instead (a tray click takes focus from the target app) and leaves
the entry's version alone.

**8 Hands-free.** `recording_hands_free_latch` (default on), push-and-hold
only: a second press within 400 ms of a short tap keeps recording; the next
press stops. A lone short tap still cancels, 400 ms later. The overlay shows a
lock and "Hands-free". Hotkey events are dispatched in order on one thread
with hook timestamps.

**9 Mouse buttons.** `mouse4` / `mouse5` (Back/Forward), optionally with
modifiers. Windows: a dedicated ctypes WH_MOUSE_LL hook thread, installed only
while a mouse binding exists, suppressing the bound button (the capture UI
says it no longer works as Back/Forward). X11: pynput mouse listener without
suppression, with a warning before saving a bare side button. macOS and
Hyprland: not in v1; capture explains why.

**10 Mic ranking.** `audio_input_priority` (ordered `{name, hostapi}`) is the
single source of truth; the legacy `audio_input_device` index migrates to its
first entry on first load (the old key stays for downgrades). The Basic and
Recording "Microphone" choice writes the first entry. The Recording page shows
the ordered list with Up/Down and a fixed "System default" last row. Matching
uses the host API and a name prefix (MME cuts names to 31 characters); WASAPI
entries open with auto-convert; the final Windows fallback is the MME Sound
Mapper. Start uses the first that opens. Mid-recording loss (inactive stream,
finished callback, or ~1 s stall; never an overflow) opens the next candidate
in the same recorder and journal, and the overlay says "Switched to <mic>".
No PortAudio re-initialisation while any stream is open; an idle-only Refresh
button on the Recording page. Meeting Mode uses the same list and excludes a
dead device when restarting. Needs a real unplug test before release.

**11 Languages.** `dictation_languages` (languages you dictate in, merged
with the engine language control on Voice model into one list) and
`dictation_active_language`. With two or more choices the engine supports,
the overlay shows a chip (clickable on Windows and X11, display-only
elsewhere), plus a `cycle_language` shortcut, a tray submenu and an Omarchy
bar entry. The active language is read when the final pass starts, so a
change during a dictation applies to it. It never reloads the engine and
never writes `local_asr_language`.

**12 Stats.** A non-modal Stats window from View → Stats and the tray (not a
Settings page): words this week and all time, average words per minute,
words AI cleaned up, day streak, top apps, a 14-day bar strip, Reset stats.
Backed by a local-only `dictation_stats` table written when history is saved,
so it survives host moves and Clear history. Named Stats to avoid confusion
with Meeting Mode's AI insights.

**13 Scratchpad.** Floating notepad (always on top by default) from a
shortcut, View menu, tray and Omarchy button. Dictation while it is focused
inserts directly into it. Autosaved to `scratchpad.txt` in the data root
(backed up). Copy all, Clear, Transform.

**14 Playback.** Play/Stop for dictation audio in the history card and entry
dialog through a dedicated sounddevice OutputStream (QtMultimedia is not
bundled). Refused while recording or in a meeting; stopped when a recording or
meeting starts. Host-kept entries fetch audio first.

**Discoverability.** The Basic Dictation tab gets a Personalize group filled
by page modules (dictionary count + Add word, snippets count, Match tone to
each app, Command Mode shortcut). A dismissible "New in OpenWhisper" tile on
Overview and Basic (`flow_features_intro_seen`). The Quick Record hint shows
the Command Mode shortcut or "Set a Command Mode shortcut".

## Architecture

```
hotkey/UI ──► TranscriptionRuntime.start_recording(profile_id, mode, selection=None)
   audio_player.stop_playback(); recorder.start_recording()
   job = dictation_pipeline.begin_job(mode, settings, selection=selection)
        focus = focus_context.service.request(include_text=read_text, include_selection=mode==COMMAND)
        recognition = dictation_pipeline.recognition_for(job=None, settings)  (language + phrases)
   streaming preview; incremental.start(controller, recognition=job.recognition)
   overlay RECORDING (or COMMAND_LISTENING); prefetch only for DICTATION + auto-paste
        ... stop: _claim_job(job) → executor worker:
   raw = recognition_context.transcribe(incremental, backend, path, recognition_for(job, settings))
   mode != DICTATION → command_runtime.complete_recording(raw, job) → (text, raw_text, info)
   else              → _maybe_cleanup_transcript(raw)   (single positional arg, as today)
        prepared = prepare_text(raw, job, settings)           dictionary → snippet plan
        whole snippet → no AI
        cleanup on → compose_cleanup_prompt(...) → cleanup(text, system_prompt=prompt)
        finished = finish_text(cleaned, prepared)             → self._delivery_html
   transcription_completed(text, raw_text, CleanupInfo)       (3 args, Qt thread)
   on_transcription_complete:
        scratchpad focused? insert_into_scratchpad(text) is True → done
        paste(text_for_paste(text, job), html, force_paste=mode != DICTATION,
              target check paste_target_ok(job) for command/transform)
        after_paste(job, pasted)
        entry.update(history_fields(job, info, ...)) → history worker:
             add_entry(**entry); record_stats(entry, row)   (own try/except)
Transforms: CommandRuntime.run_transform → capture selection → runtime.begin_rewrite_job(job)
            → runtime.submit_rewrite(work) → same transcription_completed path.
```

Per-job state: `TranscriptionRuntime._job` (set at record start, before the
preview and incremental decode start) and `_active_job` (set by
`_claim_job(job)` under the job lock; the worker, delivery and history read
only this, copied to a local at function entry). `_finish_job` clears both;
failed stop and failed claim clear `_job` and the profile; uploads, batch and
retranscribe claim with no job and reset `_job` and the profile. Entry kind is
derived from the job mode and `_deliver_to_clipboard`/source, never from
`job is None` (a live dictation can legitimately have no job in tests).

## Contracts

The foundation creates every module below with inert bodies and fixed
signatures. Streams fill in bodies and may add private helpers and new
public functions, but must not change a listed signature without telling the
coordinator.

`services/recognition_context.py` (S4 fills `transcribe`)
```python
@dataclass(frozen=True)
class RecognitionContext:
    language: str = ""             # "" = the engine's own setting
    phrases: tuple[str, ...] = ()  # vocabulary, starred first, budgeted
    def __bool__(self) -> bool
def transcribe(incremental, backend, audio_path: str, recognition: RecognitionContext | None) -> str
    # foundation: return incremental.transcribe(backend, audio_path)
```

`services/focus_context/__init__.py` (S1)
```python
@dataclass(frozen=True)
class AppIdentity: app_id: str; name: str; pid: int | None = None; window: str = ""
                   title_hint: str = ""; is_self: bool = False; platform: str = ""
                   # title_hint = site/product derived from the title (never the raw title)
@dataclass(frozen=True)
class TextContext: before: str = ""; selected: str = ""; after: str = ""
                   caret_known: bool = False; selection_known: bool = False; source: str = ""
@dataclass(frozen=True)
class FocusSnapshot: identity: AppIdentity | None = None; text: TextContext | None = None
class ContextCaptureService:
    def request(self, *, include_text: bool, include_selection: bool = False) -> Future  # Future[FocusSnapshot]
    def current_identity(self) -> AppIdentity | None   # sync; None where identity is async-only
    def reread(self, identity: AppIdentity, callback) -> None  # runs on the service thread; callback(TextContext | None)
    def shutdown(self) -> None
def get_service() -> ContextCaptureService       # lazy singleton; tests install a null one
def set_service(service: ContextCaptureService | None) -> None
def prompt_block(snapshot: FocusSnapshot | None) -> str
def join_with_context(text: str, context: TextContext | None) -> str
```
Service semantics (S1 must honour): Windows and macOS identity computed
synchronously inside `request()` (sub-millisecond); Hyprland/X11 identity on
a small separate thread; text on a dedicated UIA thread with
ConnectionTimeout ≈500 ms and TransactionTimeout ≈1000 ms; the Future always
resolves within `config.CONTEXT_CAPTURE_DEADLINE_S` with identity and text or
None; while a text read is busy new requests get identity-only immediately; a
wedged thread is abandoned and replaced at most a few times per session, then
text capture is disabled for that app; every COM pointer and BSTR released in
`finally`. Qt-thread callers use only `job.snapshot(timeout=0)`.

`services/app_styles.py` (S1)
```python
class AppCategory: EMAIL="email"; WORK="work"; PERSONAL="personal"; OTHER="other"; ALL
class Tone: FORMAL="formal"; CASUAL="casual"; VERY_CASUAL="very_casual"; ALL
class Surface: TEXT="text"; CODE="code"; TERMINAL="terminal"
@dataclass(frozen=True)
class AppStyle: category: str; tone: str; app_name: str = ""; surface: str = "text"
def resolve_tones(settings: Mapping) -> dict[str, str]
def style_for(snapshot: FocusSnapshot | None, settings: Mapping) -> AppStyle | None
def prompt_block(style: AppStyle | None) -> str
NEW_INSTALL_TONES: dict[str, str]   # seeded on first run
```

`services/cleanup_prompts.py` (S2)
```python
class CleanupLevel: NONE="none"; LIGHT="light"; MEDIUM="medium"; HIGH="high"; STORED=(LIGHT, MEDIUM, HIGH)
def resolve_level(settings: Mapping) -> str           # "none" when cleanup is off
def custom_prompt(settings: Mapping) -> str           # "" unless a real custom prompt is saved
def base_prompt(settings: Mapping, level: str) -> str # custom prompt or the level preset
```

`services/dictionary.py` (S4)
```python
@dataclass(frozen=True)
class DictionaryTerm: id: str; term: str; starred: bool = False; heard: tuple[str, ...] = (); learned: bool = False; new: bool = False
def load_dictionary(settings: Mapping) -> list[DictionaryTerm]
def recognition_phrases(settings: Mapping) -> tuple[str, ...]
def apply_replacements(text: str, terms: Sequence[DictionaryTerm]) -> str
def prompt_block(terms: Sequence[DictionaryTerm]) -> str
def schedule_learning(job, pasted_text: str) -> None   # Qt thread; never blocks
```

`services/snippets.py` (S5)
```python
@dataclass(frozen=True)
class Snippet: id: str; trigger: str; text: str; formatted: bool = False
@dataclass(frozen=True)
class SnippetPlan: text: str; whole: Snippet | None = None; placeholders: tuple[tuple[str, Snippet], ...] = ()
def load_snippets(settings: Mapping) -> list[Snippet]
def plan_expansion(text: str, snippets: Sequence[Snippet]) -> SnippetPlan
def prompt_guard(plan: SnippetPlan) -> str
def expand(text: str, plan: SnippetPlan) -> tuple[str, str, bool]   # (plain, html, ok)
```

`services/text_transforms.py` (S3): `Transform(id, name, instruction,
hotkey="")`, `STARTER_TRANSFORMS`, `load_transforms(settings)`,
`save_transform(settings, t)`, `delete_transform(settings, id)` (mutators for
`mutate_settings`), `find_transform(settings, id)`.

`services/dictation_language.py` (S8): `language_choices(settings) ->
list[str]` (codes the current engine accepts, from `dictation_languages`),
`job_language(settings) -> str`, `cycle(settings) -> None` (mutator),
`label(code) -> str`, `short_label(code) -> str` ("EN").

`services/dictation_stats.py` (S9): `record(entry_fields: dict, row) ->
None` (history-save worker), `load_summary(today=None, tz=None) ->
StatsSummary`, `reset() -> None`.

`services/audio_player.py` (S9): `stop_playback() -> None` (any thread),
`player()` singleton with `play(path, on_finished)`, `stop()`, `is_playing`.

`services/synthetic_keys.py` (S3): `send_copy() -> None`,
`wait_for_modifiers_released(timeout_s=0.8) -> bool`, `is_terminal(identity)`,
`copies_line_without_selection(identity)`.

`services/hotkey_conflicts.py` (foundation, real): `hotkey_conflict(hotkey,
settings, *, exclude=("", ""), standard_hotkeys=None) -> str` across standard
actions, cleanup profiles and transforms. `profile_hotkey_conflict` wraps it.

`services/runtime/command.py` (S3)
```python
class CommandRuntime:
    def __init__(self, controller)
    def key_pressed(self, at: float) -> None    # S6's ordered dispatcher thread
    def key_released(self, at: float) -> None
    def run_transform(self, transform_id: str) -> None   # Qt thread
    def complete_recording(self, raw: str, job) -> tuple[str, str | None, CleanupInfo | None]
        # executor worker; raises RuntimeError with a user-facing message on failure
    def cleanup(self) -> None
```

`services/dictation_pipeline.py` (foundation; coordinator-owned)
```python
class JobMode: DICTATION="dictation"; COMMAND="command"; TRANSFORM="transform"
@dataclass(frozen=True)
class DictationJob:
    mode: str = JobMode.DICTATION
    focus: Future | None = None
    recognition: RecognitionContext | None = None
    selection: Future | None = None              # Future[str], command/transform
    def snapshot(self, timeout: float = 0.0) -> FocusSnapshot | None   # never raises
    def selection_text(self, timeout: float = 0.0) -> str               # "" on timeout
@dataclass(frozen=True)
class PreparedText: original: str; text: str; cleanup_input: str; plan: SnippetPlan; skip_cleanup: bool
@dataclass(frozen=True)
class FinishedText: text: str; html: str = ""; ok: bool = True; raw_text: str = ""
def begin_job(mode, settings, *, selection=None) -> DictationJob
def recognition_for(job, settings) -> RecognitionContext
def prepare_text(raw, job, settings) -> PreparedText
def compose_cleanup_prompt(*, job, settings, profile, rules, prepared, batch_context=None) -> str
def finish_text(cleaned, prepared) -> FinishedText
def text_for_paste(text, job) -> str
def paste_target_ok(job) -> bool
def after_paste(job, pasted_text) -> None
def history_fields(job, info, *, live: bool) -> dict
def record_stats(entry_fields, row) -> None
```

`TranscriptionRuntime` public additions (foundation; coordinator-owned):
`start_recording(profile_id="", mode=JobMode.DICTATION, *, selection=None)`,
`begin_rewrite_job(job, *, source_name) -> bool` (Qt thread; refuses while
recording or busy; claims the slot; REWRITING overlay),
`submit_rewrite(work)` (runs `work() -> (text, raw_text, info)` on the
executor, then the normal `transcription_completed` / `transcription_failed`
path), `abandon_rewrite_job(status)`, `paste_text_now(text) -> bool` (Qt
thread, refuses while busy, forced paste, no history).

Controller signals and UIController methods (foundation adds and wires; the
owner fills in the module function the method delegates to):

| Signal or method | Delegates to | Owner |
| --- | --- | --- |
| `command_key_pressed(at)` / `command_key_released(at)` | `command_runtime.key_*` | S3 (S6 calls) |
| `transform_requested(str)` | `command_runtime.run_transform` | S3 (S6 emits) |
| `scratchpad_toggle_requested()` | `UIController.toggle_scratchpad` → `ui_qt.widgets.scratchpad.toggle(ui)` | S8 |
| `cycle_language_requested()` | `UIController.cycle_dictation_language` | S8 |
| `paste_last_original_requested()` | `UIController.paste_last_original` → `ui_qt.history_actions.paste_last_original(ui)` | S9 |
| `hands_free_changed(bool)` | `UIController.set_hands_free` → overlay | S6 emits, S8 draws |
| `recording_device_switched(str, str)` | overlay caption + status | S7 emits, S8 draws |
| `dictionary_term_learned(str)` | status + Settings refresh | S4 |
| `UIController.insert_into_scratchpad(text) -> bool` | `scratchpad.insert(ui, text)` | S8 |
| `UIController.capture_selection(callback, *, timeout_ms)` | `TemporaryClipboard.capture_selection` (foundation, real) | S3 supplies `send_copy` |
| `UIController.stage_transcript_for_paste(text, html=None)` | `TemporaryClipboard.stage_text(text, html)` | S5 fills html |
| `UIController.show_stats()` | `ui_qt.dialogs.stats_dialog.show_stats(ui)` | S9 |
| `UIController.populate_language_menu(menu)` | `ui_qt.widgets.language_menu.populate(menu, ui)` | S8 |
| overlay `set_hands_free`, `set_language`, `show_caption` | waveform_overlay | S8 |
| `OverlayState.COMMAND_LISTENING`, `REWRITING` | routed by foundation | S3 uses, S8 draws |

`TemporaryClipboard.capture_selection(send_copy, callback, timeout_ms)`
(foundation, real; S3 must not reorder): resolve a pending lease; keep the
original (valid prefetch or a fresh capture); `send_copy()`; wait for a
sequence change or `dataChanged` (no change = ""); read the text; immediately
write the original back and re-arm the prefetch; then `callback(text)`. The
user's clipboard is intact on every exit path. COMMAND and TRANSFORM jobs
never use the start-of-recording prefetch.

Settings page modules (`ui_qt/dialogs/settings_<page>.py`):
```python
TITLE: str; SUBTITLE: str
SEARCH_FIELDS: list[tuple[str, str, str]]   # (attr, title, description) mirroring tiles
CONTROL_ATTRS: tuple[str, ...]              # dialog attributes build() sets
def build(dialog, layout) -> None
def load(dialog, settings: dict) -> None
def rail_value(settings: dict) -> str
def refresh(dialog) -> None
def cancel_capture(dialog) -> None           # optional
def basic_rows(page, group) -> None          # optional: rows in Basic → Dictation → Personalize
```
Dialog helpers for pages (foundation): `dialog.set_standard_hotkey(action,
hotkey) -> str` (the only write path for `hotkeys.*`; returns an error or ""),
`dialog.set_hotkey_capture_suspended(bool)`, `dialog.notify_changed(kind)`
with kinds `transforms`, `hotkeys`, `dictionary`, `snippets`, `styles`,
`languages`, `microphones` (routed through UIController to the controller,
which re-registers hotkeys and refreshes UI).

## Storage

Settings keys (foundation adds all of them):

| Key | Type / default | Stream |
| --- | --- | --- |
| `app_context_enabled` | bool, True | S1 |
| `app_context_read_text` | bool, False | S1 |
| `app_context_excluded_apps` | list of str, [] | S1 |
| `app_styles_enabled` | bool, True | S1 |
| `app_style_tones` | dict category→tone; absent = all Formal; seeded for new installs | S1 |
| `app_style_overrides` | list of `{match, category}`, [] | S1 |
| `transcript_cleanup_level` | light/medium/high, "medium" | S2 |
| `text_transforms` | list (absent = starters) | S3 |
| `command_mode_insert_without_selection` | bool, True | S3 |
| `dictation_dictionary` | list of `{id, term, starred, heard, learned, new}`, [] | S4 |
| `dictionary_learn_enabled` | bool, True | S4 |
| `dictionary_steer_recognition` | bool, True | S4 |
| `dictation_snippets` | list of `{id, trigger, text, formatted}`, [] | S5 |
| `snippets_enabled` | bool, True | S5 |
| `recording_hands_free_latch` | bool, True | S6 |
| `audio_input_priority` | list of `{name, hostapi}`, [] | S7 |
| `dictation_languages` | list of codes, [] | S8 |
| `dictation_active_language` | str, "" | S8 |
| `scratchpad_always_on_top` | bool, True | S8 |
| `flow_features_intro_seen` | bool, False | foundation |

Lists of objects with fixed field names only (the backup scrub drops dict
keys containing words like "token"); `dictation_snippets` is exempt from the
backup URL scrub; `scratchpad.txt` is in backup OWNED_NAMES.

New `config.DEFAULT_HOTKEYS` actions, all `""`: `command_mode`,
`scratchpad_toggle`, `cycle_language`, `paste_last_original`.

History schema v17 (foundation): nullable `app_id`, `app_name`,
`app_category`, `cleanup_level`, `entry_kind` (`dictation` | `file` |
`command` | `transform`; null on legacy rows = derive from source_name),
`cleaned_text`, `language`. No window titles, sites or captured text. New
local-only table `dictation_stats` (create_all). Paired hosts get
`entry_kind`, `cleanup_level`, `cleaned_text` and `language` through a
top-level `entry_ext` record key (S9); app fields stay on this computer. The
agent API exposes none of them.

## Settings information architecture

Rail: Overview · **Dictation** (Voice model, Remote engine, Recording, AI
cleanup) · **Personalize** (Dictionary, Snippets, Styles, Learned rules,
Profiles, Commands) · Meeting Mode · Models & storage · App. The Styles page
is titled "Apps & styles" and starts with App awareness (know which app; read
text near the cursor; excluded apps). Basic → Dictation gets a Personalize
group fed by `basic_rows`.

## Ownership

**Coordinator-owned after the foundation:** `services/runtime/transcription.py`,
`services/dictation_pipeline.py`, `services/application_controller.py`,
`ui_qt/ui_controller.py`, `ui_qt/main_window.py`, `ui_qt/system_tray.py`,
`ui_qt/dialogs/settings_destinations.py`, `ui_qt/dialogs/settings_metadata.py`,
`tests/test_application_controller.py` and `tests/test_paste_latency.py`
fakes, `tests/conftest.py`, `services/backup.py`, `services/models.py`,
`services/database.py`. A stream that truly needs a change there makes the
smallest possible edit in a separate commit whose subject starts with
`seam:` and lists it in its report.

**Shared with anchors:** `ui_qt/styles/theme.qss` has one marked block per
stream at the end (`/* personalize:S<n> begin */ … /* personalize:S<n> end */`); add rules
only inside your block. `ui_qt/widgets/__init__.py` `_EXPORTS`: do not add
entries; import widgets by module path.

| Stream | Features | Owns |
| --- | --- | --- |
| S1 Context & styles | 1, 2 | `services/focus_context/`, `services/app_styles.py`, `ui_qt/dialogs/settings_styles.py`, Overview privacy strip lines in `settings_dialog._refresh_overview`, diagnostics metric names |
| S2 Levels & prompts | 6, 7a | `services/cleanup_prompts.py`, cleanup prompt constants in `config.py`, `compose_profile_prompt`, `resolve_transcript_cleanup_prompt` and the cleanup-prompt default in `services/settings.py`, the AI cleanup page region and cleanup Basic row in `settings_dialog.py`/`settings_basic.py`, MCP level control + its `_apply_agent_changes` line (seam), `benchmarks/cleanup_levels/` |
| S3 Commands | 3 | `services/runtime/command.py`, `services/text_transforms.py`, `services/text_rewrite.py`, `services/synthetic_keys.py`, copy support in `services/hyprland.py`, `ui_qt/dialogs/settings_commands.py`, Quick Record Command Mode hint |
| S4 Dictionary | 4 | `services/dictionary.py`, `services/recognition_context.transcribe`, `transcriber/*`, `services/isolated*.py`, `services/local_asr/{worker,nvidia}.py`, `services/remote_asr/*` hint capability, `services/incremental_dictation.py`, `meeting/corrections.py`, `ui_qt/dialogs/settings_dictionary.py` |
| S5 Snippets | 5 | `services/snippets.py`, html in `TemporaryClipboard.stage_text`/`commit_text`/pre-render, `ui_qt/dialogs/settings_snippets.py` |
| S6 Hotkeys | 8, 9 | `services/_hotkey_*.py`, `services/hotkey_manager.py`, `services/_mouse_hook_win.py`, `services/runtime/hotkeys.py` (ordered dispatcher, latch, new actions, transform family, scratchpad window counts as OpenWhisper-active), `binding_key` in `services/omarchy_controls.py`, `ui_qt/widgets/hotkey_capture.py`, `ui_qt/widgets/profile_hotkey_input.py`, Hotkeys page region of `settings_dialog.py` (rows for the four new actions + latch switch), FakeHotkeyManager |
| S7 Microphones | 10 | `services/audio_devices.py`, `services/recorder.py`, Recording page region of `settings_dialog.py` + the rule-dictation recorder lines, Basic microphone row, `meeting/capture/devices.py`, meeting mic selection in `meeting/engine.py` and `services/runtime/meeting.py:965` |
| S8 Overlay, languages, scratchpad | 11, 13 | `ui_qt/overlays/waveform_overlay.py`, `services/dictation_language.py`, `ui_qt/widgets/scratchpad.py`, `ui_qt/widgets/language_menu.py`, `ui_qt/widgets/local_engine_controls.py` language list, Status()/Button() in `services/omarchy_controls.py` |
| S9 History, stats, playback | 7b, 12, 14 | `services/history_manager.py` edit methods, `services/dictation_stats.py`, `services/audio_player.py`, `ui_qt/history_actions.py`, `ui_qt/dialogs/stats_dialog.py`, history sidebar and entry dialog, `entry_ext` + text-aware digest in `services/remote_records/kinds.py`, `search_history_entries` app match |

Cross-stream dependencies and who tests what: S4 owns "RecognitionContext
language and phrases reach every engine and the remote host"; S8 tests only
`job_language`, `cycle` and its UI. S3 tests through the controller's
`command_key_*`/`transform_requested`; S6 tests dispatch with a monkeypatched
`load_transforms`. S4 tests learning against a fake `reread`. Merge order:
S2, S5, S1, S4, S6, S3, S7, S8, S9.

## Rules for every stream

- Branch from the foundation commit; work only in your worktree.
- New tests go in new files named `tests/test_personalize_<stream>_*.py`; edit
  existing tests only where your change legitimately alters behaviour they
  pin, and say so in your report.
- Run your focused tests and `ruff check --select F .`; run the full suite
  once at the end and compare against the baseline (11 failures, all in
  `test_app_update_macos.py`, from `os.statvfs` on Windows).
- No bare `QTimer.singleShot` lambdas (they abort pytest silently); use owned
  QTimers. Never touch the real clipboard outside offscreen tests. Never log
  captured text, titles, selections or dictionary terms.
- Never call `setObjectName` on themed widgets (settingsTile, primaryButton,
  mcpChoice…); use a `tileId` property.
- Verify classic and Omarchy (`OPENWHISPER_UI`) at narrow widths and 130%
  font for UI you add.
- Commit in small, well-described commits. Do not edit CHANGELOG, README or
  this file; put user-visible changes in your report.

## Changes after review

A post-merge review (eight lenses, every finding verified twice) found 34
issues; these contract changes came out of their fixes:

- `TextContext.blocked` marks a deliberate refusal (an app on "Never read
  from", a password field) as distinct from "unknown". Command Mode and
  transforms refuse in such apps instead of falling back to a synthetic copy.
  The exclusion list stays editable whenever app awareness is on.
- `ContextCaptureService.identity_now(timeout_s)` gives the focused app for
  the paste check (Linux waits up to 0.5 s off the Qt thread).
- `DictationJob.target` records the app at the start even with app awareness
  off; only the paste check reads it. `dictation_pipeline.paste_target(job)`
  returns a `PasteTarget` (same, changed, unknown), and `paste_target_ok` is
  False when the app is unknown, so a rewrite is copied rather than pasted
  blind.
- `dictation_pipeline.html_for_paste(html, text, job)` and
  `focus_context.join_html_with_context` give rich text the same caret join as
  plain text.
- `services.runtime.command.CommandRefused` (a RuntimeError) marks Command
  Mode refusals such as "Select the text to change first"; they keep no audio
  and show a plain message.
- Page modules may define `save_drafts(dialog, *, ask=False) -> bool`; Settings
  calls it when a page is left, the window closes or is rejected, and on quit.
- The join lowercases a continuing first word only when it is a common word
  that is never a name; anything else keeps its capital.
- The frozen build collects every `ui_qt.dialogs` module, and
  `services/package_checks.py` lists the modules the app loads by name for the
  packaging self-test.

A second review round (performance, upgrade path, accessibility, Linux and
macOS paths, threading, and the first round's fixes) found 17 more; from
their fixes:

- `focus_context.same_target(a, b)` compares app, pid, window and site
  (`title_hint`); the paste check and the learning re-read both use it.
- The focus_context catalogue is the one list of terminals, including for
  Hyprland's Ctrl+Shift paste modifier.
- Windows hotkey holds follow the physical key (scan code), not its
  Shift-dependent name; only an immediate repeat of the last key counts as
  auto-repeat, and side-button holds survive a rehook.
- `capture_selection`'s callback may receive `None`: nothing was copied or
  written because this desktop cannot see other apps' copies. On Linux the
  app asks Qt for Wayland data-control (`QT_WAYLAND_USE_DATA_CONTROL=1`)
  before it starts.
- In an app OpenWhisper can't identify, Medium and High add no line breaks at
  all (paragraphs as well as lists).
- `ui_qt.widgets.history_playback.stop_all()` stops playback and any pending
  host download; downloads report through a session-long relay.
