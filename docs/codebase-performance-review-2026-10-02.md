# OpenWhisper performance and cleanup review

Reviewed October 2, 2026, against checkout `6089571` and the current working-tree changes.

The best first targets are Settings construction, repeated settings reads, and history rendering. These have directly observed costs. Startup also constructs inactive screens, while the browser history path does substantially more database and rendering work than the desktop history path. The first ten recommendations are ranked for noticeable loading and responsiveness improvements, then implementation effort and confidence. Slots 11–15 focus on reducing redundant test cases and setup work while preserving the behaviors, inputs and branches currently exercised.

This records the original audit before implementation. The recommendations were subsequently implemented; see the [October 3 implementation and validation report](../docs/performance-implementation-2026-10-03.md). Findings and measurements below describe the audited version.

## Review scope and evidence

The codebase inventory covered 689 tracked Python, TypeScript, TSX, CSS, QSS and QML files, totaling 225,476 lines. All 596 Python files were parsed in a static survey. Detailed inspection covered entry points, Qt widgets and dialogs, services, speech workers, audio capture and preview, meeting lifecycle and state, persistence, web UI, sidecars, dependencies, packaging and related tests. This was a codebase-wide survey with targeted hot-path inspection, not a claim that every line received equal scrutiny. Generated bundles, third-party dependencies, recordings and model weights were excluded from source review.

Measurements used isolated settings and credentials, synthetic records, Windows Python 3.12 and offscreen Qt. They describe local work, not real-display frame rates or an end-to-end launch benchmark. Source-proven inefficiencies are distinguished below from expected gains that still need measurement. [Evidence summary](../docs/benchmarks/codebase-performance-review-2026-10-02.json).

| Rank | Work to handle | Expected benefit | Effort |
| --- | --- | --- | --- |
| 1 | Build Settings destinations on demand | High for first Settings open | Medium |
| 2 | Share settings snapshots and avoid repeated JSON reads | High across Settings and history | Small to medium |
| 3 | Move device discovery and recording scans off the GUI thread | High with slow drivers or large recording folders | Small to medium |
| 4 | Defer inactive desktop screens and eager imports | High potential for launch; savings need profiling | Medium to large |
| 5 | Update history cards incrementally and fix worker teardown | High for sidebar smoothness | Medium |
| 6 | Make browser history reads bounded and demand driven | High for larger meeting archives | Medium |
| 7 | Virtualize transcripts and construct print content only when needed | High for long meetings and reports | Medium |
| 8 | Start meeting capture and dashboard before model initialization completes | High potential for meeting startup | Medium to large |
| 9 | Reduce complete state copies and serialization under the meeting lock | Increasing benefit as state documents grow | Medium to large |
| 10 | Separate waveform simulation from painting | Medium for animation consistency | Medium |
| 11 | Batch Settings wiring and alias checks within isolated scenarios | 19 fewer collected items and full dialog constructions | Small to medium |
| 12 | Remove repeated invalid title checks across cache dispatch variants | Up to 45 fewer integration cases after equivalence verification | Medium |
| 13 | Exclude numeric matrix rows that immediately skip | 29 fewer collected items without removing an executed comparison | Small |
| 14 | Combine related Meeting Mode geometry checks | Two fewer window setups; keep every distinct input and assertion | Small to medium |
| 15 | Keep pure tests free of irrelevant UI setup and real clock waits | Same case count with less setup and waiting | Medium |

Effort is a relative engineering estimate, not a delivery commitment. Benefits overlap: for example, lazy Settings pages and shared settings snapshots address some of the same first-open work, so their gains must not be added together.

## 1 Build Settings destinations on demand

