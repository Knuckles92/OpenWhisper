# Meeting Mode — Progress Tracker

**Updated:** 2026-08-08 · **Branch:** `meet_insight` · **Sources of truth:** `meeting-mode-plan.md` (decisions) + `meeting-mode-handoff.md` (contracts).

Update this file whenever a milestone completes or handoff is needed. Keep §Status current.

---

## Status (live)

| # | Milestone | Status | Notes |
|---|---|---|---|
| 0 | Foundation (interfaces/state/store/clock/DB/repo) | ✅ done | 38 unit tests green at prior handoff |
| 1 | Capture + spool + ASR | ✅ on disk | py_compile OK; needs unit tests still |
| 2 | Web + Qt transcript-only dashboard | ✅ wave-1 complete | webui built to `dist/`; Qt panel/tray/handlers wired |
| 3 | Agent + intelligence | ✅ wave-1 complete | `pi_sidecar.py` + `__init__.py` added; sidecar build next |
| 4 | Diarization | ✅ on disk | py_compile OK; needs pin-preservation tests |
| 5 | Multi-user hardening | ✅ unit-tested | authz + guest set_title fix; soak still manual |
| 6 | History/exports/recovery/packaging | ✅ code-complete | spec+CHANGELOG done; component archives still placeholders |
| 7 | Final review + full test pass | 🟡 nearly done | 85 meeting tests green; adversarial HIGH/MED fixed; manual verify left |

### Verified this session (2026-08-08 resume)
- Wave-1 gaps closed via Grok/Composer subagents:
  - [Pi sidecar](b0157ab8-afb8-4c57-a1c1-e69ba9ecdcc0): `meeting/agent/pi_sidecar.py` + `__init__.py`
  - [Webui](2e8128fa-0970-4807-b67f-e7f41b96272d): full React shell; `npm run build` OK → `webui/dist/`
  - [Qt wiring](cc929e48-6232-41b8-9c81-ee024652f466): main_window panel, tray, theme.qss, ui_controller handlers
- Next: integration import pass, sidecar `bundle.cjs` build, tests wave, packaging.

---

## Current workstream

1. ~~Finish `pi_sidecar.py` + `agent/__init__.py`~~
2. ~~Finish webui React shell + `webui/dist` build~~
3. ~~Integration import pass~~ (fixed recovery finalize dict mismatch)
4. ~~Sidecar `bundle.cjs`~~ (fixed pi-adapter: use `ModelRuntime.getModel`, not removed `getModel`)
5. ~~Tests wave~~ — 84 meeting tests green (`tests/test_meeting_*.py`)
6. ~~Packaging leftovers~~ — `OpenWhisper.spec` + CHANGELOG; component URLs still placeholder TODOs
7. ~~Adversarial review~~ (pause/end races, sidecar stale tools, guest set_title)
8. Manual Windows verification (user-driven)

### Known gaps (non-blocking for code complete)
- Webui: topic/rolling summary not rendered; host undo UI not exposed; no action-result toasts.
- MEETING_AGENT / SPEAKER_ID component archives not published (placeholder SHA/URLs).
- Manual: probe_loopback, live YouTube+mic meeting, crash recovery, LAN guest join.

---

## Handoff notes for next session

If this session dies mid-flight:
1. Read `meeting-mode-plan.md` then `meeting-mode-handoff.md` §3 contracts + this progress file.
2. Run: `.\venv\Scripts\python.exe -m pytest tests/test_meeting_*.py -q -p no:cacheprovider`
3. Prefer **Grok 4.5 / Composer** subagents for workhorses (not Opus unless truly needed); orchestrate from main session.
4. Standing rules: greenfield, no Qt in `meeting/`, scipy banned, exclusive file ownership when parallel.
5. Sidecar: if esbuild platform mismatch, delete `sidecar/node_modules` and `npm install` on Windows before `node build.mjs`.
