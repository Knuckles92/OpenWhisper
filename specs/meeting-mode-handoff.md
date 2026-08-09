# Meeting Mode — Status

**Branch:** `meet_insight` · **Companion:** `specs/meeting-mode-plan.md` (the approved plan and every locked product decision — read it first).

Meeting Mode is **implemented and hardened**. The original end-to-end hardware pass remains recorded below; the 2026-08-09 review added atomic ASR/state durability, complete history paging, authenticated recording playback, safe deletion, token rotation, capture failover, audit/undo UI, and publication gates. Validation is green: **556 passed / 1 skipped / 36 subtests**, the React production build passes, and the sidecar type-check/build passes.

---

## 1. Release blockers

1. **The two optional downloadable components are not publishable yet.** `meeting-agent` and `speaker-id` still contain placeholder artifacts, but they are now explicitly `published: false`, excluded from `available_component_ids()`, rejected by payload resolvers, and disabled in Settings. A release may ship the Direct agent plus Me/Others channel labels safely. Publishing either component still requires real immutable URLs, measured sizes, pinned SHA-256 digests, and its release gate.
2. **The diarization parity gate has never run.** `tests/test_meeting_diarize.py::test_embedding_parity_against_reference_vectors` is the plan's "cosine ≥ 0.999" gate and is the single skipped test. It runs once both `OPENWHISPER_SPEAKER_MODEL` (ONNX path) and `OPENWHISPER_SPEAKER_PARITY_REF` (an `.npz` of `audio_<i>` / `embedding_<i>` pairs) are set. Until it passes, embedding quality against the real model is unverified.

## 2. Environment facts (hard-won)

