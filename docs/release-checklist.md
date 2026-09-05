# Native release checklist

Every stable release ships the Windows, Linux, and macOS packages together from
a **draft** GitHub release. The Windows native-update archive is also required
except for a documented setup-only recovery release.

## Required artifacts

For version `<version>`, the draft must contain exactly these application
artifacts (plus `BUILD-METADATA.json` and `SHA256SUMS.txt`):

- `OpenWhisper-Setup-<version>.exe` — Windows per-user installer
- `OpenWhisper-<version>-win64.tar.xz` — Windows native-update payload
- `OpenWhisper-<version>-linux-amd64.deb` — Debian/Ubuntu x86-64 package
- `OpenWhisper-<version>-linux-x86_64.pkg.tar.zst` — Arch Linux x86-64 package
- `OpenWhisper-<version>-macos-arm64.dmg` — Apple Silicon macOS 14+ DMG
  containing an ad-hoc-signed app (unnotarized preview)

`BUILD-METADATA.json` records the exact full Git commit, source tag/ref, UTC
build time, and GitHub Actions run that produced the artifact set. It is also
covered by `SHA256SUMS.txt`.

A setup-only emergency release omits only the `win64.tar.xz`; it never omits
the Windows setup exe, either Linux package, or the macOS DMG.

## Build

1. Set `_version.py` to the release version. The Windows updater-capable
   bootstrap must remain `2.4.0` or newer. Update and review
   `requirements-release-constraints.txt` only when intentionally changing the
   native builders' dependency set; release jobs never resolve around it.
2. Create and push the matching tag `v<version>`, then create a GitHub
   **draft** release for that existing tag. The upload workflow checks out the
   tag itself; it never publishes artifacts built from an unrelated branch.
3. Run **Build native installers** from GitHub Actions and set `release_tag` to
   that draft tag. The workflow:
   - builds Windows on `windows-2022`;
   - builds Linux on Ubuntu 22.04, which establishes the glibc compatibility
     floor;
   - builds the Apple Silicon DMG on `macos-14` (arm64 host asserted);
   - runs package tests and frozen-bundle verification;
   - installs, launches, and removes the generated `.deb` on Ubuntu 22.04 and
     Debian 12, and the generated `.pkg.tar.zst` on Arch;
   - freezes and self-tests the ad-hoc-signed macOS `.app`, verifies its code
     signature, and packs it into a UDZO DMG (no notarization secrets);
   - combines the five artifacts, records their source commit in
     `BUILD-METADATA.json`, and generates `SHA256SUMS.txt`;
   - refuses to upload unless the destination exists, is still a draft, and
     matches `_version.py`.
4. Download the combined workflow artifact and retain it with the release
   records.
5. Confirm each GitHub asset displays a `digest: sha256:…` value and that every
   value matches `SHA256SUMS.txt`.

### Local Windows build

```powershell
.\venv\Scripts\activate
pip install -r requirements.txt -r requirements-build.txt -c requirements-release-constraints.txt
winget install -e --id JRSoftware.InnoSetup
.\scripts\build_installer.ps1 -Clean
```

The script freezes the app, verifies Windows DLLs/assets, optionally signs the
app/helper/setup, and creates the setup exe plus native-update archive.
`-SkipInstaller` still creates the archive from `dist\OpenWhisper`.

Optional signing uses `OPENWHISPER_SIGN_PFX` and
`OPENWHISPER_SIGN_PASS`. Sign the app exe, updater helper, and setup before the
archive is packed.

### Local Linux build

Build on x86-64 Ubuntu 22.04 (or the release workflow), not a newer workstation,
so PyInstaller cannot accidentally raise the official glibc floor.

```bash
sudo apt install -y \
  python3 python3-venv python3-dev build-essential binutils dpkg-dev file patchelf \
  desktop-file-utils libarchive-tools lintian xvfb xauth zstd \
  libdrm2 libegl1 libgl1 libportaudio2 \
  libwayland-client0 libwayland-cursor0 libwayland-egl1 \
  libxcb-cursor0 libxkbcommon-x11-0 \
  libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libxcb-xkb1
python3 -m venv venv
venv/bin/pip install -r requirements.txt -r requirements-build.txt -c requirements-release-constraints.txt
./scripts/build_installer.sh --clean
```

