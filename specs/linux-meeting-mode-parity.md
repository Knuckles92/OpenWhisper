# Linux Meeting Mode — Windows feature parity

Status: approved implementation plan. No application installer or Linux package work is in scope.

## Goal

A Linux user can use Meeting Mode with the same product behavior as a Windows user:

- capture the microphone as **Me**;
- capture the complete default system-output mix as **Others**;
- run live ASR, speaker identification, intelligence, dashboard, recovery, finalization, history, playback, and export;
- use either the Direct intelligence core or a self-contained Pi sidecar installed from Downloads;
- follow default-output changes and recover failed capture sources;
- deliberately continue microphone-only when system audio cannot be captured.

Linux must no longer be presented as an unsupported Meeting Mode platform once the release matrix below passes.

## Resolved decisions

1. **Parity means Windows behavior, not Linux-specific expansion.** Capture the entire default output mix. Per-application capture and an output-device picker are out of scope.
2. **Both Linux architectures ship together.** Support x86_64 and ARM64/aarch64 in the same public support change.
3. **Use one supported system-audio contract.** The production backend captures PulseAudio monitor sources. This covers native PulseAudio and PipeWire through `pipewire-pulse`. An ALSA-only starting system is supported through guided installation of a Pulse-compatible PipeWire session, not through a separate native ALSA loopback backend.
4. **Missing system audio is never silent.** Before each affected meeting, show a diagnostic dialog with the detected problem, distro-specific guidance, Copy command, Open setup guide, Retry detection, Go back, and Continue microphone-only. The microphone-only decision is not persisted.
5. **The easiest Pi path wins.** Downloads offers self-contained Pi-sidecar payloads for Linux x86_64 and ARM64; users do not need to install Node or build `sidecar/` manually.
6. **ARM64 parity is functional, not performance parity.** CPU-local and cloud transcription are supported. NVIDIA/CUDA acceleration is not guaranteed on ARM64.
7. **The public support flip is atomic.** Implementation may land in phases, but Linux remains labeled unsupported until capture, remediation, both architectures, and the sidecar pass their release gates.
8. **No installer work.** Do not modify PyInstaller/Inno, create AppImage/deb/rpm packages, add native updater support, or change desktop integration.

## Current state

Most Meeting Mode code is already platform-neutral:

| Area | Current Linux state |
| --- | --- |
| Microphone capture | Implemented through `sounddevice` / PortAudio |
| Spooling, ASR, persistence, recovery | Implemented |
| Dashboard, LAN sharing, history, playback, export | Implemented |
| Direct intelligence core | Implemented |
| Speaker identification and finalization | Implemented with existing degradation paths |
| NVIDIA local ASR | Available on supported x86_64 setups through `requirements-gpu.txt` |
| System audio | Missing from a stock Linux installation |
| Platform UX | Hard-coded unsupported |
| Pi-sidecar download | Windows-only |

The principal blockers are deliberate policy and distribution choices rather than a separate Linux meeting engine:

- `meeting/platform.py:45-56` supports only Windows and macOS 13+.
- `requirements.txt:32` installs `soundcard` only on Windows.
- `meeting/capture/soundcard_stream.py` has a cross-platform shape but `available()` only verifies importability.
- `ui_qt/widgets/tabbed_content.py`, `ui_qt/dialogs/meeting_unsupported_dialog.py`, and `ui_qt/widgets/meeting_mode_tab.py` encode Linux as unsupported.
- `services/components.py:281-299` hides every downloadable component off Windows, and the meeting-agent catalog contains only `win_amd64` payloads.
- `meeting/agent/pi_sidecar.py:68` assumes a portable runtime named `node.exe`.

## Support contract

### Supported capture environments

- Linux x86_64 or aarch64/ARM64.
- A working desktop audio session exposing PulseAudio monitor sources through either:
  - native PulseAudio; or
  - PipeWire plus `pipewire-pulse`.
