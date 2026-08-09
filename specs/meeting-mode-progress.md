# Meeting Mode — Progress Tracker

**Updated:** 2026-08-09 · **Branch:** `meet_insight` · **Sources of truth:** `meeting-mode-plan.md` (decisions) + `meeting-mode-handoff.md` (contracts).

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
