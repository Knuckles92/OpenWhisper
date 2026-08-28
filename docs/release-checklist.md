# Windows release checklist

Publish every stable release from a **draft** GitHub release that already has both artifacts. Old installer copies only understand the setup exe and can skip intermediate tags.

Exception: a release that fixes the update path itself must be setup-only. See [Fixing the update path](#fixing-the-update-path).

## Build

1. Set `_version.py` to the release version. The first updater-capable bootstrap
   must be `2.4.0` or newer.
2. Activate the venv: `.\venv\Scripts\activate`
3. `pip install -r requirements.txt -r requirements-build.txt`
4. `.\scripts\build_installer.ps1 -Clean`
5. Confirm `installer\Output\` contains:
   - `OpenWhisper-Setup-<version>.exe`
   - `OpenWhisper-<version>-win64.tar.xz`
6. Confirm the script printed SHA-256 values for **both** files.

Optional signing: set `OPENWHISPER_SIGN_PFX` and `OPENWHISPER_SIGN_PASS` before the build. The app exe, updater helper, and setup exe are signed; the archive is packed after that.

`-SkipInstaller` still packs the native archive from `dist\OpenWhisper`.

## Soak the native path before publishing

Nothing published is needed. `OPENWHISPER_UPDATE_FEED_URL` points the update
check at a stand-in for GitHub's `/releases/latest`, and
`scripts\serve_update_feed.py` serves the build in `installer\Output` with the
same asset names, sizes, and `sha256:` digests GitHub would. Assets must come
from the feed's own origin; everything else is verified as it is against GitHub.

1. Install the release users will update **from** with its setup exe — the
   previous stable, or whatever is already installed on the test machine.
2. Build this release (above), then `python scripts\serve_update_feed.py`.
3. In a second shell, start the *installed* app with the override:
   `$env:OPENWHISPER_UPDATE_FEED_URL = "http://127.0.0.1:8765/releases/latest"; & "$env:LOCALAPPDATA\Programs\OpenWhisper\OpenWhisper.exe"`
4. Help → Check for Updates → Update. The app should come back on the new
   version, and `%LOCALAPPDATA%\OpenWhisper\updater.log` should end with
   `Update to <version> is healthy`. Anything else is a bug in the path users
   are about to take.
5. Check Add/Remove Programs shows the new version, then run the app once more
   without the override.

The soak exercises the *installed* version's launch code and helper, which is
the code that will run for users. It cannot exercise a fix to that code — that
is what setup-only releases are for.

## Draft, then publish

1. Create a GitHub **draft** for tag `v<version>` (not latest yet).
2. Upload both artifacts. Wait until each asset shows a `digest: sha256:…` field.
3. Confirm exact names:
   - `OpenWhisper-Setup-<version>.exe`
   - `OpenWhisper-<version>-win64.tar.xz`
4. Confirm sizes match the local files.
5. Smoke-test:
   - Fresh per-user Inno install of this build, finishing with **Launch
     OpenWhisper** checked: setup still holds its mutex at that moment, so this
     is the launch the startup gate is most likely to get wrong
   - In-app update from the previous updater-capable build (native path), then
     read `%LOCALAPPDATA%\OpenWhisper\updater.log`: it names the phase that
     failed, and the locks the commit waited out on the way to succeeding
   - Program Files / elevated install still offers the setup exe
   - Uninstall after a native update, both keeping and deleting user data
6. Publish the draft. Do not publish a latest stable release with only one of the two artifacts.

## Apply rules

- Native apply is only for a validated HKCU Inno install whose `InstallLocation` matches the running exe and that already contains `OpenWhisperUpdater.exe`.
- Missing archive, missing digest, HKLM/Program Files, or an unsupported manifest/topology uses the setup exe.
- A setup-only release is the emergency switch that disables native apply.

## Fixing the update path

The download, the prepare, the handoff, and the helper that commits all run from
the version being updated **from** — the helper is copied out of the old install
before the app exits. A fix to any of them cannot reach the builds that need it,
so it takes effect one release later than it ships.

Three fixes have needed this: 2.4.2 fixed a handoff that never exited (2.4.0 and
2.4.1 both had it), 2.4.4 made the commit wait out a file lock instead of
failing on the first sharing violation (2.4.2 and 2.4.3 both fail on it), and
2.4.6 stopped the helper from inheriting the app's working
directory — the install directory, when started from a shortcut — which held
the move-aside locked for the helper's whole life (2.4.4 and 2.4.5 both fail on
it). In each case:

1. Publish the fix **setup-only**. With no archive asset, `resolve_apply_mode`
   returns `SETUP` and no affected build can enter the native path.
2. Delete the archive asset from the release those builds are currently offered,
   so they stop entering it before the fix is even published. Do this only for a
   defect that leaves the install broken or the app stuck: a commit that fails
   and rolls back cleanly costs one download and is worth the diagnostic.
3. Rely on the setup exe to close the stuck app: `CloseApplications=force` plus
   the `CloseRunningApp` taskkill in `installer\OpenWhisper.iss` are what make
   installing over a build that cannot quit itself work at all.
4. Resume dual artifacts in the following release, once a fixed build is the one
   doing the updating. Soak `fixed → next` first (see [Soak the native
   path](#soak-the-native-path-before-publishing)); the fixed build must be the
   installed one.

## First native-capable train

1. Ship a bootstrap release (helper + manifest + Inno mutex/uninstall cleanup) that existing 2.3.x users install through Inno.
2. Soak `bootstrap → candidate` on a disposable Windows VM.
3. Publish the next patch with both complete assets. That is the first native in-app update.