- Source/venv application installation using the repository's documented dependency flow.

### Supported through remediation

A user beginning with ALSA-only audio, PipeWire without Pulse compatibility, missing `libpulse`, or no active user audio server receives detected, package-family-specific setup instructions. The app does not silently claim capture is ready and does not modify system packages, kernel modules, services, or user configuration itself.

### Explicitly out of scope

- A native ALSA `snd-aloop` backend. It requires kernel-module and output-routing configuration and would not provide a zero-configuration equivalent to the Windows default-output mix.
- A second native PipeWire capture implementation while `pipewire-pulse` provides the supported monitor API.
- Per-application capture.
- Output-device selection separate from the OS default.
- Flatpak portals or sandbox policy.
- Linux installer/package/updater work.
- Guaranteed ARM64 GPU acceleration.

## Architecture

### 1. Linux system-audio capability probe

Add a capture-layer probe, recommended location:

- `meeting/capture/linux_audio.py`

It should return structured data rather than a bare boolean. Suggested result fields:

```python
LinuxAudioCapability(
    ready: bool,
    reason: str,
    server_kind: str,       # pulse | pipewire-pulse | unavailable | unknown
    default_sink: str,
    monitor_source: str,
    package_family: str,    # apt | dnf | pacman
    remediation_key: str,
)
```

Required failure classifications:

- `soundcard_missing`
- `libpulse_missing`
- `audio_server_unavailable`
- `pipewire_pulse_missing`
- `default_sink_missing`
- `monitor_source_missing`
- `monitor_open_failed`
- `unsupported_architecture`
- `unknown_failure`

The probe must:

1. remain safe to call from the meeting start worker;
2. never raise into the UI;
3. resolve the current default speaker and its monitor;
4. verify the selected source is actually loopback/monitor capture;
5. report actionable evidence without logging credentials or unrelated environment data;
6. be reusable by the pre-start dialog, the engine, and a diagnostic script.

Do not use `sounddevice` device-name matching as the primary Linux path. Linux PortAudio installations commonly expose ALSA devices without Pulse monitor sources. Use SoundCard's PulseAudio backend and explicitly validate its monitor selection.

### 2. Production loopback source

Harden `meeting/capture/soundcard_stream.py` as the shared Windows/Linux SoundCard source:

- update module and class documentation so it is no longer described as Windows-only;
- make capability detection resolve and validate the default speaker monitor instead of checking only `import soundcard`;
- keep Windows COM initialization as a Windows-only no-op elsewhere;
- log the selected backend, default sink, monitor, sample rate, and channels;
- retain bounded startup and shutdown waits, with a Linux-appropriate startup bound established by the proof phase;
- preserve non-fatal behavior: a failed loopback source must not prevent microphone capture;
- continue using mono int16 `CaptureBlock` values so the existing spool/resampler remains unchanged;
- retain `is_default_device_current()` and verify it correctly detects Linux default-sink changes;
- ensure an open-but-silent/wrong source is not reported as healthy during the initial proof window.

The engine's existing fallback chain can remain platform-neutral:

```text
WASAPI sounddevice → ScreenCaptureKit → SoundCard monitor
```

Each backend rules itself out through capability detection. Update comments in:

- `meeting/engine.py`
- `meeting/capture/__init__.py`
- `meeting/capture/devices.py`

The watchdog already polls every two seconds and throttles retries to ten seconds. A usable replacement source should be restored within twelve seconds of becoming available.

### 3. Pre-start Linux readiness flow

Refactor `ui_qt/ui_controller.py:876+` so meeting readiness is not represented by one unsupported-platform acknowledgement.

The common start flow for tab, button, demo, tray, and hotkey entry points should perform:

1. unsupported-OS policy check;
2. macOS Screen Recording check when applicable;
3. Linux system-audio capability check when applicable;
4. start dual-channel capture when ready;
5. otherwise show the Linux diagnostic/remediation dialog;
6. proceed microphone-only only after an explicit decision for that meeting.

