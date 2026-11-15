# macOS installer auto-paste

Auto-paste sends Cmd+V from the running OpenWhisper process. It requires
Accessibility permission for that installed copy. Microphone, Input Monitoring,
and Screen & System Audio Recording are separate permissions; granting them
does not authorize auto-paste.

## User recovery

1. Open Settings → Home → Set up auto-paste… in OpenWhisper.
2. Open Accessibility Settings and turn on OpenWhisper. Check the app path shown
   in setup if multiple copies are installed.
3. Return to OpenWhisper. Setup checks the running process every second and
   reports when access is enabled.
4. If the switch is already on but access remains undetected, choose
   **Repair old Accessibility entry…**. This removes only this app's Accessibility
   entry and requests access again. Enable the current app in System Settings;
   use + to add the displayed bundle if necessary.
5. If repair fails, setup explains how to remove and re-add the entry manually.
   Quit and reopen the installed app if macOS still does not detect access.

Opening Settings never resets permissions. Repair is explicit, unavailable
while access is working, and runs at most once successfully per setup window.
Development launches must not reset Python.app's shared entry. Clipboard paste
remains available while access is missing.

Home checks permission while visible, so its warning and setup link update
after access is granted or revoked without reopening Settings. This check only
reads Accessibility status; it does not refresh devices or models.

## Preserve permission across releases

The installed copy inspected on November 15, 2026 was ad-hoc signed, with no Team
Identifier and a designated requirement consisting of a specific code hash.
Its Accessibility switch was off and OpenWhisper reported no access. This
confirms the immediate permission problem; it does not establish why the switch
became off.

After the user approved the macOS authentication prompt, the installed setup
dialog detected access without restarting. Home retained its old warning;
the source fix adds a lightweight permission refresh to that page.

The user confirmed that the physical Control+Option+R recording test pasted
successfully after the installed app was relaunched. Computer-use simulated
shortcuts did not trigger the Carbon hotkey; earlier button-driven tests
transcribed the generated sample and reported a paste, but insertion into the
tool's TextEdit window was not observed. The successful physical check is
user-confirmed, not independently observed through the tool. This does not
establish that relaunch was necessary. Update-to-update permission continuity
has not been tested in this session.

Ad-hoc signing also explains why permission may need granting again after an
update: its designated requirement identifies one build. Apple explains how
compatible designated requirements preserve access to privacy-protected
resources in [TN3127](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements/).

For distributed releases, use the existing Developer ID signing path in
`scripts/build_installer_macos.sh` and `.github/workflows/build-installers.yml`:
configure `OPENWHISPER_MACOS_CODESIGN_IDENTITY` and the corresponding notary
credentials. Keep the bundle identifier `tech.fiorilabs.openwhisper` and the
Developer ID team consistent between versions. Notarization supports trusted
distribution; compatible signing identity is what matters for permission
continuity. Moving from ad-hoc to Developer ID can itself require a new grant.
Do not weaken the designated requirement to a bundle identifier alone.

The existing GitHub Actions path reads `MACOS_CERTIFICATE_P12`,
`MACOS_CERTIFICATE_PASSWORD`, `MACOS_NOTARY_KEY_P8`, `MACOS_NOTARY_KEY_ID`, and
`MACOS_NOTARY_ISSUER` secrets. Setting up those credentials is still required
before this repository's release pipeline can produce Developer ID builds.
Incomplete credentials fail the build instead of silently falling back to
ad-hoc signing. The builder also verifies the Developer ID Application
authority and TeamIdentifier.

After configuring credentials, set the repository's Actions variable
`MACOS_REQUIRE_SIGNING` to `true`. With that variable set, missing credentials
also fail the build, so a later configuration change cannot accidentally
replace a signed release with an ad-hoc build. With no credentials and no
requirement, preview builds remain available and emit an explicit warning
about permission loss across updates.

## Validation

Automated coverage: `tests/test_accessibility_setup.py` checks permission polling,
startup recovery, opening Settings without a reset, explicit repair, successful
repair running once, failed repair remaining retryable, and preserving working
access. It also checks wrapped guidance layout with a long development path.
`tests/test_settings_unified.py` checks that Home updates after access changes
and stops polling when hidden.

The setup, unified Settings, Carbon hotkey, and macOS installer suites passed together:
161 tests and 64 subtests. The new source changes have not been packaged or
installed over the existing app.

For release qualification, test the actual installed bundle:

1. With Accessibility off, dictate into a disposable text document. Confirm a
   clipboard fallback and no claim that auto-paste succeeded.
2. Enable access through setup and confirm it detects the change.
3. Dictate with the text document focused. Confirm the transcript appears there.
4. Quit and relaunch; repeat the paste check.
5. Update between two builds signed with the same Developer ID team. Repeat
   without re-granting permission. Separately exercise ad-hoc update recovery.

Permission status alone is not an end-to-end paste test. Record actual text
insertion separately from the permission check.
