# Linux system audio for Meeting Mode

OpenWhisper can capture the **default output mix** on Linux through PulseAudio
monitor sources. That covers:

- native PulseAudio
- PipeWire with the `pipewire-pulse` compatibility service

Public Meeting Mode promotion for Linux stays **gated** until the manual
x86_64/aarch64 hardware release matrix is attested. On a current build you can
still try the path after the unsupported-platform acknowledgement; the app will
run the same diagnostics and remediation dialog.

ALSA-only desktops are supported by installing a Pulse-compatible session, not
by a separate ALSA loopback backend.

## Diagnostic keys

Every readiness failure carries a stable key. The in-app dialog and this guide
use the same keys.

| Key | Meaning | Typical fix |
| --- | --- | --- |
| `soundcard_missing` | Python `soundcard` package not importable | Install into the app venv |
| `libpulse_missing` | Pulse client library (`libpulse.so.0`) missing | Install distro Pulse client package |
| `audio_server_unavailable` | No Pulse-compatible server answered | Install/start Pulse or PipeWire-Pulse |
| `pipewire_pulse_missing` | PipeWire is up without Pulse compatibility | Install/enable `pipewire-pulse` |
| `default_sink_missing` | No default output sink | Choose a default output in desktop settings |
| `monitor_source_missing` | Default sink has no validated loopback monitor | Confirm monitor source; do not swap stacks casually |
| `monitor_open_failed` | Monitor found but open/read timed out or failed | Close exclusive apps; restart user audio services |
| `unsupported_architecture` | Not x86_64/amd64 or aarch64/arm64 | Unsupported |
| `unknown_failure` | Unexpected probe error | See verification commands; open an issue with logs |

## Quick checks

```bash
pactl info
pactl list short sinks
pactl list short sources
python scripts/probe_linux_loopback.py
```

A healthy setup shows a Pulse or PipeWire server name, a default sink, and a
matching monitor source. Silence alone is not a failure.

## Stack-aware fixes

### `soundcard_missing`

```bash
python -m pip install 'soundcard>=0.4.3'
python -c "import soundcard; print(soundcard.__version__)"
```

### `libpulse_missing`

- Debian/Ubuntu: `sudo apt install -y libpulse0`
- Fedora/RHEL: `sudo dnf install -y pulseaudio-libs`
- Arch: `sudo pacman -S --needed libpulse`

Verify:

```bash
python -c "import ctypes; ctypes.CDLL('libpulse.so.0')"
```

### `pipewire_pulse_missing` (PipeWire without Pulse compatibility)

Only when the probe classified the stack as PipeWire without Pulse:

- Debian/Ubuntu: `sudo apt install -y pipewire-pulse wireplumber`
- Fedora/RHEL: `sudo dnf install -y pipewire-pulseaudio wireplumber`
- Arch: `sudo pacman -S --needed pipewire-pulse wireplumber`

Then:

```bash
systemctl --user enable --now pipewire pipewire-pulse wireplumber
systemctl --user is-active pipewire-pulse
```

Sign out and back in if the services do not appear immediately.

Rollback:

```bash
systemctl --user disable --now pipewire-pulse
```

### `audio_server_unavailable` (no Pulse-compatible server answered)

When nothing answered, recover the stack you already use first. A temporarily
stopped native PulseAudio session must **not** be “fixed” by installing
PipeWire.

```bash
systemctl --user status pulseaudio pipewire pipewire-pulse wireplumber
systemctl --user is-active pulseaudio
systemctl --user is-active pipewire-pulse
pactl info
pactl list short sinks
pactl list short sources
```

After you identify the installed stack, restart **only that one** (do not chain
Pulse into PipeWire with `||`):

```bash
# Native PulseAudio only:
systemctl --user restart pulseaudio

# OR PipeWire + pipewire-pulse only:
systemctl --user restart pipewire pipewire-pulse wireplumber
```

Only when you have confirmed no Pulse-compatible server is installed at all
(for example a fresh ALSA-only image) should you install one deliberately:

- Debian/Ubuntu native Pulse: `sudo apt install -y pulseaudio`
- or PipeWire with compatibility: `sudo apt install -y pipewire pipewire-pulse wireplumber`
- Fedora/RHEL: `sudo dnf install -y pipewire pipewire-pulseaudio wireplumber`
- Arch: `sudo pacman -S --needed pipewire pipewire-pulse wireplumber`

Prefer restarting an existing Pulse or PipeWire user session before replacing
packages.

### Transient sink / monitor / open failures on a live stack

Keys: `default_sink_missing`, `monitor_source_missing`, `monitor_open_failed`.

These do **not** mean you should install a competing audio stack.

1. Choose a default output in desktop sound settings.
2. Play a short sound to wake the device.
3. Confirm monitors:

```bash
pactl info
pactl list short sinks
pactl list short sources
```

4. If the stack is already PipeWire-Pulse:

```bash
systemctl --user is-active pipewire pipewire-pulse wireplumber
```

5. If the stack is native PulseAudio, restart or inspect Pulse only — do not
   enable PipeWire just because a sink was missing.

6. Retry detection in OpenWhisper. Sign out/in only if a service restart was
   required for the monitor to appear.

### `unsupported_architecture`

Meeting Mode’s Linux path covers x86_64/amd64 and aarch64/arm64 only.

## Verification after any fix

```bash
pactl info | grep -i 'Server Name'
pactl list short sinks
pactl list short sources
python scripts/probe_linux_loopback.py
```

`probe_linux_loopback.py` exits 0 only when dual-channel capture is ready.

## Distro caveats

- Unknown package families never receive automatic `apt`/`dnf`/`pacman` install
  lines; use the generic guidance and your distro docs.
- Flatpak/sandboxed installs are out of scope for this guide.
- NVIDIA CUDA on Linux is separate (`requirements-gpu.txt`) and is not required
  for Meeting Mode capture.
- ARM64 Meeting Mode is functional (CPU/cloud transcription) once attestation
  passes; CUDA is not promised on ARM64.

## Notes

- OpenWhisper never runs package or service commands for you.
- A microphone-only choice applies only to the current meeting and is not
  persisted.
- Public “supported on Linux” marketing stays off until the hardware release
  matrix is attested, even though the implementation is present.