Add a dialog such as:

- `ui_qt/dialogs/meeting_linux_audio_dialog.py`

Required states and actions:

- concise detected problem;
- what Meeting Mode will miss without the fix;
- detected distro/package family and audio stack;
- a copyable command or bounded sequence of commands;
- **Copy command**;
- **Open setup guide**;
- **Retry detection** without closing the app;
- **Continue microphone-only**;
- **Go back**.

The dialog must not execute `sudo`, install packages, load kernel modules, replace the audio server, or edit user configuration. Commands are advice and must be reviewed and tested per distro family before release.

The existing `MEETING_UNSUPPORTED_PLATFORM_ACK` setting remains for genuinely unsupported systems and old macOS. Linux no longer reads or writes it. The microphone-only choice is session-scoped and never persisted.

If system audio disappears after capture has started, do not interrupt the meeting with a modal. Keep the existing persistent capture status, retry through the watchdog, and report restoration when it succeeds.

### 4. Remediation data and documentation

Extend the existing Linux package-family detection in `services/linux_deps.py`; do not create a second distro detector.

Maintain remediation as structured data keyed by failure and package family. At minimum cover:

- Debian/Ubuntu (`apt`)
- Fedora/RHEL-family (`dnf`)
- Arch-family (`pacman`)

The commands must account for these distinct cases:

- `libpulse` missing while a compatible server already exists;
- PipeWire running without `pipewire-pulse`;
- ALSA-only desktop requiring PipeWire, Pulse compatibility, and an appropriate session manager;
- packages installed but the user service not active;
- a restart or sign-out required before retry can succeed.

Keep the in-app explanation usable offline. **Open setup guide** should lead to a detailed README/docs page with the same diagnostic keys, verification commands, rollback notes, and distro caveats. The website must not be the sole source of recovery instructions.

### 5. Supported-platform policy and copy

Only after the capture and remediation gates pass, update `meeting/platform.py`:

- Windows: supported;
- Linux x86_64 and aarch64: supported;
- macOS 13+: supported;
- old macOS and unknown platforms: unsupported.

Supported means the OS has a maintained first-class capture path; it does not mean every machine currently has a usable output device. This matches Windows, which remains supported when no loopback device can be opened.

Update:

- `ui_qt/widgets/tabbed_content.py` — Linux tab is not greyed or locked;
- `ui_qt/dialogs/meeting_unsupported_dialog.py` — remove Linux-specific wording and behavior;
- `ui_qt/widgets/meeting_mode_tab.py` — describe PulseAudio/PipeWire monitor capture and mic-only degradation honestly;
- `ui_qt/ui_controller.py` — route Linux through readiness/remediation, not unsupported acknowledgement;
- `services/settings.py` docstrings concerning the unsupported acknowledgement;
- `README.md` platform feature bullet and comparison table;
- `CHANGELOG.md`.

Recommended idle copy:

> System audio uses the default PulseAudio/PipeWire output monitor. If it is unavailable, OpenWhisper will explain how to enable it before offering a microphone-only meeting.

### 6. ARM64 dependency readiness

Before making the support claim, verify the complete source-install dependency graph on aarch64, not only Meeting Mode's own Python files.

The current ecosystem has aarch64 builds for key native dependencies such as CTranslate2, PyQt6, ONNX Runtime, PyAV, and tokenizers, but wheel tags impose glibc and Python-version floors. Establish and document the actual minimum supported distro/Python combination by resolving `requirements.txt` in a clean ARM64 environment.

Required outcomes:

- clean `pip install -r requirements.txt` on the ARM64 release baseline;
- PyQt application startup;
- CPU faster-whisper model load and transcription;
- microphone and monitor capture;
- local speaker embedding when ONNX Runtime is available;
- honest degradation if an optional accelerator is unavailable;
- no CUDA promise on ARM64.

