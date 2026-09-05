# Packaging and downloadable components

Native installer commands, component pins, and the recipes that refresh GPU and meeting-agent payloads. User-facing install steps stay in the [README](../README.md#installation). Release publication steps stay in the [release checklist](release-checklist.md).

Existing component URLs live in [`services/components.py`](../services/components.py); additional speech runtimes and models are pinned in [`services/local_asr/`](../services/local_asr/). Every archive is SHA-256 verified before extraction. No new speech runtime is added to the base application dependencies.

## Building native installers

**Windows** (PowerShell):

```powershell
.\venv\Scripts\activate
pip install -r requirements.txt -r requirements-build.txt -c requirements-release-constraints.txt
winget install -e --id JRSoftware.InnoSetup
.\scripts\build_installer.ps1 -Clean
```

This creates `OpenWhisper-Setup-<version>.exe` and the Windows in-app update payload `OpenWhisper-<version>-win64.tar.xz` under `installer\Output\`.

**Linux** (x86-64 Debian/Ubuntu; build on Ubuntu 22.04 for the release compatibility floor):

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

This creates `OpenWhisper-<version>-linux-amd64.deb` and `OpenWhisper-<version>-linux-x86_64.pkg.tar.zst` from the same frozen tree, validates the desktop metadata and package control fields, checks every bundled ELF for unresolved libraries, and runs the frozen import self-test under Xvfb. The script prints each package SHA-256.

**macOS** (Apple Silicon host only):

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt -r requirements-build.txt -c requirements-release-constraints.txt
./scripts/build_installer_macos.sh --clean
```

This freezes `OpenWhisper.app`, verifies Info.plist identity and privacy keys, runs `--version` / `--self-test`, checks the ad-hoc code signature and arm64 Mach-O slices, and writes `OpenWhisper-<version>-macos-arm64.dmg` under `installer/Output/`. The build host must be Darwin arm64; Intel Macs are rejected. The DMG is intentionally unnotarized until a Developer ID pipeline is added.

The **Build native installers** GitHub Actions workflow builds Windows, Linux, and macOS in parallel and produces one combined release-candidate artifact; an optional workflow-dispatch input uploads all five native files plus `SHA256SUMS.txt` to an existing draft release. Native release builds use the reviewed exact versions in [`requirements-release-constraints.txt`](../requirements-release-constraints.txt), while ordinary source installs retain the compatible ranges in `requirements.txt`. See [`OpenWhisper.spec`](../OpenWhisper.spec) for the shared frozen-bundle definition and the [release checklist](release-checklist.md) before publishing.

## Component pins

| What | Where the bytes come from | Moves when |
| --- | --- | --- |
| Windows installer (`OpenWhisper-Setup-*.exe`) | Latest GitHub Release | Every app release |
| Windows native update archive (`OpenWhisper-*-win64.tar.xz`) | Same GitHub Release | Every app release (in-app apply) |
| Linux Debian package (`OpenWhisper-*-linux-amd64.deb`) | Same GitHub Release | Every app release (APT/dpkg upgrade) |
| Linux Arch package (`OpenWhisper-*-linux-x86_64.pkg.tar.zst`) | Same GitHub Release | Every app release (`pacman -U` upgrade) |
| macOS Apple Silicon DMG (`OpenWhisper-*-macos-arm64.dmg`) | Same GitHub Release | Every app release (manual replace; unnotarized preview) |
| `gpu-accel` | PyPI NVIDIA wheels | The CUDA pin in `requirements-gpu.txt` changes |
| `meeting-agent` | nodejs.org + a zip on the GitHub tag in `MEETING_AGENT_RELEASE_TAG` | Node or the sidecar bundle changes |
| `speaker-id` | Unpublished placeholder | — |
| `asr-nvidia-cpu` / `asr-nvidia-cuda` | python.org + NVIDIA NeMo-Speech.cpp release archives | Pinned native runtime changes; shared by Parakeet/Nemotron |
| `asr-qwen` | python.org + pinned PyPI/PyTorch wheels | Qwen runtime or its dependency closure changes |
| `asr-moonshine` | python.org + pinned PyPI wheels | Moonshine native runtime changes |
| Additional speech model weights | Pinned Hugging Face revisions; Moonshine model host | Model artifact manifest changes |

An app patch updates all five application artifacts. Existing component pins stay put so Downloads keep working.

For optional speech runtime updates, model additions, offline checks, and catalog synchronization, follow [Contributing](../CONTRIBUTING.md#local-speech-backends-and-models). These runtimes have independent versions; changing Whisper CUDA pins does not update them.

## Updating the GPU component

To move to newer CUDA wheels, bump the pins in `requirements-gpu.txt`, then:

```powershell
python scripts\build_component.py gpu-accel
```

That resolves what pip would install for `win_amd64`, records the SHA-256 pip reports for each wheel, downloads them to measure the extracted payload, and prints a ready-to-paste `_BUILTIN_GPU_ARCHIVES` block plus the matching `install_bytes`. Paste both into `services/components.py` and bump `GPU_COMPONENT_VERSION` so existing installs are offered the new payload.

Two things not to skip: `install_bytes` drives the pre-install free-space check, so it must be the measured value the script prints rather than an estimate; and `GPU_COMPONENT_VERSION` is what the Model Manager compares against an installed manifest, so leaving it unchanged means nobody is told an update exists.

## Updating the meeting-agent component

Leave the catalog alone when only the app changed. Rebuild when Node or `sidecar/` changes:

```powershell
python scripts\build_component.py meeting-agent
```

That pins official Node archives for Windows x64, Linux x64, and Linux ARM64 from nodejs.org, builds `sidecar/dist/bundle.cjs` once, zips the platform-neutral bundle, and prints a ready-to-paste `_BUILTIN_MEETING_AGENT_BY_PLATFORM` block covering `win_amd64`, `linux_x86_64`, and `linux_aarch64`. Attach the bundle zip to the GitHub Release whose tag you set as `MEETING_AGENT_RELEASE_TAG`, paste the block, and bump `MEETING_AGENT_COMPONENT_VERSION` so existing installs are offered the new payload. Do not replace the zip on an older tag — GitHub assets are treated as immutable.
