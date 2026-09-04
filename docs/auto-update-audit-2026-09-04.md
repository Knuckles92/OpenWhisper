# Auto-update audit — September 4, 2026

Reviewed OpenWhisper 2.5.1 at commit `2097400` across release discovery, verified downloads, native preparation, Qt handoff, helper commit/recovery, startup health acknowledgement, Inno Setup, packaging, and CI. The findings below describe that baseline. The follow-up implementation targets 2.5.2; see the status below.

The existing design has useful safeguards: exact asset selection, SHA-256 verification, bounded extraction, manifest checks, installation binding, a journal written before directory moves, process-identity checks, and rollback. The recurring failures are concentrated at the boundaries between installed versions, processes, and the two installation paths. Two issues should block resuming native archives: stale recovery can undo a newer repair, and old clients can skip the setup-only bridge.

## Implementation status

Implemented for 2.5.2:

- Setup retires native transactions before replacing files, uses a durable backup of the managed runtime, verifies the result, restores the old runtime on failure, and preserves legacy GPU files and user data.
- New native manifests require updater 2.5.2. Local builds keep the bridge archive under .tmp for validation only; release bundling enforces setup-only publication for the bridge.
- Worker and Qt deliveries carry attempt identities. Canceled and superseded preparations are discarded; handoff checks active work again before exiting.
- Complete partial downloads finalize without a Range request, HTTP 416 restarts once, and short responses retain a resumable prefix.
- Preparing and terminal transactions retain enough information to retry cleanup, including locked rollback and failed trees. Startup dispatches interrupted recovery before normal initialization.
- Rollback snapshots settings and SQLite through its backup API, including committed WAL records. A completed rollback never replays that snapshot over subsequent user work.
- Health acknowledgement requires database initialization and a matching transaction; an early process exit fails promptly. Independent frozen launches sanitize PyInstaller environment and Windows DLL search state.
- Windows CI runs updater/controller regressions. Release builds test the frozen helper and add a disposable previous-release setup upgrade, stale-file check, self-test, data-preservation check, and uninstall.

The setup lifecycle job must pass on the release runner before publication. Full native previous-version GUI automation and physical power-loss testing remain outside the automated lifecycle check; failure-injection tests cover the transaction boundaries.

## Original findings

### 1. P1 — An old recovery transaction can undo a newer installer repair

[Recovery decision](../services/app_update_apply.py), [rollback replacement](../services/app_update_apply.py), [setup implementation](../installer/OpenWhisper.iss).

An interrupted native update can retain its journal, rollback tree, helper, and RunOnce recovery command. Setup installs a newer version without retiring those transactions. When the old recovery later runs, it checks the registered path and uninstaller name but does not establish that the active installation still belongs to that transaction. If the active executable matches neither recorded version and the old rollback tree validates, it replaces the active installation with the rollback tree.

**Reproduced:** staged an interrupted `2.4.0 → 2.4.1` transaction, overlaid a simulated `2.5.1` setup payload, and supplied registration reporting `2.5.1`. Calling recovery changed the executable from `repaired-2.5.1` to `old-2.4.0` and returned `rolled_back`. Registry writes and process launches were mocked; filesystem reconciliation was real.

**Change:** make setup reconcile or retire transactions belonging to its exact target installation before replacing files, including their recovery commands. This must work against already shipped helpers, not just a newly fixed helper. Recovery should distinguish an independently repaired installation from its own damaged candidate and must not overwrite a validated later installation. Do not solve this by requiring exact `DisplayVersion` equality again; that would reintroduce the stale-registration problem fixed in 2.5.1.

**Required regression:** interrupt native apply, install a newer setup, then run the original transaction's recovery and confirm the repaired version survives.

### 2. P1 — Setup-only releases do not protect users who skip them

[Minimum updater version](../services/update_contract.py), [manifest enforcement](../services/app_update_apply.py), [helper selection](../services/app_update_apply.py).

The manifest still declares `minimum_updater_version = "2.4.0"`. A user remaining on a broken 2.4.x build can skip 2.5.1 and go directly to the next release containing an archive. That update uses their old download, preparation, handoff, and helper code. Merely restoring dual artifacts one release after a fix does not establish that installed users received the fix.

**Verified against history:** extracted and executed `validate_manifest` from tags `v2.4.0`, `v2.4.2`, `v2.4.4`, `v2.4.6`, and `v2.5.0`, using shared validation dependencies and a small future-version fixture. All accept the current minimum; all reject a manifest minimum of `2.5.1` with the existing installer-required error. This checks the historical validator, not a full run of those historical binaries.