Do not infer ARM64 support from pure-Python imports or from Node availability alone.

## Pi-sidecar parity

The Downloads component is application functionality and remains in scope even though application installer work is excluded.

### Platform-aware component catalog

Refactor `services/components.py` so component availability and compatibility use the current platform/architecture rather than a global Windows-only catalog.

Add normalized tags:

- `win_amd64`
- `linux_x86_64`
- `linux_aarch64`

Expected component availability:

| Component | Windows x64 | Linux x64 | Linux ARM64 |
| --- | --- | --- | --- |
| GPU acceleration component | Existing | Hidden; retain `requirements-gpu.txt` flow | Hidden |
| Meeting Intelligence Agent | Available | Available | Available |
| Speaker-ID component | Preserve existing publication policy | Preserve existing publication policy | Preserve existing publication policy |

`check_compatibility()` must compare the manifest with the normalized current platform. A Windows payload must never be considered compatible on Linux, and an x64 Linux payload must never be considered compatible on ARM64.

### Self-contained Node payloads

Update `scripts/build_component.py` to generate/pin meeting-agent entries for both Linux architectures while preserving the existing Windows output.

Use official Node 22 archives:

- Linux x64: `node-v<version>-linux-x64.tar.xz`
- Linux ARM64: `node-v<version>-linux-arm64.tar.xz`

The sidecar `bundle.cjs` is platform-independent and should be built once per component version. It may be referenced by all platform entries if archive naming and immutable release pins remain clear.

Add a safe Node tar extractor in `services/components.py` that:

- selects only the Node executable;
- rejects absolute paths, traversal, links, and unexpected members;
- writes `node` into the component payload;
- applies executable permissions;
- reports bounded extraction progress;
- validates `node` and `bundle.cjs` before atomic commit.

Update `meeting/agent/pi_sidecar.py`:

- resolve `node.exe` on Windows;
- resolve the bundled executable `node` on Linux;
- retain `node` from `PATH` as the source-development fallback;
- preserve existing Windows `CREATE_NO_WINDOW` behavior only on Windows.

The Model Manager should enable **Pi (sidecar)** immediately after a successful Linux component install, matching Windows behavior.

## Work phases

### Phase 0 — Proof and facts

1. Add `scripts/probe_linux_loopback.py`.
2. Install/test SoundCard manually on representative PulseAudio and PipeWire-Pulse systems without changing the product support flag.
3. Measure monitor resolution, startup delay, block cadence, silence behavior, and mic/loopback alignment.
4. Resolve `requirements.txt` and start the app on clean ARM64.
5. Verify the exact remediation commands for apt, dnf, and pacman families.

**Gate:** SoundCard must reliably capture the default output monitor on the release matrix. If it fails, stop and redesign the backend before changing product policy.

### Phase 1 — Capture backend and dependency support

1. Enable `soundcard` on Linux in `requirements.txt`.
2. Add `libpulse.so.0` to `services/linux_deps.py` with package-family mappings.
3. Add the structured Linux capability probe.
4. Harden `SoundcardLoopbackSource` for Linux.
5. Wire actionable failure details into engine capture status.
6. Add capture unit and integration tests.

### Phase 2 — Readiness and remediation UX

1. Add the Linux audio diagnostic dialog.
2. Route every meeting start path through it.
3. Implement Copy command, Open setup guide, Retry detection, Continue microphone-only, and Go back.
4. Add offline guidance and detailed docs.
5. Test each failure classification and all dialog decisions.

### Phase 3 — Multi-architecture Pi component

1. Introduce normalized platform tags and platform-specific catalog entries.
2. Add Linux x64 and ARM64 Node payloads.
3. Add safe tar extraction and executable validation.
4. Make sidecar Node resolution platform-aware.
5. Expose the meeting-agent component in Downloads on Linux.
6. Test installation, activation, removal, update, and sidecar handshake on both architectures.