The build must finish with all of these gates passing:

- required Qt, web, icon, CTranslate2, ONNX Runtime, and PyAV files exist;
- forbidden Torch/CUDA/scipy/sympy/networkx trees did not leak in;
- `ldd` finds no unresolved library in any bundled ELF;
- `OpenWhisper --version` reports the release version;
- the frozen import/Qt self-test passes under Xvfb;
- the desktop file validates and Lintian reports no unoverridden error tags;
- package name/version/architecture and required paths pass `dpkg-deb` checks;
- the generated Debian `Depends` and Arch `depend` lines record the highest
  GLIBC symbol required by the actual bundle;
- the frozen tree does not contain `libstdc++.so.6` or `libgcc_s.so.1`;
- the Arch artifact starts with `.PKGINFO`, includes a gzip-compressed
  `.MTREE`, and contains the same launchers, desktop entry, and icon.

The results are `installer/Output/OpenWhisper-<version>-linux-amd64.deb` and
`installer/Output/OpenWhisper-<version>-linux-x86_64.pkg.tar.zst`.
`--skip-package` stops after frozen-tree verification.

### Local macOS build

Build on an **Apple Silicon** Mac running macOS 14 or newer. The script refuses
Intel hosts and non-Darwin environments.

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt -r requirements-build.txt -c requirements-release-constraints.txt
./scripts/build_installer_macos.sh --clean
```

The build must finish with all of these gates passing:

- `OpenWhisper.app` exists with bundle id `tech.fiorilabs.openwhisper`;
- `CFBundleShortVersionString` / `CFBundleVersion` match `_version.py`;
- `LSMinimumSystemVersion` is `14.0`;
- `NSMicrophoneUsageDescription` and `NSAudioCaptureUsageDescription` are set;
- `NSScreenCaptureUsageDescription` is **not** present;
- frozen `--version` and `--self-test` succeed (including PyObjC capture stack);
- `codesign --verify --deep --strict` succeeds (ad-hoc by default);
- every Mach-O has an `arm64` slice and no absolute build-host library paths;
- the DMG mounts, contains `OpenWhisper.app` plus an `/Applications` symlink,
  and the nested app passes strict code-signature verification and reports the
  release version.

The result is `installer/Output/OpenWhisper-<version>-macos-arm64.dmg`.
`--skip-dmg` stops after app-bundle verification. Optional future signing uses
`OPENWHISPER_MACOS_CODESIGN_IDENTITY`; the first public path does **not**
require Apple credentials and does **not** notarize.

Do not gate the unnotarized preview on `spctl` success. Gatekeeper is expected
to warn until the user chooses **Open Anyway**.

## Soak the Windows native-update path

Nothing needs to be published. `OPENWHISPER_UPDATE_FEED_URL` points an installed
copy at a stand-in for GitHub `/releases/latest`, and
`scripts/serve_update_feed.py` serves the Windows build from
`installer/Output` with the same names, sizes, and SHA-256 digests.

1. Install the release users will update **from** (normally previous stable).
2. Build this release, then run `python scripts/serve_update_feed.py`.
3. Start the installed app with the override:
   ```powershell
   $env:OPENWHISPER_UPDATE_FEED_URL = "http://127.0.0.1:8765/releases/latest"
   & "$env:LOCALAPPDATA\Programs\OpenWhisper\OpenWhisper.exe"
   ```
4. Help → Check for Updates → Update. The app must return on the new version and
   `%LOCALAPPDATA%\OpenWhisper\updater.log` must end with
   `Update to <version> is healthy`.
5. Check Add/Remove Programs reports the new version, then launch once without
   the override.

The soak exercises the updater in the version being updated **from**. It cannot
test a fix to that old updater; use the setup-only rule below when repairing
the update path.

## Smoke-test the Linux packages

Use a clean Ubuntu 22.04 or Debian 12 VM and a clean Arch Linux VM or
container, each with a normal desktop session. Do not only extract the
packages or test on the build host.

1. Install with `sudo apt install ./OpenWhisper-<version>-linux-amd64.deb`.
   Confirm APT resolves dependencies with no manual Python/pip step.
2. Confirm `dpkg-query -W openwhisper` reports `<version>` and package-owned
   files are under `/usr/lib/openwhisper`, `/usr/bin`, and `/usr/share`.
3. Launch from the application menu, `openwhisper`, and `ow`. Confirm all three
   identities open the same app and the taskbar associates the window with the
   desktop entry.
4. In an X11 session, record and transcribe a short microphone sample, copy it
   to the clipboard, exercise one global hotkey, and verify auto-paste. In a
   native Wayland application, confirm the documented degraded behavior:
   recording/transcription and explicit clipboard copy work, while global
   hotkeys and synthetic paste are not promised across compositor-owned
   windows. Check `~/.local/share/OpenWhisper/openwhisper.log` for loader/import
   errors.
5. On a desktop without a legacy tray (stock GNOME is the important case),
   confirm tray controls are disabled and closing the window exits instead of
   hiding an unrecoverable process.
6. Install the new package over the previous stable package. Confirm settings,
   history, recordings, and cached models remain and the About dialog shows
   `Linux package` plus the new version.
7. `sudo apt remove openwhisper`, then confirm package files are gone while
   `${XDG_DATA_HOME:-~/.local/share}/OpenWhisper` remains. User data is removed
   only when the user deletes it explicitly; never remove the shared Hugging
   Face cache.

On Arch, repeat the same lifecycle with
`sudo pacman -U ./OpenWhisper-<version>-linux-x86_64.pkg.tar.zst`,
`pacman -Q openwhisper` reporting `<version>-1`, and
`sudo pacman -R openwhisper` for removal.

The Linux packages are CPU-only and package-manager-owned. Help → Check for
Updates is intentionally notify-only; it must open the release page rather than
trying to overwrite `/usr/lib/openwhisper`.

## Smoke-test the macOS DMG

Use a clean Apple Silicon Mac on macOS 14+ that did not build the artifact. Do
not only open the DMG on the CI host.

1. Download the DMG from the draft release (or copy the workflow artifact) so
   quarantine attaches as it would for end users.
2. Open the DMG, drag OpenWhisper into Applications, and launch it once.
3. When Gatekeeper blocks the unnotarized app, use **System Settings → Privacy
   & Security → Open Anyway**, then confirm **Open**. Do not disable Gatekeeper
   and do not strip quarantine attributes as the install procedure.
4. Confirm About shows the release version and channel label
   `macOS application`.
5. Record and transcribe a short microphone sample. Grant Microphone when
   prompted.
6. Exercise a Carbon hotkey while another app is focused, enable auto-paste,
   and grant Accessibility to the OpenWhisper app identity if needed.
7. Start a Meeting Mode session with something playing and grant Screen &
   System Audio Recording; confirm system audio appears or the documented
   mic-only degradation is honest. Optional: run
   `scripts/probe_macos_loopback.py` from a source checkout on the same machine
   when debugging capture.
8. Save an API key under Settings → API keys and confirm Keychain storage.
9. Help → Check for Updates must be notify-only (no in-app replace).
10. Quit, replace Applications/OpenWhisper.app with a newer DMG build, and
    confirm `~/Library/Application Support/OpenWhisper` settings/history remain
    and TCC grants still attach to `tech.fiorilabs.openwhisper`.

The macOS DMG is CPU-only and unnotarized. Intel Macs are out of scope for this
artifact.

## Validate optional Windows speech backends

For a release containing the backend expansion, use a fresh Windows x64 installation and the runtime/model downloads offered by that build. Source-worker tests alone do not validate the frozen distribution.

- Confirm the bundle contains `services/local_asr` worker source and JSON manifests, while optional PyTorch/Qwen/Moonshine SDKs and weights remain outside the base installer.
- Install NVIDIA Speech CPU/GPU, Qwen3-ASR runtime, and Moonshine runtime through Downloads. Verify their version, hashes, disk checks, cancel/retry behavior, and removal/reinstallation.
- Download and transcribe with all six new model choices: Parakeet v3, Qwen 0.6B/1.7B, Nemotron 3.5, and Moonshine Small/Medium. Check actual CPU and NVIDIA devices where supported; Moonshine remains CPU-only.
- Exercise dictation and Upload File, long audio, silence, canceled inference, backend switches, missing runtimes, and a restart with models cached and networking unavailable. Keep the model-download policy and hard offline override effective.
- Run meetings with Parakeet, Nemotron, and Moonshine; verify native previews for the latter two, final flush, durable transcript integrity, and engine restoration after stopping or failure. Qwen must not appear as a meeting choice.
- Check current Whisper/API settings survive upgrade, and on-demand/meeting model selections remain independent. Verify CPU/NVIDIA family preferences are accurately displayed.
- Keep the full model reference, setup guide, third-party notices, About description, issue form, and next release notes aligned with the packaged capabilities. Remove the version-specific Unreleased notices only when compatible installers are available. Do not add these features to old 2.5.2 release notes.

The checked-in source benchmarks are evidence from one Windows machine, not proof of frozen packaging or broad accuracy. Preserve their hardware/sample limitations in release messaging.

## Draft, then publish

Before publishing, confirm:

- tag and all filenames use the exact same version;
- `BUILD-METADATA.json` names the commit targeted by the release tag and links
  to the build's GitHub Actions run;
- all five required application artifacts are present (subject only to the
  documented Windows setup-only exception for `win64.tar.xz`);
- sizes match the combined workflow artifact;
- GitHub digest fields match `SHA256SUMS.txt`;
- Windows fresh install, native update, elevated/Program Files fallback, and
  uninstall have been smoked;
- Linux fresh install, desktop launch, upgrade, no-tray behavior, and removal
  have been smoked;
- macOS DMG fresh install via Open Anyway, microphone, hotkeys/auto-paste,
  Meeting Mode permission path, Keychain, and manual app replacement have been
  smoked on clean Apple Silicon hardware;
- release notes describe the supported Linux scope: amd64 Debian 12+/Ubuntu
  22.04+ and Arch, CPU-only, manual APT/dpkg or `pacman -U` upgrades, optional
  tray integration, and the X11 requirement for reliable global
  hotkeys/auto-paste;
- release notes describe the macOS preview scope: Apple Silicon, macOS 14+,
  unnotarized DMG, Open Anyway, CPU-only, notify-only updates, no Homebrew
  cask in the official tap yet.

Only then publish the draft. Do not publish stable assets piecemeal.

## Windows apply rules

- Native apply is only for a validated HKCU Inno install whose
  `InstallLocation` matches the running exe and already contains
  `OpenWhisperUpdater.exe`.
- Missing archive/digest, HKLM/Program Files, or an unsupported manifest uses
  the setup exe.
- A setup-only release is the emergency switch that disables native apply.
- Linux and macOS remain notify-only; never route package-manager-owned files
  through the Windows directory-swap helper.

## Fixing the Windows update path

Download, prepare, handoff, and commit run from the version being updated
**from**. A fix therefore takes effect one release later than it ships. For a
bug that can leave the install broken or the app stuck:

1. Publish the fix without `OpenWhisper-<version>-win64.tar.xz` so affected
   builds must use setup.
2. If necessary, remove the archive from the release those builds are currently
   offered. Do this only for a destructive/stuck defect; a clean rollback is
   worth retaining for diagnostics.
3. Rely on Inno `CloseApplications=force` plus `CloseRunningApp` to replace the
   affected build.
4. Continue shipping the Linux `.deb` / `.pkg.tar.zst` and the macOS DMG
   normally; they do not use this path.
5. Resume the full five-artifact set (including the Windows archive) in the
   following release after soaking `fixed → next` locally.

Historical examples are 2.4.2 (handoff did not exit), 2.4.4 (commit did not wait
for file locks), and 2.4.6 (helper inherited the locked install working
directory).
