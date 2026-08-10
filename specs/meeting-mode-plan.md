# Meeting Mode — Implementation Plan

## Context

OpenWhisper gains **Meeting Mode**: a long-running listening session that captures meeting audio (mic + system audio), transcribes it locally in near-real-time, and maintains a live, multi-user browser dashboard of meeting intelligence — evolving topic, participants, key points, decisions, action items, topic timeline, risks/disagreements, shared notes, full transcript, and a quiet inbox of context-aware questions. Two equal jobs: live thinking copilot + faithful durable record. Windows is the v1 platform. Branch: `meet_insight`.

**Hard constraint:** greenfield — do not reference or revive the previously deleted meeting-mode code from git history. (Known landmine: `services/database.py` `_drop_removed_meeting_tables()` drops tables named `meetings`/`meeting_chunks`/`meeting_insights` on every startup — all new tables use fresh names; that method stays untouched.)

## Locked product decisions (from grill session)

- **Lifecycle**: exclusive mode — start/stop from tray + main window (+ optional unbound-by-default global hotkey); dictation hotkeys suspended while active; dashboard has start/pause/end controls (host-only).
- **Audio**: mic + WASAPI loopback as two separate streams tagged `mic`="Me", `loopback`="Others"; durable chunked WAV spool registered in SQLite (crash-safe); audio kept permanently by default.
- **ASR**: local faster-whisper only; dedicated model instance with meeting-specific model setting (default "auto" → turbo/GPU, base/CPU); segment timestamps first-class.
- **Diarization**: real-time, progressive, best-effort clustering of the Others channel that refines as the meeting progresses; manual correction first-class; human corrections pinned/authoritative; graceful degradation to Me/Others.
- **Intelligence**: Direct OpenRouter/OpenAI is the shipped default agent core. The Pi SDK sidecar remains behind a publication gate until its managed Node payload has real immutable release artifacts; both share the same adapter boundary and v1 state-only authority (validated structured state patches + questions; no shell/fs/external actions).
- **Update loop**: adaptive checkpoints ~30–60s or topic shift, only when new transcript exists, coalescing when behind; state+delta payload; final consolidation pass at meeting end that must not break evidence links.
- **Questions**: quiet inbox, never steals focus; answer/dismiss/ignore; auto-resolve with "answered from audio" badge at high confidence, greyed suggestion at medium; one-click add-to-notes.
- **Consent**: one-time informed dialog (transcript/state → OpenRouter; audio never leaves) + per-meeting visible cloud toggle remembering last choice; no key/network → transcript-only mode with banner; insights re-runnable from history.
- **Web**: FastAPI + uvicorn + WebSocket; React/TypeScript built frontend (self-contained, no CDN). **Full multi-user v1**: tokenized guest link + separate host token, guests join with display name, can answer questions / fix speakers / edit cards; all edits attributed; last-write-wins + audit trail; host-only end/delete/export/undo. Default bind localhost; explicit host action shares on LAN.
- **Every card editable** by any participant; generated content visibly provisional (proposed vs human-edited/confirmed styling); evidence links (clickable segment references) on every generated claim; human-touched items protected from agent overwrite.
- **History**: full v1 — past-meetings list, FTS search, rename, delete, re-run insights. **Exports**: Markdown, JSON (full state + evidence), plain timestamped transcript.
- **LLM settings**: meeting-specific provider/model reusing `services/transcript_cleanup.py` plumbing (`find_api_key`, `list_cleanup_models`, provider base URLs), defaulting from cleanup config.

## Architecture

```
Mic + WASAPI loopback  →  capture adapters (interface: CaptureSource)
  →  SpoolWriter: chunked 16kHz WAVs + SQLite registration (crash-safe)
  →  MeetingAsrEngine: dedicated faster-whisper, per-chunk retry, timestamped segments
  →  Diarizer: ONNX speaker embeddings + online clustering (progressive relabel)
  →  MeetingStateStore: single-writer, patch ops, seq numbers, audit, write-through persistence
  →  AgentCore (Pi sidecar / direct OpenRouter) ⇄ CheckpointScheduler
  →  FastAPI + WebSocket hub  →  React dashboard (multi-user)
```