### Phase 4 — Platform promotion

1. Mark Linux x86_64 and aarch64 supported in policy.
2. Remove the unsupported Linux tab/dialog path.
3. Add supported Linux copy and runtime degradation messaging.
4. Flip tests that intentionally encode Linux as unsupported.
5. Update README and changelog.

This phase lands only after Phases 0–3 satisfy their gates.

### Phase 5 — Regression and release validation

1. Run all Python tests.
2. Run sidecar typecheck, tests, and build.
3. Run automated null-sink/monitor integration tests where possible.
4. Complete the manual hardware matrix.
5. Confirm no installer files changed.

## Tests

### New Linux capture tests

Recommended new module:

- `tests/test_meeting_linux_capture.py`

Cover:

- SoundCard missing;
- `libpulse` missing;
- no server;
- PipeWire without Pulse compatibility;
- no default sink;
- no monitor source;
- valid Pulse monitor;
- monitor source rejected when it is not loopback;
- recorder open failure;
- start timeout;
- mono/stereo conversion;
- timestamps and block duration;
- stop and restart;
- default-sink switch;
- watchdog restoration;
- mic-only continuation and cancellation.

### Existing tests to update

- `tests/test_meeting_unsupported_platform.py` — Linux architectures become supported; old macOS and unknown systems remain unsupported.
- `tests/test_meeting_mode_tab.py` — Linux copy and warning behavior.
- `tests/test_meeting_intro_dialog.py` — Linux opens normally.
- `tests/test_meeting_macos_capture.py` — move generic fallback-chain coverage into a platform-neutral module if appropriate.
- `tests/test_meeting_engine.py` — dual-channel Linux start, degradation, status, and recovery.
- `tests/test_components.py` — platform catalogs, compatibility, Linux extraction, component availability, and architecture mismatch.
- `tests/test_model_manager_dialog.py` — Pi core availability after Linux component installation.
- Pi-sidecar tests — Linux executable resolution, handshake, cancellation, restart, and shutdown.

### Diagnostic/remediation UI tests

Cover every failure classification and assert:

- correct short explanation;
- correct package-family guidance;
- Copy command payload;
- guide URL;
- Retry reruns the probe;
- ready-on-retry proceeds without another warning;
- Continue microphone-only applies only to the current meeting;
- Go back starts nothing;
- hotkey/tray starts cannot bypass the dialog.

### Commands

```bash
venv/bin/python -m pytest -q
npm --prefix sidecar ci
npm --prefix sidecar run typecheck
npm --prefix sidecar test
npm --prefix sidecar run build
```

The focused current baseline is 129 passed and 2 skipped across the Meeting Mode platform/capture suite. Those tests presently encode Linux as unsupported and must be changed only with the production support path.

## Manual release matrix

At minimum:

| Architecture | Environment | Required result |
| --- | --- | --- |
| x86_64 | Ubuntu-family, PipeWire-Pulse | Dual capture, recovery, Direct and Pi cores |
| x86_64 | Fedora-family, PipeWire-Pulse | Dual capture, recovery, Direct and Pi cores |
| x86_64 | Native PulseAudio | Dual capture and default-sink switch |
| x86_64 | ALSA-only starting state | Correct remediation; successful capture after supported setup |
| aarch64 | Ubuntu-family, PipeWire-Pulse | Clean install, CPU ASR, dual capture, Pi core |
| aarch64 | ALSA-only starting state | Correct remediation; successful capture after supported setup |

For each ready environment:

1. start with microphone and active system playback;
2. verify local speech is **Me** and playback/call audio is **Others**;
3. verify no crossed or duplicated channel assignment;
4. switch the default output mid-meeting;
5. unplug/reconnect USB or Bluetooth output;
6. verify restoration within twelve seconds after a usable output returns;
7. suspend/resume;
8. pause/resume the meeting;
9. finish, re-transcribe, identify speakers, generate reports, export, reopen, and play the recording;
10. run both Direct and Pi-sidecar intelligence;
11. run a 60-minute alignment test, targeting no more than 250 ms absolute channel offset and no more than 100 ms/hour drift;
12. verify intentional microphone-only continuation is prominently recorded in the capture status.