**Change:** raise and maintain a monotonic minimum for native apply. At least 2.5.1 excludes the known earlier defects; if this audit's fixes ship later, use the first version whose updater passes the required upgrade tests. Old clients already understand this field, so this protection can be delivered in the target artifact. It still requires an archive download before old clients discover the incompatibility. Future clients should consult trusted compatibility metadata before downloading and use setup directly for an incompatible updater/layout.

Route a typed compatibility failure to the verified setup path. Do not treat every native error as permission to bypass verification. Keep setup-only releases as the emergency fallback.

### 3. P2 — Cancel can lose a race with a queued successful completion

[Worker completion](../services/application_controller.py), [UI completion](../ui_qt/ui_controller.py).

The worker checks its cancellation/attempt token before emitting success, but Qt delivers the result later. The user can cancel between those events. The UI notices cancellation only for logging and still starts the updater and exits. The completion signal also does not carry an attempt identifier, so a queued result from an earlier attempt cannot be rejected reliably after a retry.

There is a second ownership gap: if cancellation wins just after `prepare_candidate` saves its journal, the worker discards the successful handoff without disposing of the candidate. Startup pruning preserves every `prepared` journal, and RunOnce is not registered until commit, so this candidate can remain indefinitely. A helper launch failure leaves the same kind of prepared transaction.

**Reproduced:** delivered a successful native result to a canceled controller with no open dialog; launch and exit were both requested. Separately canceled immediately after preparation; the worker reported cancellation while the candidate and `prepared` journal survived startup pruning.

**Change:** carry attempt identity through progress and completion signals, recheck it and cancellation on the UI thread immediately before handoff, and give each prepared transaction an explicit owner. Canceled, superseded, or unlaunchable preparations need an idempotent abandon operation. Do not delete prepared transactions solely on age while a worker/helper may still own them.

### 4. P2 — A complete partial download can retry HTTP 416 indefinitely

[Resume handling](../services/app_update.py), [network-error handling](../services/app_update.py).

The downloader resets a partial only when its size exceeds the expected size. A partial exactly equal to the expected size is hashed, then requested again with `Range: bytes=<size>-`. A server returning 416 leaves the partial intact, so Retry repeats the same request. This can arise if a process stops after receiving the full file but before finalization.

**Reproduced:** a valid 17-byte partial produced `Range: bytes=17-` on two successive attempts, both failed with HTTP 416, and the partial survived both attempts.

**Change:** verify and finalize an exact-size partial without a network request. Discard/restart it if its digest is wrong. Handle a stale range with a bounded restart from zero while retaining exact-size and digest checks. Also distinguish explicit cancellation from a truncated transfer: the current incomplete-size error deletes resumable data.

### 5. P2 — Cleanup can discard the only record of a locked old tree

[Tree cleanup](../services/app_update_apply.py), [startup pruning](../services/app_update_apply.py).

Cleanup ignores failures deleting candidate/rollback directories, then removes the transaction or delegates deletion of its journal. Startup pruning only examines the transaction root; it cannot subsequently find the sibling `.old-*` or `.new-*` tree whose journal is gone. Windows file locks can therefore leave an entire old installation behind after an otherwise successful update.

**Reproduced:** simulated a locked rollback directory during cleanup. The old directory survived, its journal was removed, and a subsequent startup prune removed nothing.

**Change:** retain a terminal cleanup record until every owned path has been collected. Retrying cleanup should not change the successful update result. Preserve exact path ownership and reparse checks; do not broadly delete directories matching `.old-*` or `.new-*` names.

### 6. P2 — Setup recovery can leave a mixture of runtime versions

[Setup file copy](../installer/OpenWhisper.iss), [uninstall-only cleanup](../installer/OpenWhisper.iss).

Setup recursively copies the new bundle over the existing installation. There is no installation-time reconciliation of files removed from the new bundle. `_internal` removal appears under `[UninstallDelete]`, so it does not clean a normal upgrade. Old Python extensions, Qt plugins, or DLLs can remain when dependencies change. This is a structural risk identified from the installer script; a specific stale-DLL startup failure was not reproduced here.

**Change:** define which files setup owns and reconcile them against the new payload, preserving only explicit legacy exceptions. Prefer a staged replacement or targeted removal after the app has stopped. Adding a blanket deletion without accounting for interruption and the preserved NVIDIA tree would introduce another failure mode. Inno's [installation-time deletion section](https://jrsoftware.org/ishelp/topic_installdeletesection.htm) is separate from its uninstall cleanup.

**Required regression:** install a previous bundle containing a retired module/DLL, upgrade through setup, and verify the retired file is gone while settings, history, recordings, components, and model caches remain.

## Release controls that would prevent recurrence

