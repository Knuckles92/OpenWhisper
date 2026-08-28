# Windows release checklist

Publish every stable release from a **draft** GitHub release that already has both artifacts. Old installer copies only understand the setup exe and can skip intermediate tags.

Exception: a release that fixes the handoff itself must be setup-only. See [Fixing the handoff](#fixing-the-handoff).

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

## Draft, then publish

1. Create a GitHub **draft** for tag `v<version>` (not latest yet).
2. Upload both artifacts. Wait until each asset shows a `digest: sha256:…` field.
3. Confirm exact names:
   - `OpenWhisper-Setup-<version>.exe`
   - `OpenWhisper-<version>-win64.tar.xz`
4. Confirm sizes match the local files.
5. Smoke-test:
   - Fresh per-user Inno install of this build
   - In-app update from the previous updater-capable build (native path)
   - Program Files / elevated install still offers the setup exe
   - Uninstall after a native update, both keeping and deleting user data
6. Publish the draft. Do not publish a latest stable release with only one of the two artifacts.

## Apply rules

- Native apply is only for a validated HKCU Inno install whose `InstallLocation` matches the running exe and that already contains `OpenWhisperUpdater.exe`.
- Missing archive, missing digest, HKLM/Program Files, or an unsupported manifest/topology uses the setup exe.
- A setup-only release is the emergency switch that disables native apply.

## Fixing the handoff

The download, the prepare, the handoff, and the helper that commits all run from
the version being updated **from** — the helper is copied out of the old install
before the app exits. A fix to any of them cannot reach the builds that need it.

2.4.2 fixed a handoff that never exited (2.4.0 and 2.4.1 both had it), so:

1. Publish the fix **setup-only**. With no archive asset, `resolve_apply_mode`
   returns `SETUP` and no affected build can enter the native path.
2. Delete the archive asset from the release those builds are currently offered,
   so they stop entering it before the fix is even published.
3. Rely on the setup exe to close the stuck app: `CloseApplications=force` plus
   the `CloseRunningApp` taskkill in `installer\OpenWhisper.iss` are what make
   installing over a build that cannot quit itself work at all.
4. Resume dual artifacts in the following release, once a fixed build is the one
   doing the updating. That release is the first real test of the native path,
   so soak `fixed → next` on a VM before publishing it.

## First native-capable train

1. Ship a bootstrap release (helper + manifest + Inno mutex/uninstall cleanup) that existing 2.3.x users install through Inno.
2. Soak `bootstrap → candidate` on a disposable Windows VM.
3. Publish the next patch with both complete assets. That is the first native in-app update.