## Acceptance criteria

Linux Meeting Mode parity is complete only when all are true:

- Linux x86_64 and aarch64 open Meeting Mode without an unsupported-platform acknowledgement.
- A ready PulseAudio or PipeWire-Pulse environment captures microphone and default system output into separate durable channels.
- The resulting transcript and speaker behavior match the Windows Me/Others contract.
- Default-output changes and source failures recover through the watchdog.
- Missing prerequisites produce accurate, distro-specific, offline-usable remediation.
- A user must explicitly choose microphone-only for each affected meeting.
- Retry detection can proceed without restarting OpenWhisper when the audio session becomes ready.
- Downloads installs and activates a self-contained Pi sidecar on Linux x64 and ARM64.
- Direct-core fallback remains functional when the sidecar is absent.
- ARM64 completes local CPU transcription and the full meeting lifecycle.
- Windows and macOS Meeting Mode tests remain green.
- The complete Python and sidecar suites pass.
- The manual release matrix passes.
- No application installer, packaging, or updater files are changed.

## Risks and stop conditions

1. **Monitor matching is unreliable.** Do not ship a fuzzy name match that can silently record a physical microphone. Require positive monitor validation.
2. **Transport latency breaks channel ordering.** If SoundCard delivery timestamps exceed the alignment targets, fix timestamping or select a lower-level Pulse backend before promotion.
3. **Remediation is destructive or distro-incorrect.** Never execute package/audio-server changes automatically. A command must be verified on its target family before appearing in-app.
4. **ARM64 dependency floor is too narrow.** Publish the actual glibc/Python/distro minimum discovered by the clean install; do not claim older systems that cannot resolve native wheels.
5. **Component platform checks remain Windows-shaped.** Platform normalization and architecture mismatch tests are mandatory before offering Linux payloads.
6. **Partial delivery tempts an early copy change.** Keep Linux unsupported in product policy until every acceptance gate passes.

## Files expected to change

Capture and policy:

- `requirements.txt`
- `meeting/platform.py`
- `meeting/capture/__init__.py`
- `meeting/capture/devices.py`
- `meeting/capture/soundcard_stream.py`
- `meeting/capture/linux_audio.py` (new)
- `meeting/engine.py`
- `services/linux_deps.py`
- `services/settings.py`
- `services/runtime/meeting.py` if a session-scoped mic-only decision must enter engine options

UI and docs:

- `ui_qt/ui_controller.py`
- `ui_qt/widgets/tabbed_content.py`
- `ui_qt/widgets/meeting_mode_tab.py`
- `ui_qt/dialogs/meeting_unsupported_dialog.py`
- `ui_qt/dialogs/meeting_linux_audio_dialog.py` (new)
- `ui_qt/dialogs/__init__.py`
- `ui_qt/styles/theme.qss`
- `README.md`
- `CHANGELOG.md`
- detailed Linux system-audio setup documentation (new)

Pi component:

- `services/components.py`
- `scripts/build_component.py`
- `meeting/agent/pi_sidecar.py`
- `ui_qt/dialogs/model_manager_dialog.py` only where component availability copy needs adjustment

Diagnostics and tests:

- `scripts/probe_linux_loopback.py` (new)
- `tests/test_meeting_linux_capture.py` (new)
- existing platform, engine, component, Model Manager, and sidecar tests listed above

Files explicitly not in scope:

- `OpenWhisper.spec`
- `OpenWhisperUpdater.spec`
- `scripts/build_installer.ps1`
- `installer/**`
- updater implementation
- Linux package manifests or desktop bundles