### New code layout

- `meeting/` — standalone-extractable package, **no Qt imports**:
  - `interfaces.py` — all boundary Protocols: `CaptureSource`, `ChunkSpool`, `AsrEngine`, `Diarizer`, `AgentCore`, `AgentToolHost`, `StateStore`, `TransportServer`, `MeetingRepository`.
  - `engine.py` (`MeetingEngine` orchestrator, owns its own executor/threads — never the controller's 2-worker pool), `clock.py` (monotonic meeting epoch, gap-fill, drift re-anchor, pause credit).
  - `capture/` — `devices.py` (find `"[Loopback]"` WASAPI input devices matching default render device), `sd_stream.py` (primary, sounddevice ≥ 0.5.0), `soundcard_stream.py` (fallback), `spool.py` (VAD-aligned chunk cuts: ≥400ms quiet after 5s, hard cut 20s; atomic WAV writes; gap-fill zeros when loopback goes silent >120ms).
  - `asr/` — dedicated `LocalWhisperBackend(model_name=...)` instance; `backend.model.transcribe(float32_array, vad_filter=True)` keeping `segment.start/.end`; reuse `fft_resample` from `services/streaming_transcriber.py`.
  - `diarize/` — numpy fbank + ONNX speaker embedding (WeSpeaker-family model, ~28MB, on the **already-shipped onnxruntime**; no torch, no new Python deps) delivered as a new downloadable component `SPEAKER_ID` via `services/components.py`; incremental cosine assignment (≥0.62 → cluster, else new "Speaker N") + periodic full AHC re-cluster where pinned segments dominate cluster→participant mapping and never relabel; any failure → pass-through Me/Others + banner chip.
  - `state/` — `schema.py` (MeetingState, CardItem, Question, Participant dataclasses), `patches.py` (op vocabulary + jsonschema validation + inverse ops), `store.py` (single-writer apply path shared by agent AND human actions → one audit trail, one broadcast).
  - `agent/` — `base.py` (AgentCore/AgentToolHost Protocols), `scheduler.py` (45s base, 30s floor/60s ceiling, Jaccard topic-shift early trigger, coalescing), `pi_sidecar.py`, `openrouter_direct.py` (capability-probed tool-calling → JSON-mode fallback with repair retry), `question_engine.py` (confidence ≥0.8 auto-resolve, 0.4–0.8 greyed suggestion), `prompts.py`.
  - `web/` — `server.py` (uvicorn in dedicated thread, `install_signal_handlers=False`, stop via `should_exit`), `auth.py` (host/guest `token_urlsafe(32)`, `hmac.compare_digest`), `ws.py` (snapshot-on-connect `hello`, `patch`/`segment`/`presence` broadcasts, `action` dispatch with `client_action_id` echo for optimistic UI), `api.py` (SPA/static, transcript paging, exports, audio playback).
  - `export/` — markdown, JSON, transcript txt. `recovery.py` — startup scan for interrupted sessions.
- `services/runtime/meeting.py` — `MeetingRuntime`: thin Qt adapter (engine callbacks → pyqtSignals), instantiated beside the other runtimes in `ApplicationController.__init__` (~line 132).
- `sidecar/` — TypeScript Pi glue (`@earendil-works/pi-coding-agent`), esbuild → single `bundle.cjs`.
- `webui/` — React + TS + Vite; **built `dist/` committed** (CI check rebuilds and fails if stale); added to `OpenWhisper.spec` `datas`, served via `config.bundle_root()`.
- `ui_qt/dialogs/meeting_consent_dialog.py` (cloned from `HuggingFaceConsentDialog` pattern), `ui_qt/dialogs/meeting_recovery_dialog.py`, `ui_qt/widgets/meeting_panel.py` (start/pause/end, elapsed, cloud toggle, open dashboard, copy guest link).

### Data model (SCHEMA_VERSION 9 → 10; ORM in `services/models.py` + raw-SQL migration block; enable WAL)

Fresh table names: `meeting_sessions` (status, tokens, cloud flag, spool dir, latest `state_json` snapshot + seq, pid/heartbeat for crash detection) · `meeting_audio_chunks` (channel, seq, file, start_s/duration_s, `asr_status` pending/done/failed + attempts/error) · `meeting_segments` (stable `sg_…` IDs as evidence anchors; channel, start/end, text, `speaker_participant_id`, `speaker_source` channel/diarizer/human, `speaker_pinned`, embedding BLOB) · `meeting_segments_fts` (FTS5 + sync triggers) · `meeting_participants` (id, display_name, kind me/others_cluster/guest, `name_source` default/human/agent_inferred) · `meeting_state_items` (card, text, data_json, status proposed/edited/confirmed/removed, author, pinned, revision, evidence_json) · `meeting_questions` (status, answer + source + confidence, suggested_answer, thread, evidence) · `meeting_events` (append-only audit: seq, actor, action, payload, precomputed inverse op → host undo).

**Patch protocol** (agent tools `patch_state`/`ask_question`/`resolve_question`; humans use the same op vocabulary plus `pin_item`, `reassign_segment_speaker`, `rename_participant`, `answer_question`, `dismiss_question`): ops like `add_item`/`update_item{base_revision}`/`remove_item`/`set_topic`/`append_timeline`/`set_rolling_summary`/`upsert_participant`/`suggest_participant_name`. Validator rejects per-op (not per-batch): unknown ops, revision mismatch, ops against human-touched items (`pinned` / `edited` / `confirmed` / `name_source='human'`), nonexistent evidence IDs. No wholesale-replace op exists. Rejections returned to the agent so the next checkpoint self-corrects.

### Pi sidecar

Delivered as downloadable component `MEETING_AGENT` (portable win-x64 Node LTS + `bundle.cjs`, ~25–30MB compressed) via the existing component catalog/download/progress UI — installer stays flat; consent dialog offers the install inline. Lifecycle: spawn `CREATE_NO_WINDOW` with `OPENWHISPER_SIDECAR_TOKEN` + `OPENROUTER_API_KEY` env; NDJSON JSON-RPC over stdio; `hello` handshake (token, 10s timeout) → `initialize`/`checkpoint`/`consolidate`/`cancel`/`ping`/`shutdown`; health ping 10s, 3 misses → restart with backoff (1s/5s/30s, max 3 per 10min) then "intelligence offline" + transcript-only. Sidecar registers exactly three Pi tools (patch_state, ask_question, resolve_question) bridging back over RPC — state-only authority is structural, and Python validates every op anyway. `DirectOpenRouterAgent` implements the same `AgentCore` Protocol (settings-selectable; also the no-sidecar fallback).

### Integration points (existing patterns to follow)

- Signals: new `meeting_*` pyqtSignals in `ApplicationController` (~L54–93), connected in `_connect_signals` (~L864); UI callbacks assigned in `_setup_ui_callbacks` (~L168); cleanup registered in `cleanup()`.
- Exclusive mode: `controller.meeting_active` checked by `TranscriptionRuntime` start paths + tray toggle + `reload_whisper_model`.
- Settings: `SettingsKey` + `resolve_*()` validators + `config.py` defaults — `MEETING_WHISPER_MODEL`, `MEETING_LLM_PROVIDER/MODEL` (default from cleanup config), `MEETING_AGENT_CORE` ("direct" until the Pi payload is published), `MEETING_CLOUD_CONSENT_GIVEN`, `MEETING_CLOUD_LAST_ENABLED`, `MEETING_SERVER_BIND` ("localhost"|"lan"), `MEETING_SERVER_PORT` (0 = ephemeral); `'meeting_toggle': ''` in `DEFAULT_HOTKEYS`.
- Reuse: `find_api_key` / `list_cleanup_models` / provider base-URL helpers (`services/transcript_cleanup.py`); component infra (`services/components.py`); `fft_resample`; menu/tray/callback wiring patterns; QSS objectName styling.
- `requirements.txt`: bump `sounddevice>=0.5.0`; add `fastapi`, `uvicorn`, `websockets`, `soundcard` (fallback), `jsonschema`.

## Phased milestones (each independently verifiable)

1. **Capture + spool + ASR core** — loopback probe script first (gate: `[Loopback]` devices appear with bundled PortAudio DLL; if not, promote `soundcard` to primary). Device layer, spool writer, clock/gap-fill, dedicated ASR with retry, DB migration v10. *Verify:* unit tests (clock drift, gap-fill, chunk cuts, v9→v10 migration fixture); manual: YouTube playing + speaking → interleaved timestamped Me/Others transcript in SQLite; kill -9 mid-meeting → finalized chunks all `pending`.
2. **State store + web + transcript-only dashboard** — patches/store/repository, FastAPI+WS hub, React shell (live transcript, notes, editable cards, human ops), tokens/roles, reconnect resync, `MeetingRuntime` + panel + menus + exclusive mode. *Verify:* pin-protection/revision/undo unit tests; two-browser LAN session; WS reconnect.
3. **Agent + intelligence** — `DirectOpenRouterAgent` first (proves the loop, zero packaging risk), then Pi sidecar (TS bundle, component, lifecycle, RPC), scheduler, question inbox, consent dialog + cloud toggle, offline banner. *Verify:* fake-sidecar stdio stub tests (handshake/auth/restart/cancel); golden op-validation tests; live meeting with OpenRouter key; kill node.exe mid-meeting → restart → banner.
4. **Diarization** — fbank/embedder/clustering, `SPEAKER_ID` component, relabel flow, manual correction + pin UI, degradation. *Verify:* embedding parity vs reference vectors (cosine ≥0.999); pin-preservation clustering tests; podcast-through-loopback manual test.
5. **Multi-user hardening** — LAN bind toggle + firewall guidance, join flow, presence, host undo, token regeneration, per-action authorization matrix. *Verify:* authz tests (guest attempting host ops); 5-client soak; reconnect storms.
6. **History + exports + recovery polish** — past-meetings list, FTS search, rename/delete (cascade + spool cleanup), MD/JSON/TXT exports, re-run insights, recovery dialog, `OpenWhisper.spec` updates. *Verify:* export golden files; FTS tests; frozen PyInstaller build smoke on clean Windows VM.

## Risks (ranked, with mitigations)

1. **Pi sidecar packaging** — component delivery isolates installer; DirectOpenRouter fallback ships first; failures bound to "intelligence offline", never meeting loss.
2. **WASAPI loopback reliability** — phase-1 probe gate; `soundcard` fallback behind `CaptureSource`; gap-fill + device-change watchdog; degrade to mic-only with banner.
3. **Multi-user on plain LAN HTTP** (locked decision, flagged) — cleartext transcript/tokens on LAN. Mitigations: localhost default until host explicitly shares; 128-bit tokens, constant-time compare; host token never in guest links; one-click link regeneration; TLS as fast-follow.
4. **VRAM contention** (two model instances) — existing CPU-fallback machinery + settings hint on small cards.
5. **Diarization quality** — parity test guard; worst case is designed channel-level degradation.

## Verification (end-to-end)

Activate venv, `python -m pytest tests/` for the new unit suites per phase; `python app_qt.py` → start meeting from tray → dashboard opens in browser → speak + play audio → live interleaved transcript, checkpoint updates on cards with evidence chips, question inbox behavior, guest join from second device, end meeting → consolidation → exports → meeting appears in history with search; kill-process tests for crash recovery; full PyInstaller build for packaging phases.
