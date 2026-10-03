# OpenWhisper performance implementation

Implementation of the 15 recommendations in the [October 2 audit](../docs/codebase-performance-review-2026-10-02.md), October 3, 2026. Work was divided across desktop, dashboard, meeting processing/state, and test consolidation, with integration checks in the shared checkout. Pre-existing working-tree changes were preserved.

## Implemented work

| Audit item | Result |
| --- | --- |
| 1. Lazy Settings destinations | Register lightweight destination shells and build controls on selection or explicit access. Overview, rail summaries and search work without constructing hidden pages. New-page loading is scoped so opening another destination preserves unfinished input. |
| 2. Settings snapshots | Cache defensive snapshots using file identity/version checks; invalidate on writes and detect external replacement. Write transactions still reread the canonical file. History rendering shares one provider-settings snapshot. |
| 3. Background discovery and storage scans | Recording-device discovery and usage summaries run on workers when needed. Usage scans count bytes directly, without building and sorting recording records. Delivery remains safe when the dialog closes. |
| 4. Deferred desktop startup | Upload, Meeting Mode and Host Dashboard materialize on demand. Runtime updates are retained and replayed, including partial meeting state and multiple download states. Package exports are lazy. Database initialization runs on a dedicated worker; recovery/sync wait for successful completion and shutdown prevents late startup. |
| 5. Incremental history | Reuse unchanged history and past-meeting cards by record identity and displayed values, including fresh ORM instances. Cancellable delivery relays prevent emitting through a deleted widget. |
| 6. Bounded dashboard history | Use aggregate SQL summaries and bounded keyset pagination. Preview selection excludes transcript loading; opening the full view loads a page on demand. Closing/changing selection aborts requests. Progress polling reads state without transcript/content materialization. |
| 7. Transcript rendering and printing | Window long transcripts using measured row heights, preserving keyboard/citation/playback navigation. Mount complete print content only for printing and remove it afterward. Full export explicitly loads remaining pages and commits the final page before printing. |
| 8. Capture before processing readiness | Start durable capture and the dashboard before local speech/speaker/agent initialization. Pending chunks replay in order, exactly once, before live delivery. End/shutdown cancel initialization; unfinished durable audio remains recoverable and the dictation-model lease is released once. |
| 9. Meeting-state copies | Copy only operation mutation domains and required mutable containers. Serialize once for persistence and reconcile canonical title metadata without rebuilding the document. Preserve rollback, input ownership, audit/undo and ordered notifications. Unknown handlers fall back to a full isolated copy. |
| 10. Waveform timing | Advance particle simulation on elapsed timer time; painting reads the current simulation. Equal elapsed time at 30/60 updates produces equivalent physics. |
| 11. Settings test setup | Replace 12 checkbox items and nine alias items with two isolated scenarios and named subtests. Reset persisted values/control state between checkbox checks and routing between aliases. Retain every original row and strengthen unrelated-key assertions. |
| 12. Title-contract matrix | Keep all six valid boundaries across all nine write paths. Exercise nine invalid classes through each distinct validator, with no-dispatch, unchanged durable/live state, no-notification and no-audit assertions. Remove 45 duplicated invalid dispatch variants. |
| 13. Numeric test matrix | Collect the 43 valid size/window pairs directly; eliminate 29 rows that previously skipped before application code. Preserve seeds, NumPy comparisons, tolerances and the block-boundary test. |
| 14. Geometry tests | Combine expansion, row dimensions/insets and clear/shrink checks into one window lifecycle, preserving both payload variants and their initial dashboard conditions. Retain both tab-switch scenarios. |
| 15. Pure-test overhead | Conservatively follow local import dependencies and keep Qt isolation for every possible UI test; pure cases skip widget enumeration and installed-agent probe setup. Keep settings, credentials, database and recording isolation for all tests. Replace MeetingClock sleeps with a module-owned monotonic clock; real worker/race/cancellation deadlines remain real. |

“Browser” in item 6 means the web UI opened for a meeting dashboard. Its meeting-history API and transcript requests are the affected paths.

## Measured changes

These are isolated synthetic probes on Windows Python 3.12 and offscreen Qt, plus React static rendering. They measure construction, database operations and rendered output, not end-to-end launch or real-display frame rates. Cold/warm results are kept separate; gains overlap and should not be added.