The [normal CI workflow](../.github/workflows/ci.yml) runs Python tests on Linux. The [Windows release job](../.github/workflows/build-installers.yml) builds and performs a [frozen application self-test](../scripts/build_installer.ps1), but does not run the updater regression suite or an installed-to-installed upgrade. The documented local feed is useful; the upgrade exercise still depends on a person following the checklist.

Make these automated gates for a native release:

- Run the updater suite on Windows, with preferences, registry, and mutexes isolated from the runner's own state.
- Install a retained previous-release binary in a disposable Windows environment and upgrade it to the candidate using the local feed. Exercise the real shortcut working directory, frozen helper, process exit, restart, and registered version.
- Test the oldest supported direct-upgrade version and a version that must use setup, not only the immediately previous release. A generated tiny payload can test the protocol, but cannot replace the real frozen-binary test.
- Exercise locked files, a failed health check, and process termination between journal writes/directory moves. Then retry, reboot/log on, and repair with setup.
- Run two sequential native upgrades and an uninstall, checking preserved user data and the absence of orphaned payloads.
- Gate the archive's minimum updater version and topology revision against those results. Preserve build provenance and the existing draft/digest checks.

The setup-only workflow input defaults to false. That is workable as an explicit release choice, but it is not an enforced property of the 2.5.1 source tag. The build/release gate should make the intended recovery policy reviewable and verify it before publication.

## Further compatibility work

- **Recovery discovery:** the entry point gates launches on live mutexes but does not reconcile unfinished journals before ordinary startup. Recovery is reached through the helper's explicit recovery argument, normally registered in RunOnce. Treat RunOnce as a fallback, and add a durable startup/recovery protocol for transactions left behind without it. Windows runs these commands at logon, not whenever the app is opened; see [Microsoft's RunOnce documentation](https://learn.microsoft.com/en-us/windows/win32/setupapi/run-and-runonce-registry-keys).
- **Readiness and data compatibility:** [startup acknowledgement](../ui_qt/bootstrap.py) is scheduled 1.5 seconds after deferred initialization begins; it does not await every background service/model load. Define required local readiness without depending on network/model downloads. The helper restores binaries, not migrated databases/settings. Keep migrations backward compatible across the rollback window or make a consistent pre-migration backup and explicit restore policy. Do not blindly restore data after allowing the new app to accept user work.
- **Frozen process contract:** standardize independent helper/app launches and review inherited DLL search paths as PyInstaller changes. The helper relaunch clears private bootstrap environment variables, but external-program launch behavior also involves Windows DLL search state. PyInstaller documents this [inheritance and sanitization requirement](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#launching-external-programs-from-the-frozen-application). This is additional hardening, not a reproduced launch failure in this audit.

## Original audit validation and limits

The repository virtual environment was activated for every Python invocation.

- Existing updater/setup suite: **151 passed, 3 failed** in the initial run. The three failures read the developer's saved automatic-check preference while assuming it was enabled.
- The initial failures exposed a test-isolation issue: dialog cancellation wrote to real settings. A temporary settings-file fixture now isolates every dialog test.
- Ordinary updater suite after the settings-file fixture: **154 passed in 5.38 seconds**, with no preference overrides required.
- Disposable filesystem/mocked-process probes reproduced findings 1, 3, 4, and 5. Historical-validator probes confirmed finding 2 and that a higher manifest minimum is already understood.
- No real application install, release publication, registry modification, or actual power loss was performed. No full frozen upgrade or Inno lifecycle was executed. Regression cases were added to the test suite.

Recommended order: fix setup/recovery ownership and the native compatibility floor first; fix handoff cancellation and transaction disposal next; then add download/cleanup regressions and make the actual Windows upgrade lifecycle a release gate before restoring native archives.

## Follow-up validation

- Full Windows suite after correcting portable test fixtures: **1,885 passed,
  9 skipped**, plus 4 passing subtests (294.58 seconds).
- Final legacy recovery/cleanup change: **85 updater and resilience tests passed**.
- Database readiness and transaction acknowledgement: **9 bootstrap tests passed**,
  including three new health-path cases.
- Actual frozen helper protocol: prepare, rollback, and clean replacement passed
  against disposable installation fixtures.
- Frozen application startup/import self-test passed during Windows packaging.
- New-module lint, changed-code undefined-name checks, workflow YAML parsing,
  PowerShell syntax, and git whitespace checks passed.
- The GitHub previous-release setup lifecycle is configured as a release gate but
  was not run against a live registered installation in this local session.

Final Windows installer compiled successfully: `installer/Output/OpenWhisper-Setup-2.5.2.exe` (122,532,191 bytes). SHA-256: `035687feecc22c2140af27bef713b60367dc61e84ad416ced8a142645a541aa8`. The final frozen helper protocol test passed again after the last recovery fix. No 2.5.2 native archive is present in release output; the verified archive is retained only under `.tmp/update-validation`.
