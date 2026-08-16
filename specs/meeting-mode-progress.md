# Meeting Mode — Progress Tracker

**Updated:** 2026-08-15 · **Branch:** `meet_insight` · **Sources of truth:** `meeting-mode-plan.md` (decisions) + `meeting-mode-handoff.md` (contracts).

Update this file whenever a milestone completes or handoff is needed. Keep §Status current.

---

## Status (live)

| # | Milestone | Status | Notes |
|---|---|---|---|
| 0 | Foundation (interfaces/state/store/clock/DB/repo) | ✅ done | 38 unit tests green at prior handoff |
| 1 | Capture + spool + ASR | ✅ hardened | atomic segment/chunk commit, stable IDs, retry/recovery, per-channel device watchdog |
| 2 | Web + Qt transcript-only dashboard | ✅ hardened | paged transcript/history, scoped evidence hydration, authenticated playback, capture status |
| 3 | Agent + intelligence | ✅ hardened | evidence required on every agent op; bounded retry can recover from offline; Direct is the published default |
| 4 | Diarization | ✅ unit-tested | durable embeddings + pin preservation; real-model parity gate remains external |
| 5 | Multi-user hardening | ✅ unit-tested | authz + guest set_title fix; soak still manual |
| 6 | History/exports/recovery/packaging | ✅ hardened | full paging/search/playback; atomic rename/delete; unfinished ASR cannot finalize |
| 7 | Final review + full test pass | ✅ automated green | 556 passed / 1 skipped / 36 subtests; React build + sidecar type-check/build green; manual Windows gates remain |

### Verified this session (2026-08-09 hardening)

- ASR callbacks are durable and idempotent; incomplete terminal sessions are recoverable.
- State changes are persistence-first and meeting-scoped; every agent claim requires evidence.
- History is fully paged; evidence hydration, search selection, audit/undo, token rotation, and authenticated host/guest playback are wired.
- Failed/default-changed capture channels restart independently and report degradation.
- Placeholder components are unreachable until real artifacts are explicitly published.
- Full validation: 556 passed / 1 skipped / 36 subtests; React production build and sidecar type-check/build are green.

### AI note taker (2026-08-15)

Feature: a dedicated note-taker section on the dashboard — an agent pass that keeps
running, timestamped, evidence-linked meeting minutes like a professional note taker.

- **State**: new `live_notes` card key (`meeting/state/schema.py`). Note blocks are
  ordinary card items: `data.heading` (short label), `data.start_s` (meeting-clock
  stamp of the covered passage), body prose, evidence anchors. Full edit / confirm /
  pin / remove / host-undo / human-protection semantics come from the existing card
  machinery; old snapshots without the key load with an empty page (no migration —
  cards live inside `state_json`).
- **Agent (both cores)**: `build_note_taker_system_prompt()` + `build_notes_user_prompt()`
  in `meeting/agent/prompts.py`; the copilot system prompt delegates `live_notes` to the
  note-taker pass during the meeting; the final consolidation pass reconciles the page
  against the complete final transcript (fix blocks that later discussion superseded or
  contradicted, merge fragments, rebuild an emptied page). `CheckpointPayload.is_notes`
  selects the persona. **Direct core**: own system/user prompt, structural op filter,
  question tools blocked, JSON mode filtered. **Pi sidecar**: `supports_notes_pass = True`;
  notes checkpoints carry the note-taker `system_prompt` to the bundle; the Python tool
  bridge filters `tool.patch_state` to live_notes ops (ids resolved against the pass
  snapshot) and answers question tools with `notes_only` — so even a bundle that predates
  `is_notes` cannot escape the gate. The bundle adds `notesOnly` tool policy + a notes
  prompt branch (`sidecar/src`; `dist/bundle.cjs` rebuilt).
- **Shared gate**: `filter_notes_ops` / `live_note_ids` live in
  `meeting/state/patches.py` (the op-vocabulary boundary) and are used by both cores.
- **Scheduler**: `_maybe_fire_notes()` in `meeting/agent/scheduler.py`, fired from the
  checkpoint success path (before polish) every `_NOTES_EVERY_N_CHECKPOINTS = 2`
  successful checkpoints or ≥ `_NOTES_MIN_INTERVAL_S = 45s`, capped at the most recent
  `_NOTES_MAX_SEGMENTS = 300`. Capability-gated on `supports_notes_pass`. Failures never
  count toward checkpoint health and never mark segments consumed (next pass re-covers);
  cadence bookkeeping advances on both outcomes so a failing core cannot hot-loop.
- **End-of-meeting loop**: the offline re-decode strip keeps `live_notes` (only cards
  with dying evidence anchors are stripped); once consolidation is committed, proposed
  note blocks are stripped and the consolidation pass rebuilds/reconciles the page from
  the complete final transcript. End paths without a final report keep the live notes.
- **Web**: `live_notes` added to the card vocabulary; dedicated `NotesPane` under the
  spotlight row (chronological blocks, mm:ss badges, serif headings, evidence chips,
  provisional styling, auto-follow scrolling, human note composer). Excluded from the
  generic Captured list / composer and from the spotlight ranking. Fallback archive
  viewer renders a "Meeting Notes" section. Markdown export gains a "Meeting Notes"
  section (`**[mm:ss] heading** — body`).
- **Live-incident fix (2026-08-15 trial)**: the first dogfood run produced zero notes
  because the machine runs the Pi sidecar core and the notes pass was initially
  capability-gated to the direct core only. Both cores now support it; the bundle in
  `sidecar/dist` is rebuilt (this machine resolves the payload to the repo dist, no
  installed meeting-agent component).
- **Verification**: new `tests/test_meeting_notes_agent.py` (25 tests: prompts, shared
  filter, patch flow incl. duplicate/human-protection, scheduler cadence incl.
  retry-after-failure, export, engine strip ordering) and three new sidecar tests
  (notes flag + persona prompt over real stdio; hostile notes-tools storm filtered by
  the bridge). Full suite: 722 passed / 1 skipped / 38 subtests; sidecar type-check +
  bundle build and webui type-check + production build green.

---

## Current workstream

1. ~~Implement review remediation~~
2. ~~Add failure-path regression coverage~~
3. ~~Rebuild web and sidecar artifacts; run type checks~~
4. ~~Run the complete Python suite~~
5. Manual Windows verification (user-driven)

### Known gaps (non-blocking for code complete)
- MEETING_AGENT / SPEAKER_ID component archives are not published; both are hidden and unusable until real URLs, sizes, and SHA-256 digests replace the placeholders.
- Real speaker-model embedding parity remains unverified until the external ONNX model/reference fixtures are supplied.
- Manual: probe_loopback, live YouTube+mic meeting, crash recovery, LAN guest join.

---

## Handoff notes for next session

If this session dies mid-flight:
1. Read `meeting-mode-plan.md`, then `meeting-mode-handoff.md` and this file.
2. Run: `.\venv\Scripts\python.exe -m pytest tests/ -q -p no:cacheprovider`.
3. Standing rules: no Qt in `meeting/`; `meeting/state/patches.py` remains the validation boundary; never mark a chunk done before its segments are durable.
4. Sidecar: if esbuild platform mismatch, rebuild with the Windows Node runtime documented in `meeting-mode-handoff.md`.