**Finding.** The dialog itself is created lazily, but its first construction immediately builds all 19 destinations. [_add_page calls](../ui_qt/dialogs/settings_dialog.py#L560) reach an [immediate page builder](../ui_qt/dialogs/settings_dialog.py#L720). Opening Overview therefore constructs model pickers, Downloads, remote controls, Recording and every meeting settings destination.

**Observed cost.** The isolated probe created 1,829 child widgets. First construction took 806 ms; subsequent constructions in the same process took 209–211 ms, with model-cache scans and device enumeration mocked. This is construction time alone.

**Recommended change.** Register destination metadata and page factories, then construct the selected destination on first use. Keep search entries and rail summaries independent of hidden widgets. This also reduces the responsibilities of the 3,768-line Settings module.

**Verify.** Measure first-open and reopened Settings separately, including first paint. Cover every destination, deep-link routing, search activation and persisted values. Preserve the existing background cache scan and coalesced refresh behavior.

## 2 Share settings snapshots and avoid repeated JSON reads

**Finding.** [SettingsManager.load_all_settings](../services/settings.py#L589) reopens and parses the complete JSON file under a shared lock; `get()` calls the same path. The Settings probe counted 88–89 reads during construction. Each custom-provider history card independently resolves its chip and tooltip through [_format_cleanup_info](../ui_qt/widgets/history_sidebar.py#L75), with calls at [line 228](../ui_qt/widgets/history_sidebar.py#L228) and [line 236](../ui_qt/widgets/history_sidebar.py#L236). A 100-card probe counted exactly 200 JSON reads.

**Recommended change.** First pass one settings/provider-name snapshot through each construction or refresh, and compute shared display text once. Then consider a process-local cached read snapshot with explicit invalidation on writes and detection of external replacement. Keep strict read-modify-write semantics, thread safety and defensive handling of nested mutable values.

**Verify.** Count file reads per render and refresh. Check settings changes from desktop controls, agent controls and another process. The measured read count proves redundant I/O; the amount of latency removed depends on the filesystem and work already eliminated by recommendation 1.

## 3 Move device discovery and recording scans off the GUI thread

**Finding.** Constructing the Recording page [populates audio devices immediately](../ui_qt/dialogs/settings_dialog.py#L1014), reaching [PortAudio query_devices](../services/recorder.py#L157). Every Settings show also [refreshes recording usage](../ui_qt/dialogs/settings_dialog.py#L1003). The usage path obtains a [complete recording list](../services/history_manager.py#L239), then enumerates, stats, parses timestamps and sorts every WAV even though the summary only needs count and total bytes.

**Recommended change.** Discover devices on a worker when the Recording destination needs them. Cache usage totals and invalidate them on recording creation/deletion; refresh them in the background. Use a direct count/size scan when rebuilding totals, rather than constructing and sorting all RecordingInfo objects. Keep retention deletion previews separate and accurate.

**Verify.** Inject a slow device query and large recording folder; confirm Settings can paint and respond while work runs. Device-driver latency was deliberately excluded from the local construction probe, so production savings remain unmeasured.

## 4 Defer inactive desktop screens and eager imports

**Finding.** [MainWindow construction](../ui_qt/main_window.py#L438) creates Quick Record, Upload File and Meeting Mode before showing any of them; [HostDashboard](../ui_qt/main_window.py#L500) is also created immediately and hidden. Package initializers eagerly import broad sets of [widgets](../ui_qt/widgets/__init__.py#L1) and [dialogs](../ui_qt/dialogs/__init__.py#L1), pulling Settings code into imports of otherwise smaller features.

**Observed cost.** MainWindow created 717 child widgets; first construction in the isolated process took 524 ms, versus 71–75 ms for warm repeats. Existing recent launch logs show approximately 0.4–0.75 seconds in UIController construction. These measurements do not attribute all that time to inactive screens.

**Recommended change.** Construct the restored visible mode first. Instantiate inactive features on activation or prepare them gradually after first paint. Make package exports lazy and import dialog classes at their use sites. Replace controller assumptions that every tab already exists with a small feature registry. Also move [database initialization](../services/application_controller.py#L953) to a coordinated worker: it currently runs after window.show but before the main event loop begins.

**Verify.** Measure process launch to first visual, usable window, and engine ready separately, across cold/warm launches and all restored modes. [StartupProfiler](../ui_qt/bootstrap.py#L237) currently starts after earlier entry-point imports and native bootstrap work, so its total alone cannot represent complete process startup.

## 5 Update history cards incrementally and fix worker teardown

**Finding.** Database reads are already asynchronous and capped, but [_apply_history_results](../ui_qt/widgets/history_sidebar.py#L902) clears the page and reconstructs up to 100 cards on the GUI thread. Past Meetings [rebuilds its list similarly](../ui_qt/widgets/past_meetings_panel.py#L648). Local results and a later remote merge can cause two rebuilds.

**Observed cost.** With the real theme and 100 synthetic entries, card construction plus subsequent event processing took roughly 125–170 ms. The populated list had 626 child widgets. Async database loading therefore does not remove the visible rendering stall.

**Recommended change.** Diff results by record ID and reuse unchanged cards, or adopt a model/delegate view. As an interim step, insert cards in small batches across event-loop turns. Tie background query delivery to an owner that safely survives or cancels widget teardown: the focused test run observed [_history_loaded.emit](../ui_qt/widgets/history_sidebar.py#L831) raising after HistorySidebar was deleted. A result-generation check inside the receiving slot cannot protect an emit against a destroyed QObject.

**Verify.** Measure unchanged refreshes, search changes and late remote merges. Use delayed query completion while closing/deleting the sidebar to verify teardown safety. Preserve the 100-item cap and search debounce.

## 6 Make browser history reads bounded and demand driven

**Finding.** [/api/meetings](../meeting/web/api.py#L388) loads every meeting and schedules a summary task for each. [summarize_meeting_content](../meeting/content.py#L100) loads all transcript and audio rows to calculate counts and a short preview. The desktop already has an [aggregated summary query](../meeting/persist/repository.py#L263) that the web route bypasses.

The same browser feature also [downloads every transcript page on selection](../webui/src/components/HistoryPane.tsx#L220). Selection cleanup suppresses stale UI updates but does not stop the download loop. While report/review work runs, a two-second interval repeatedly calls the full detail endpoint just to use its state; that endpoint also fetches transcript rows and content metadata.

**Recommended change.** Reuse the aggregated summaries with server-side paging and filtering. Fetch preview metadata separately; hydrate the full transcript only when needed. Propagate AbortSignal through requests and check cancellation between pages. Add a lightweight state/status endpoint and schedule the next poll only after the previous request finishes.

**Verify.** Use archives of increasing size and rapidly change selection. Record query count, rows materialized, response bytes and outstanding requests. Expected scaling improvements are source-backed; real archive/LAN speedups have not been benchmarked.

## 7 Virtualize transcripts and construct print content only when needed

**Finding.** [TranscriptPane](../webui/src/components/TranscriptPane.tsx#L125) renders every segment. [ReportTabs](../webui/src/components/report/ReportTabs.tsx#L121) also mounts a hidden FullMeetingDocument, which contains [another complete transcript](../webui/src/components/report/FullMeetingDocument.tsx#L144). CSS hides layout, but React still constructs and reconciles the duplicate tree.

**Observed cost.** A synthetic render of the visible transcript plus a Brief report produced 1,000 transcript articles from 500 segments. At 5,000 segments it produced 10,000 articles and about 3.75 MB of HTML. This was a static render, excluding browser DOM creation, layout and paint.

**Recommended change.** Window the visible transcript with reliable citation/seek scrolling, and create the full print document only when preparing a full export or entering browser printing. Keep complete transcript data available for exports without keeping duplicate controls mounted.

**Verify.** Profile browser commit/scroll time at 500 and 5,000 segments. Check citation jumps, keyboard access, selection, playback following, browser print and downloaded full reports. Preserve memoized sorting and the existing localized playing-segment updates.

## 8 Start meeting capture and dashboard before model initialization completes

**Finding.** [Meeting startup](../meeting/engine.py#L587) initializes diarization and ASR before starting capture and the dashboard. [MeetingAsrEngine](../meeting/asr/engine.py#L110) synchronously constructs or reloads its backend. Model initialization therefore lies directly on the path to recording and dashboard availability, even though meeting startup itself runs off the Qt thread.

**Recommended change.** Split startup into durable capture, dashboard availability, and processing readiness. Initialize consumers in the background, retain captured chunks, and replay pending chunks in order when ASR becomes ready. The [current missing-ASR branch](../meeting/engine.py#L2001) leaves chunks pending; explicit replay and shutdown ownership must be implemented before reordering startup.

**Verify.** With an intentionally delayed model loader, verify capture starts promptly, every chunk is processed exactly once after readiness, and ending/canceling during startup leaves a recoverable meeting. Maintain model leases, permissions and partial-start cleanup. The ordering is proven; saved startup time will depend on model and device.

## 9 Reduce complete state copies and serialization under the meeting lock

**Finding.** Every operation batch [round-trips the complete state document](../meeting/state/store.py#L142), serializes it for persistence, and may reconstruct it again from the returned snapshot. [The repository rewrites state_json](../meeting/persist/repository.py#L986). Runtime/status updates repeat the same pattern, and subscribers are notified while the store lock remains held. Notes, cards and report Markdown increase the cost of otherwise small changes.

**Recommended change.** Profile cost against state size, then use structural copy-on-write for changed sections and avoid redundant conversions. Keep runtime status lightweight. If necessary, persist ordered events with less frequent complete checkpoints, with explicit crash replay. Treat this as a focused store/repository change rather than a general rewrite.

**Verify.** Measure operation latency and reader lock wait with small and large reports. Preserve transactional failure behavior, sequence ordering, title reconciliation, audit/undo and recovery durability. Do not simply release the lock around notifications: the current locking deliberately prevents out-of-order delivery. User-visible savings are not yet measured.

## 10 Separate waveform simulation from painting

**Finding.** The overlay timer [tracks elapsed time](../ui_qt/overlays/waveform_overlay.py#L485), but ParticleStyle draw methods [emit and advance particles](../ui_qt/waveform_styles/particle_style.py#L121) with fixed `dt = 1/30`. Repainting changes simulation state, while dropped paints slow particle motion.

**Reproduced behavior.** Three draws without advancing the timer produced 8, then 16, then 24 particles and moved existing particles, although animation_time stayed at zero.

**Recommended change.** Advance simulation once per timer update using monotonic, clamped elapsed time. Keep paint methods free of simulation side effects and base emission on elapsed time, retaining fractional emission remainder. This is a cleanup of animation ownership as well as a smoothness fix.

**Verify.** Repeated draws must leave simulation unchanged. Compare motion and emission over equal elapsed time at 30 and 60 FPS, including a delayed frame. Preserve the existing stop behavior for hidden overlay timers.

## Test reduction scope and coverage requirements

The current Python suite collects **4,647 items across 226 test files**. The targets in slots 11–14 could reduce that collection by **95 items**: 19 through batching Settings scenarios, 45 through verified shared validation, 29 by avoiding immediately skipped rows, and two by consolidating window geometry scenarios. This is a planning count, not an implemented or proven suite-wide saving. The grouped tests must still execute every original assertion row with useful subtest labels. Parameterizing duplicate code by itself does not reduce executed cases.

The current `.venv` has neither `coverage` nor `pytest-cov` installed. No before/after statement or branch comparison was produced. Before implementing reductions, record the baseline executed statements and branch edges for the affected production modules, plus a mapping from each removed test to retained assertions and input classes. Require the same relevant statement/branch sets and behavioral checks after the change; a matching aggregate percentage alone is insufficient. Use focused fault injection or mutation checks for validation, persistence and control-wiring regressions. Preserve platform, driver, cancellation, authorization and numerical boundary cases unless their equivalence is demonstrated.

## 11 Batch Settings wiring and alias checks within isolated scenarios

**Finding.** [test_bound_checkbox_saves_its_key_immediately](../tests/test_settings_binder.py#L143) runs 12 rows through a [fixture that constructs the complete Settings dialog](../tests/test_settings_binder.py#L115). Separately, [legacy destination routing](../tests/test_settings_unified.py#L191) constructs nine complete dialogs for nine aliases. These are repeated builds of the same large UI to check different table entries.

**Recommended change.** Keep one isolated dialog per contract test and run the original rows as labelled subtests. For checkbox wiring, restore the saved snapshot and control state under the loading guard before each row; retain the exact toggle and saved-key assertions, and check that unrelated keys stay unchanged. For aliases, reset to Overview and assert that reset before selecting each alias. Keep the generic SettingsBinder unit tests, dialog-load tests and unified routing/integration checks.

**Candidate reduction.** The 12 checkbox items become one item, and the nine alias items become one: **21 → 2 collected items**, avoiding **19 full dialog constructions**. All 21 input rows remain exercised. Do not share a mutable dialog across unrelated tests or widen fixture scope indiscriminately.

**Coverage preservation.** Maintain an explicit row-to-subtest mapping, including the corrupt stored value in the binding table and aliases that share a destination. Check first-open behavior separately so repeated use of an existing dialog cannot replace initialization coverage. Compare the affected wiring/selection branches and diagnostic output before accepting the consolidation.

## 12 Remove repeated invalid title checks across cache dispatch variants

**Finding.** [The title contract matrix](../tests/test_meeting_title_consistency.py#L168) collects 15 titles across nine routes, or 135 cases. Nine titles are invalid. The [REST action normalizes titles](../meeting/web/api.py#L454) before current/cached/uncached dispatch, and [MCP retitle normalizes them](../services/agent_mcp/controls.py#L331) before its dispatch and database work. Repeating each invalid title across those downstream cache variants exercises the same early rejection path.

**Recommended change.** Retain all **54 valid title × route combinations**. Retain the nine invalid title classes through each independent validator/error adapter: repository, state operations, MCP and REST, for **36 invalid cases**. Remove only the invalid combinations duplicated by the REST cached/uncached and MCP current/cached/uncached variants, after verifying their shared rejection paths. Preserve the existing title persistence, undo, stale-cache and partial-audit regressions.

**Candidate reduction.** **135 → 90 cases**, or **45 fewer integration cases**. This is conditional on the coverage map; title type, empty/whitespace input, maximum length, Unicode and control characters must all remain covered.

**Coverage preservation.** Assert that rejected titles do not dispatch to a current/cached store, write state, or create audit events. Keep route-specific authorization and error translation tests. Compare branch sets at every independent entry point and fault-inject a missing normalization check to confirm the retained tests fail. Similar names or the same response code alone do not justify removal.

## 13 Exclude numeric matrix rows that immediately skip

**Finding.** [TestMovingAverage.test_matches_numpy_convolve](../tests/test_audio_processor.py#L431) collects eight signal sizes × nine window sizes, or 72 cases. **29 rows skip before calling application code** because the window is longer than the signal. Those rows still incur collection and the global autouse fixture setup.

**Recommended change.** Generate the exact 43 valid `(size, window)` pairs before collection. Keep the original random seed expression, reference convolution and shape, dtype and tolerance assertions for every valid pair. Do not thin the valid numeric matrix or remove the [block-boundary comparison](../tests/test_audio_processor.py#L446).

**Candidate reduction.** **72 → 43 collected items**, removing 29 skipped items and their fixture work. Every comparison that currently runs still runs; this is the clearest low-risk count reduction in the audit.

**Coverage preservation.** Compare the old non-skipped pair set to the new collected pair set exactly. Preserve all numeric inputs and seeds, not just branch percentages. These skipped combinations currently supply no oversized-window behavior assertion; excluding them must not be represented as removing tested application behavior.

## 14 Combine related Meeting Mode geometry checks

**Finding.** [TestMeetingModeWindowHeight](../tests/test_meeting_mode_tab.py#L191) constructs a complete MainWindow for each of five cases. Three cases separately check finalization expansion, row insets and subsequent shrinkage. Its [settle helper](../tests/test_meeting_mode_tab.py#L229) waits a fixed 350 ms; the five cases call it 14 times, totaling 4.9 seconds of deliberate waiting. In the local baseline, the five call phases ranged from 0.80 to 1.52 seconds.

**Recommended change.** Group the three related finalization checks into one labelled scenario using one isolated window. Preserve both original payload variants, including the explicit `dashboard_available` value and the variant that omits it. Restore and verify the original idle geometry before each independent phase. Retain expansion/minimum-height, row-inset, shrink and released-scroll-space assertions. Keep the Record/Upload static-height and Meeting Mode tab-transition tests separate.

**Candidate reduction.** **Five → three collected items**, avoiding two full window setups. Replacing positive fixed waits with animation-finished signals or bounded geometry predicates can remove additional waiting. Exact time saved depends on the resets needed to preserve the original scenarios; the 4.9-second source total is not a measured removable budget.

**Coverage preservation.** Verify every original payload, starting condition and geometry assertion still executes. Keep a real animation/event-loop integration path. Negative assertions such as no resize or no callback need a controlled timer or appropriate observation window; an immediate assertion would weaken the test.

## 15 Keep pure tests free of irrelevant UI setup and real clock waits

**Finding.** Global autouse fixtures in [conftest.py](../tests/conftest.py#L64) initialize Qt and [enumerate/clean up widgets](../tests/conftest.py#L111) for pure service tests as well as UI tests. The [installed-agent fixture](../tests/conftest.py#L185) also imports and patches agent-picker UI globally. Separately, [MeetingClock arithmetic tests](../tests/test_meeting_clock.py#L15) deliberately sleep for a combined 0.38 seconds even though the clock performs synchronous monotonic-time arithmetic.

**Recommended change.** Make UI-dependent fixtures opt in through a verified marker/fixture inventory; keep user-data paths, database isolation and credential protection in place for all tests that could reach them. Preserve widget destruction and suppression of real installed-agent probes for every UI test. Give pure clock tests a controllable module-owned monotonic clock and advance it directly through start, pause, resume and recovery, instead of sleeping. Avoid replacing the global time module used by pytest and other workers.

**Reduction type.** This leaves the behavioral case count unchanged while reducing per-test setup, imports and waiting. Do not present it as fewer assertions or fewer platform checks. Existing minimal harnesses in [settings_models tests](../tests/test_settings_models.py#L56) and [settings_downloads tests](../tests/test_settings_downloads.py#L99) already demonstrate the narrower construction approach.

**Coverage preservation.** Map which tests create Qt objects, including indirect imports, before changing autouse scope. Run service-only tests with Qt startup instrumented to reject unexpected use. Retain real worker, timeout, cancellation and scheduler integration tests; deterministic clock arithmetic cannot replace those. Verify UI cleanup still prevents abandoned widgets and that tests cannot touch real settings, recordings or credentials.

## Validation and implementation order

The focused baseline run passed 74 tests across bootstrap, history scalability, startup visibility, meeting start readiness and unified Settings in 12.31 seconds. It emitted the late HistorySidebar worker warning described in recommendation 5, plus an environment warning that pytest could not write its existing cache directory. Ruff F checks passed across Python source, scripts, benchmarks and tests. All Python files parsed successfully, and git diff --check found no whitespace errors. No full Python suite, browser interaction suite or production ASR benchmark was run for this report.

The test-reduction follow-up collected the complete Python suite without running it: 4,647 items in 3.11 seconds. Its six-file baseline run passed **340 tests and two subtests**, with **29 skips**, in **36.43 seconds**, using `--durations=12` and disabling pytest's cache provider. The selected files were audio processing, meeting title consistency, SettingsBinder, unified Settings, MeetingClock and Meeting Mode. The detailed local output is in `test-reduction-baseline.txt` (local scratch output). Neither tests nor application source were edited, so no reduced-suite speedup or coverage equivalence is claimed.

Implement recommendations 1–3 first, then measure Settings opening again before adding further caching complexity. Address 5 alongside the teardown regression. For the browser, combine 6 and 7 into a bounded history-data and rendering pass. Recommendation 4 requires controller lifecycle changes, and 8–9 require stronger cancellation, ordering and persistence verification, so they should follow the smaller changes.

For test reduction, start with the exact skipped-pair cleanup in 13 and the isolated Settings batching in 11. Establish the assertion/branch map before pruning the title routes in 12 or consolidating lifecycle checks in 14. Apply fixture scoping in 15 after a complete dependency inventory. An AST scan found four identical sidecar test-body pairs across Pi and OpenCode, but they use different driver fixtures: preserve execution for both drivers. Likewise, retain the numerical seam test and the full startup visibility matrix; these exercise meaningful boundaries and interactions.

Keep the existing optimizations: deferred speech-model loading, cache-first model access, bounded background history queries, search debounce, isolated cancellable inference, nonblocking normal preview stop, shared model-cache scans, throttled download progress and hidden-animation shutdown. The unused-name lint pass was clean; the highest-value cleanup is reducing repeated work and clarifying ownership, rather than removing imports indiscriminately.