| Measurement | Before | After |
| --- | --- | --- |
| First Settings construction | 806 ms; 1,829 child widgets | 221 ms; 317 child widgets |
| Warm Settings construction | 209–211 ms | 52–54 ms |
| Settings JSON reads during construction | 88–89 | One first time; zero warm |
| First MainWindow construction | 524 ms; 717 child widgets | 117 ms; 292 child widgets |
| Warm MainWindow construction | 71–75 ms | 42–44 ms |
| Unchanged 100-card history refresh | Reconstructed cards | 1.3–1.9 ms; all 100 cards reused |
| Visible transcript plus report at 5,000 segments | 10,000 transcript articles; about 3.17 MB markup | 30 articles; about 13.3 KB markup |
| Runtime-state SQL write, median | 8.306 ms | 6.386 ms |
| Summary patch SQL write, median | 6.643 ms | 5.113 ms |
| Card edit SQL write, median | 7.665 ms | 5.638 ms |

The state benchmark uses a 739,068-byte document with 210 card items and six reports, five alternating paired trials, three warmups and 15 timed writes per trial. Both versions use the same final SQLite repository. Live/saved snapshots, audit order and undo are compared as part of the probe.

The transcript probe renders the same synthetic read-only meeting through the original and current source. Its seven warm repetitions produce median static-render times of 250 ms and 2.2 ms at 5,000 segments; these are Node rendering times, not browser paint times. Reproduce with `node webui/benchmarks/history-render.cjs --baseline=6089571738ebcfb952d21738c76d739056b5ce27 --repeats=7`.

The dashboard history regression fixes the number of SELECT queries at five for a page with 300 transcript rows and prohibits loading complete transcript/audio collections for summaries. Model-startup regressions use event barriers to prove capture/dashboard readiness while processors remain blocked, and verify replay and cancellation ownership.

## Test reduction and coverage

The six-file reduction subset changes from 369 collected items (340 passed, 29 skipped) to 274 collected items (274 passed, plus named subtests): **95 fewer collected items**. This removes repeated setup and previously skipped rows while retaining meaningful inputs and assertions. New performance/cancellation/printing regressions are added separately.

Coverage is compared by executed production-line and branch sets, not only percentages. Original and consolidated tests execute **exactly the same 17,553 production lines and 1,728 branches**, with no lost or added coverage. This comparison covers the six-file consolidation subset, not a full-suite coverage claim. Original test copies run against the same final production implementation as consolidated tests. The copies receive the page-activation adaptations required by the lazy UI and the diagnostic-path isolation fix; their original matrices and clock waits remain intact. Coverage instrumentation is installed in ignored scratch tooling, without changing project dependencies.

Integration also exposed a corpus benchmark that left `HF_HUB_OFFLINE` enabled for later tests. Its environment changes now restore on success and errors, with regressions for absent and existing values. Diagnostic output is isolated in disposable test directories so test-installed handlers cannot modify the checkout's metrics.

Final validation passed:

- Full Python/Qt suite: 4,586 passed, 12 skipped and 3,921 subtests passed in 287.72 seconds, with no failures.
- Dashboard: all 151 tests passed; TypeScript checking and the production build passed. The generated bundle was refreshed.
- Python correctness (`ruff --select F`), whitespace checks and dependency-declaration synchronization passed.
- Independent review reproduced and verified fixes for canceled diarizer adoption, revoked agent initialization/tool access and replay-preview ordering.
- Hash checks confirm the 15 unrelated pre-existing source/data changes stayed intact. Existing changes in shared edited files were retained.

Machine-readable evidence is recorded in [implementation results](../docs/benchmarks/performance-implementation-2026-10-03.json). Test durations were recorded during concurrent validation and do not establish an isolated before/after suite speedup.

## Validation limits

Actual browser visual/paint QA could not run because the browser automation runtime failed to initialize. Deterministic scroll, resize, keyboard, citation, playback, print hydration and cancellation tests passed. Qt probes use offscreen rendering. Real-device model loading, production archives and compositor smoothness still need representative manual profiling; the recorded gains do not claim those measurements.
