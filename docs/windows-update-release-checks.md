# Windows update release checks

The updater contract is in services/update_contract.py. Treat its minimum updater
version as a tested compatibility boundary: an installed helper prepares and
commits the next release, so changing only the new helper cannot repair an old one.

2.5.2 is the required setup bridge. The local builder places its native archive in
.tmp/update-validation solely for archive verification; only its setup executable
belongs in a release. Release bundling enforces this even if the emergency
setup-only input is false. Keep setup assets on every later Windows release.

Before publishing a candidate, require the Windows workflow to pass:
1. Updater, controller, and bootstrap regressions.
2. Frozen application imports and bundled native-library checks.
3. The frozen helper's prepare, rollback, and clean replacement protocol.
4. Installation of the latest stable setup, upgrade through the candidate setup,
   removal of a retired runtime file, preservation of user data, frozen self-test,
   and uninstall without deleting retained user data.
5. Release inventory, build metadata, and GitHub asset digests.

The setup lifecycle script supplies its own silent flags. Passing it proves
installation and data preservation, but does not prove that the old app launches
setup silently. The 2.5.1 launcher supplies no arguments, so updating to the
2.5.2 bridge opens the wizard. Release notes must state this. The setup handoff
regression tests protect the silent flags and restart marker in 2.5.2 onward.

scripts/smoke_windows_setup.ps1 refuses to run outside a disposable GitHub Actions
runner or over an existing registered installation. Logs are preserved as a
workflow artifact. Never use a developer's live installation for this gate.

For the first native release after the bridge, additionally exercise an actual
installed 2.5.2 application against the documented loopback release feed. Verify
the old installed helper performs the handoff, the new main window acknowledges
health, the registered version advances, and settings/history survive. Repeat
with a denied launch and an interrupted commit. The unit suite injects these
failures, but does not automate the previous version's entire GUI.

When changing journal states, manifest schema, topology, startup health, or data
migrations, revisit the minimum updater version and ship another setup bridge
when old helpers cannot safely complete the new protocol. Database migration
failures must be reported before health acknowledgement; optional model downloads
must not determine updater health.