- Dev shell is **WSL**; the app targets **Windows**. The Windows venv is invocable from WSL as `./venv/Scripts/python.exe`, but **only with the command sandbox disabled** (WSL interop socket is blocked; `/sandbox` manages this). Plain `python3` lacks every third-party dep and is good only for `python3 -m py_compile`.
- Run tests: `./venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider` (the cache write is denied on this mount; `-p no:cacheprovider` silences it).
- **This machine exposes no WASAPI `[Loopback]` devices**, so the `soundcard` fallback is the operative system-audio path here and it works. Both paths must stay. `python scripts/probe_loopback.py` checks both and passes if either works.
- **Building the sidecar must happen on Windows** — the installed esbuild is `@esbuild/win32-x64` only. Use `"/mnt/c/Program Files/nodejs/node.exe" 'D:\coding\whisper_local\sidecar\build.mjs'` from WSL, or `npm install @esbuild/linux-x64 --no-save` first.
- Building `webui` from WSL needs `npm install @rollup/rollup-linux-x64-gnu --no-save` once (npm's optional-dependency bug).

## 3. What was verified, not just written

| Claim | How it was checked |
|---|---|
| Timeline accuracy | Simulated hour of 44.1 kHz audio: drift **+0.000014 s** (was +4.658 s), chunk contiguity exact |
| Audio quality into Whisper | THD+N **−109.7 dB** (was −29.2 dB) after moving to one resample per chunk |
| Pi sidecar boots | Ran `dist/bundle.cjs`: valid `hello` line, exit 0, zero `import_meta` stubs |
| Diarization scaling | Re-cluster at N=3000: **0.013 s** (was 33.4 s); worst `assign()` 13 ms (was 3.7 s); 4-speaker fixture still splits 625/625/625/625 |
| REST authorization | Every route enumerated from the router, probed with none/bad/guest/host tokens — **zero gaps** |
| FTS injection | 17 hostile queries — no injection, operators neutralized |
| Token leakage | Host token absent from `hello`, `/api/session`, `/api/meetings`, all three exports, and now from logs |
| End to end | Real meeting: dashboard 200 + built SPA, bad token 403, exports clean, shutdown clean |
| Regression detectors are real | The engine fixes were each reverted one at a time to confirm the test caught it |
| Durable ASR | Segment rows and chunk `done` status commit in one transaction; stable IDs make callbacks idempotent; terminal meetings with unfinished chunks are rediscovered |
| Durable state | Copy-on-write state is persisted before live replacement/broadcast; persistence-failure tests verify no seq/state leak |
| History/playback | Keyset pagination covers equal timestamps; audio mixes mic+loopback with silence gaps and rejects paths outside the meeting spool |
| Capture recovery | Default-device changes restart only the affected channel and persist/broadcast capture availability |

## 4. Architecture (as built)

```
mic + WASAPI loopback (or soundcard fallback)   ← two separate streams, never mixed
  → SpoolWriter: audio thread only enqueues; a writer thread does gap-fill,
    cut detection, one resample per chunk, atomic WAV write, DB registration
  → MeetingAsrEngine: dedicated faster-whisper, unbounded queue, retry ×3,
    meeting-clock timestamps preserved
  → OnlineDiarizer: bounded working set + anchors; human pins never relabeled
  → MeetingStateStore: single writer, seq-ordered, audited, write-through
  → AgentCore (Pi sidecar | direct OpenRouter) ⇄ CheckpointScheduler
  → FastAPI + WebSocket → React dashboard (host + guests)
```

Key invariants worth not breaking:
- **No Qt imports anywhere under `meeting/`** — the package stays standalone-extractable.
- **`meeting/state/patches.py` is the only place state changes are validated.** Agent, human, and system actors all pass through it.
- **Human corrections outrank automation**: pinned/edited/confirmed items and human-named participants reject agent writes; pinned segments reject diarizer relabels (`segment_pinned`).
- **`delete_meeting` deletes children explicitly** — SQLite does not fire the FTS sync triggers on FK cascade, which would orphan transcript text in the search index.
- **New meeting tables use fresh names.** `services/database.py` still drops legacy `meetings`/`meeting_chunks`/`meeting_insights` at startup; never reuse those names.

## 5. Authorization model (as enforced)

| Actor | May do |
|---|---|
| agent | add/update/remove card items (not `user_notes`), set topic/summary, suggest participant names, ask + resolve questions. Provisional by construction; cannot touch human-edited content |
| guest (`user`) | edit/pin/confirm card items, add notes, answer/dismiss questions, fix speaker labels |
| host | everything a guest can, plus title/topic/summary, pause/end/delete/export/search, undo anyone, cloud toggle |
| system | app-internal only: creates `me`/`guest` participants, applies diarizer relabels and undo inverses |

Deliberate: `resolve_question` is **agent-only** (it stamps "answered from audio" — a human reaching it would attribute their own words to the recording; humans use `answer_question`). `set_topic`/`set_rolling_summary`/`set_title` are **host-only**, matching the host-only REST rename route.

## 6. Known gaps (deferred, not forgotten)

- **Token regeneration is implemented.** The host dashboard rotates both capability tokens, receives the replacement host URL, and forces connected sockets to re-authenticate. LAN traffic is still plain HTTP, so rotation limits exposure but does not encrypt it.
- **LAN sharing is plain HTTP.** Locked decision, flagged: live transcript and tokens travel unencrypted. Mitigated by localhost default, 128-bit tokens, constant-time compare, and an explicit warning in the new Settings → Meeting tab. TLS is the fast-follow.
- **Over-merged speakers**: re-clustering can now split them, but only within the bounded working set — very early over-merges that scroll out of the window stay merged.
- **Sidecar RPC is not pipelined**: `RpcEndpoint.handleLine` dispatches concurrently, so `initialize` + `checkpoint` sent back-to-back can race. Harmless today (the Python side awaits `initialize`), but it would bite anything that pipelines.
- **`scripts/probe_loopback.py`** is a manual gate, not part of the suite.

## 7. Manual verification still worth doing on Windows

1. `python scripts/probe_loopback.py` — confirm at least one system-audio path works.
2. `python app_qt.py` → start a meeting from the tray while a video plays and you speak → dashboard should show interleaved **Me** / **Others** lines with correct timestamps.
3. With an OpenRouter key + consent: confirm cards populate at checkpoints, evidence chips jump to the right transcript line, and the question inbox never steals focus.
4. Kill the process mid-meeting → relaunch → the recovery dialog should offer Finalize/Discard and no audio should be lost.
5. Settings → Meeting → "Share on local network", then join from a phone with the guest link; confirm a guest can edit a card and fix a speaker but cannot end, delete, or export.
