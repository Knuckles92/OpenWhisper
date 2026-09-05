# OpenWhisper

**Website:** [openwhisper.fiorilabs.tech](https://openwhisper.fiorilabs.tech/)

A cross-platform desktop app (Windows, macOS, Linux) for recording audio and transcribing it to text using local speech models or the OpenAI API. Features a modern PyQt6 GUI, system tray integration, global hotkeys, and auto-paste. The app detects your OS at runtime and adapts hotkey handling, auto-paste, and platform conventions automatically — see [Platform differences](#platform-differences).


<p align="center">
  <img width="680" alt="OpenWhisper Quick Record" src="docs/screenshots/01-quick-record-idle.png" />
</p>

<p align="center">
  <img width="360" alt="Waveform overlay cycling through recording, transcribing, copied, and live streaming preview" src="docs/screenshots/overlay-states.gif" />
</p>

<p align="center">
  <img width="735" alt="Model Manager: assign on-demand voice, text cleanup, and meeting models" src="docs/screenshots/01-model-manager-voice.png" />
</p>

<p align="center">
  <img width="960" alt="Downloads: speech model catalog and per-model technical profile" src="docs/screenshots/06-downloads-model-profile.png" />
</p>

<p align="center">
  <img width="720" alt="Settings: General destination with auto-paste, clipboard, tray, and updates" src="docs/screenshots/01-general.png" />
</p>

## Features

- **Global Hotkeys** – Start/stop recording from any app (customizable)
- **Auto-paste** – Dictated transcription automatically pastes to your active window (Upload File results stay in the app, with Copy buttons)
- **Audio Upload** – Import existing audio files for transcription
- **Meeting Mode (Windows, macOS 13+)** – Record microphone and system audio into a searchable meeting transcript; share a live dashboard, review evidence-linked insights, play the recording, and export. After local transcription finishes, **Continue in the background** starts another meeting while cleanup and reports finish. Linux system-audio capture is a [preview](docs/linux-system-audio.md).
- **AI Transcript Cleanup & Learned Rules** – Post-process transcripts with LLMs (OpenAI, OpenRouter, or custom OpenAI-compatible endpoints) and teach spelling and style rules by text or voice
- **Local and cloud engines** – Transcribe locally with **Whisper**, **Parakeet**, **Qwen3-ASR**, **Nemotron Streaming**, or **Moonshine**, or in the cloud with **GPT-Transcribe**, **GPT-4o Transcribe**, **GPT-4o Mini Transcribe**, or **Whisper**. [Setup and comparisons](docs/local-asr.md).
- **Model Manager** – Browse, download, and select local speech models, or pick OpenAI/OpenRouter cleanup models with a guided provider → model picker
- **Live preview** – While-you-speak preview where the engine supports it (Parakeet, Nemotron Streaming, and Whisper dictation; Nemotron and Moonshine in Meeting Mode on Windows x64)
- **Transcription History** – Search, retranscribe, and export Markdown, plain text, or JSON
- **Real-time Visualization** – Animated waveform overlay shows recording status
- **Smart Splitting (API)** – Large audio files split automatically to avoid API limits
- **Audio Device Selection** – Choose your preferred microphone input
- **System Tray** – Minimize to tray when the desktop session provides one
- **Window Memory** – Remembers window position and size between sessions

## Transcription backends and models

The published 2.5.2 installers include Local Whisper and the OpenAI API. Parakeet, Qwen3-ASR, Nemotron Streaming, and Moonshine are in this source tree and need a checkout until a release ships them.

| Backend | Model choices | Device support | Workflows |
| --- | --- | --- | --- |
| Local Whisper | auto, standard Whisper sizes, turbo, and Distil-Whisper | CPU; NVIDIA CUDA on Windows/Linux | Dictation, uploads, meetings; dictation preview |
| Parakeet | TDT 0.6B v3 | Windows x64 CPU / NVIDIA GPU | Dictation with preview, uploads, meeting chunks |
| Qwen3-ASR | 0.6B, 1.7B | Windows x64 CPU / NVIDIA GPU | Dictation and uploads |
| Nemotron Streaming | 3.5 ASR Streaming 0.6B | Windows x64 CPU / NVIDIA GPU | Dictation with preview, uploads, meetings with native preview |
| Moonshine | Streaming Small, Streaming Medium; English | Windows x64 CPU | Dictation, uploads, meetings with native preview |
| API | GPT-Transcribe, GPT-4o Transcribe, GPT-4o Mini Transcribe, Whisper | Cloud; API key and network | Dictation and uploads |

New installations start on **Parakeet** on Windows x64 and on **Local Whisper** elsewhere. A saved Backend choice is always kept. On a fresh Windows source install the app asks to download the Parakeet weights and then points to Downloads for its runtime.

See the [complete model reference](docs/models.md) for exact IDs and the [local backend guide](docs/local-asr.md) for downloads, licenses, runtime requirements, and measured speed/quality. Voice recognition is separate from text cleanup and meeting intelligence.

## Platform differences

The same codebase runs on all three platforms; a few behaviors adapt to the OS:

| Area | Windows | macOS | Linux |
|------|---------|-------|-------|
| Global hotkeys | `keyboard` library (per-key suppression) | Carbon `RegisterEventHotKey` (no Accessibility permission; falls back to [`pynput`](https://pypi.org/project/pynput/) if registration fails) | `pynput` (X11/XWayland; compositor-limited on Wayland) |
| Default hotkeys | Numpad (`*`, `-`, `Ctrl+Alt+*`) | Control+Option (`⌃⌥R`, `⌃⌥⎋`, `⌃⌥⇧R`) | Numpad (same as Windows) |
| Auto-paste | `Ctrl+V` | `Cmd+V` | `Ctrl+V` |
| GPU | NVIDIA CUDA — Whisper via Downloads component or source wheels; optional engines via their own runtimes | CPU only (no Metal/MPS) | NVIDIA CUDA — Whisper via source wheels; native packages are Whisper CPU |
| Meeting Mode system audio | WASAPI loopback, with a `soundcard` fallback | ScreenCaptureKit (macOS 13+), needs Screen Recording | Pulse/PipeWire preview; see [Linux system audio](docs/linux-system-audio.md) |
| Distribution / launchers | Native `.exe` installer | Apple Silicon `.dmg` preview (unnotarized) or source + `install.sh` | Native `.deb` / `.pkg.tar.zst` installers; `openwhisper` / `ow` commands |

> On Linux, `pynput` cannot swallow individual keys, so hotkey combinations also reach the focused app. Native Wayland blocks reliable global hotkeys and auto-paste; use an X11 session (or XWayland windows). Recording, transcription, clipboard copy, and the rest of the UI remain available on Wayland. On macOS, Carbon hotkeys do not require Accessibility; if registration fails, the app falls back to `pynput` and combos may leak. The Control+Option defaults avoid Spotlight, 1Password, and other common shortcuts.

## GPU acceleration

Whisper GPU needs an NVIDIA driver with CUDA 12 (525+). You do not need the CUDA Toolkit. Native packages ship Whisper on CPU; Windows can install GPU acceleration from **Downloads → Components**, and Linux uses a [source install](#install-from-source-all-platforms) plus `requirements-gpu.txt`. macOS stays on CPU. Optional Windows engines have their own [runtimes](docs/local-asr.md#download-and-disk-sizes).

Library loading, cuDNN, `auto` device selection, and older-GPU fallbacks: [Whisper GPU acceleration](docs/whisper-gpu.md).

## Installation

**Updates:** **Help → Check for Updates** reports new GitHub releases. Per-user Windows installs can apply a verified archive in-app (elevated / Program Files installs use the setup exe). Linux upgrades with the same `apt` / `pacman` command. macOS replaces the DMG. A source checkout is `git pull --ff-only` (and `pip install -r requirements.txt` if dependencies changed). Turn automatic checks or notifications off in **Settings → General**. Updating from 2.5.1 to 2.5.2 opens the Windows setup wizard once to install the repaired updater.

### Windows — installer (recommended)

Download **OpenWhisper-Setup-2.5.2.exe** from [openwhisper.fiorilabs.tech](https://openwhisper.fiorilabs.tech/) or the [Releases page](https://github.com/Knuckles92/OpenWhisper/releases), then run it.

- No Python, no admin rights, no UAC prompt — it installs per-user to `%LOCALAPPDATA%\Programs\OpenWhisper`.
- Settings, history, and recordings live in `%LOCALAPPDATA%\OpenWhisper` and are kept if you reinstall.
- The 2.5.2 installer prompts for Whisper Base (~150 MB) on first use. On this source tree, first-run size depends on the selected engine; see the [size table](docs/local-asr.md#download-and-disk-sizes).

> **SmartScreen warning:** the installer is not yet code-signed, so Windows shows *"Windows protected your PC"*. Click **More info → Run anyway**. Verify the download by comparing its SHA-256 against the checksum published next to the download link:
> ```powershell
> Get-FileHash .\OpenWhisper-Setup-2.5.2.exe -Algorithm SHA256
> ```

To uninstall, use *Settings → Apps → Installed apps*. You'll be asked whether to keep your settings and history.

### Linux — native packages (recommended)

Download **OpenWhisper-2.5.2-linux-amd64.deb** or **OpenWhisper-2.5.2-linux-x86_64.pkg.tar.zst** from the [Releases page](https://github.com/Knuckles92/OpenWhisper/releases), then install so audio and Qt system libraries are resolved automatically:

```bash
sudo apt install ./OpenWhisper-2.5.2-linux-amd64.deb
# or
sudo pacman -U ./OpenWhisper-2.5.2-linux-x86_64.pkg.tar.zst
```

- Debian 12+ / Ubuntu 22.04+ (`amd64`) or Arch and compatible derivatives (`x86_64`).
- No Python or virtual environment. The package installs under `/usr/lib/openwhisper`, adds a desktop-menu entry, and provides `openwhisper` and `ow`.
- Settings, history, recordings, and app-managed components live under `${XDG_DATA_HOME:-~/.local/share}/OpenWhisper`. Whisper models use the shared Hugging Face cache (`${HF_HOME:-~/.cache/huggingface}`); other local engines use the app data directory.
- Native packages ship Whisper on CPU. Use the source installation below for NVIDIA CUDA.
- Tray integration is used only when the desktop exposes a system tray. On native Wayland, use in-app controls and clipboard copy unless you switch to X11 for global hotkeys and auto-paste.

Verify a download against `SHA256SUMS.txt` from the same release:

```bash
sha256sum -c SHA256SUMS.txt --ignore-missing
```

Remove with `sudo apt remove openwhisper` or `sudo pacman -R openwhisper`. To also remove per-user data, delete `~/.local/share/OpenWhisper` (or `$XDG_DATA_HOME/OpenWhisper`). The shared Hugging Face cache is left in place; remove it only if you understand other applications may use it.

> Fedora, ARM64, and older-glibc systems are not targets of the official Linux packages; use the source installation below. Official packages are built on Ubuntu 22.04 and record their actual minimum glibc requirement.

### macOS — Apple Silicon DMG (preview)

Download **OpenWhisper-*-macos-arm64.dmg** from the [Releases page](https://github.com/Knuckles92/OpenWhisper/releases).

- **Apple Silicon only** (arm64). macOS **14 (Sonoma) or newer**.
- Open the disk image and drag **OpenWhisper** into **Applications**.
- The app inside the DMG is **ad-hoc signed**; the DMG is **not notarized**. The first launch is blocked by Gatekeeper until you approve it once:
  1. Open OpenWhisper from Applications (or double-click it in the DMG).
  2. When macOS reports that the developer cannot be verified, open **System Settings → Privacy & Security**.
  3. Find the OpenWhisper message near the bottom of the Security section and choose **Open Anyway**.
  4. Confirm **Open** when prompted. Subsequent launches use the same exception.
- Do **not** turn Gatekeeper off, and do **not** strip quarantine attributes as a normal install step. Apple’s Open Anyway flow is the supported override for an unnotarized build.
- Settings, history, and recordings live in `~/Library/Application Support/OpenWhisper` and survive replacing the app bundle.
- Transcription is CPU-only on macOS. Downloadable Windows/Linux components (GPU Acceleration, Meeting Agent) are not offered on Mac.
- Grant **Microphone**, and for Meeting Mode **Screen & System Audio Recording**, under Privacy & Security when prompted. Auto-paste still needs **Accessibility**. See [Required macOS permissions](#required-macos-permissions).

Verify a download against `SHA256SUMS.txt` from the same release:

```bash
shasum -a 256 -c SHA256SUMS.txt --ignore-missing
```

Intel Macs, older macOS releases, and signed/notarized distribution are not covered by this preview; use the source installation below or wait for a later release.

### Install from source (all platforms)

Use this for Intel Macs, older macOS, Fedora, development, or Linux NVIDIA GPU acceleration.

**Python:** 3.11–3.12 recommended (3.11 verified; 3.12 stable). 3.13 works, but on Debian/Ubuntu you need `python3-dev` and a compiler because `pynput` → `evdev` has no prebuilt wheel for 3.13 yet. Use a virtual environment.

On a minimal Linux install, install Python and the system libraries PyQt6 and `sounddevice` need before creating the venv. The launcher prints the matching `apt` / `dnf` / `pacman` command if a library is missing.

**Debian / Ubuntu / Pop!_OS:**

```bash
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-dev build-essential \
  libportaudio2 libegl1 libgl1 libxcb-cursor0 libxkbcommon-x11-0 \
  libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libxcb-xkb1
```

**Fedora / RHEL:**

```bash
sudo dnf install -y \
  python3 python3-devel gcc portaudio \
  xcb-util-cursor xcb-util-keysyms xcb-util-wm \
  libxkbcommon-x11 mesa-libEGL libglvnd-glx
```

**Arch Linux:**

```bash
sudo pacman -S --needed \
  python python-pip base-devel portaudio \
  xcb-util-cursor xcb-util-keysyms xcb-util-wm \
  libxkbcommon libgl
```

Meeting Mode on Linux also needs a Pulse-compatible session and the Pulse client library. See [`docs/linux-system-audio.md`](docs/linux-system-audio.md).

```bash
git clone https://github.com/Knuckles92/OpenWhisper
cd OpenWhisper
# Windows
python -m venv venv
venv\Scripts\activate
# macOS / Linux
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

From the repo root, run `install.cmd` (Windows) or `./install.sh` (macOS / Linux) once to register `ow` and `openwhisper`. Details: [Source launchers](CONTRIBUTING.md#source-launchers).

Native installer builds and component-pin updates: [Packaging](docs/packaging.md).

### Optional downloadable components

**Downloads** (from Model Manager) installs Whisper GPU acceleration, the Meeting Intelligence Agent, and the Windows speech runtimes (four engines, six models). Speech model weights are separate downloads. Every archive is SHA-256 verified before extraction.

For cloud transcription, transcript cleanup, or meeting intelligence, add API keys under **Settings → API keys**. They are saved in the OS credential store, never in the settings file or the log. A **Test** button checks a key before you rely on it. Environment variables or a `.env` file are used when no key is saved (`OPENAI_API_KEY`, `OPENROUTER_API_KEY`). Custom OpenAI-compatible text endpoints are added in **Model Manager**.

## Required macOS permissions

Grant these to the app identity that is actually running OpenWhisper:

- **Microphone** — needed to record audio (System Settings > Privacy & Security > Microphone). You'll be prompted on first recording.
- **Screen & System Audio Recording** — needed for **Meeting Mode** to capture the other side of a call. macOS has no loopback input device, so system audio comes from a ScreenCaptureKit stream; only its audio is read and every video frame is discarded. Without this grant a meeting still runs, but records your microphone only. OpenWhisper asks for it when you start a meeting.
- **Accessibility** — needed only for **auto-paste**. Without it, transcriptions are still copied to the clipboard. Global hotkeys work without Accessibility (Carbon `RegisterEventHotKey`).
- **Input Monitoring** *(optional)* — may be required when **remapping hotkeys** in Settings > Hotkeys. Normal hotkey use does not need this.

To check the system-audio path independently of the app, run `.venv/bin/python scripts/probe_macos_loopback.py` with something playing.

For packaged builds, this should appear as the OpenWhisper app. For development launches, use `scripts/openwhisper` or `ow`; on macOS the launcher runs through the framework `Python.app` so Accessibility has an app bundle it can select. If the list does not populate automatically, use the `+` button in Accessibility and add the app bundle shown in OpenWhisper's startup prompt, then fully quit and relaunch.

Do not add `venv/bin/python` if macOS greys it out — that path is usually a virtualenv symlink. If auto-paste silently does nothing, Accessibility is still missing for the current launch identity. If hotkey capture in Settings fails, add Input Monitoring as well.

## Usage

If you registered the launcher, type `ow` or `openwhisper` from any terminal. Otherwise:

```bash
# macOS / Linux
python3 app_qt.py
# Windows
python app_qt.py
```

### Hotkeys

Default hotkeys depend on your platform (all remappable in **Settings > Hotkeys**):

| Action | Windows / Linux | macOS |
|--------|-----------------|-------|
| Start/stop recording | `*` (numpad) | `⌃⌥R` |
| Cancel | `-` (numpad) | `⌃⌥⎋` |
| Enable/disable program | `Ctrl+Alt+*` | `⌃⌥⇧R` |
| Minimize to tray | `Ctrl+Alt+M` | `⌃⌥M` |

On macOS, supported modifiers are `⌘` (Command), `⌃` (Control), `⌥` (Option), `⇧` (Shift).

**Recording activation** (Settings > Hotkeys) chooses how the record hotkey behaves:

- **Toggle** (default) — press to start recording, press again to stop and transcribe.
- **Push and hold** — hold the hotkey to record; release to stop and transcribe. Taps shorter than 250 ms are canceled instead of transcribed.

## Settings

Access settings via **File > Settings** or the system tray menu:

- **General:** Auto-paste, copy to clipboard, minimize to tray on close, UI font size, and automatic update checks and notifications.
- **Recording:** Microphone input, saved-recording retention, and the live preview overlay (dictation preview and its font size).
- **Cleanup:** Enable AI transcript cleanup, thinking level, and custom prompt. Provider and model live in **Model Manager**.
- **Learned rules:** Personal spelling and formatting rules, taught by text or voice. Applied whenever AI cleanup runs.
- **Intelligence:** What the meeting agent may search, and which models it uses.
- **After the meeting:** End-of-meeting steps (re-transcription, AI cleanup, final report with Ribbon, Brief, and Signal views).
- **Dashboard:** Localhost vs. LAN sharing and port.
- **API keys:** OpenAI, OpenRouter, and custom-endpoint credentials in the OS credential store.
- **Hotkeys:** Global shortcuts and record activation (toggle or push-and-hold).
- **Advanced:** Developer mode (demo meeting fixture) and the model download policy. Voice engine, device, and quantization are set in the main window or **Model Manager**.

## Offline Usage and Model Downloads

Model loading is **cache-first**: complete cached models load locally without network metadata checks. Missing models follow the download policy, including speech models hosted outside Hugging Face. Optional runtime installation is a separate action in Downloads.

The policy lives in **Settings → Advanced → Hugging Face Downloads**:

- **Ask before downloading** (default): a consent dialog shows the model, repository, and approximate size. Approve once, or switch to always allowing downloads.
- **Always allow downloads**: missing models download without prompting. Cached models are still never re-checked for updates.
- **Never connect (fully offline)**: no downloads unless you explicitly approve a one-time override.

`HF_HUB_OFFLINE=1` before launch is a hard override that disables downloads entirely (still supported for scripts and CI):

```bash
export HF_HUB_OFFLINE=1
python3 app_qt.py
```

```powershell
$env:HF_HUB_OFFLINE = "1"
python app_qt.py
```

## Requirements

- Python 3.11–3.12 recommended; 3.13 supported (needs `python3-dev` on Debian/Ubuntu for `evdev`)
- Windows, macOS, or Linux for the base app; the four Windows engines (six models) currently require Windows x64
- Model-specific runtime, disk, and device requirements: [local backend guide](docs/local-asr.md#download-and-disk-sizes)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for backend/model changes, documentation requirements, validation, and code style. [Documentation index](docs/README.md).

## Credits

OpenWhisper builds on these projects:

- [OpenAI Whisper](https://github.com/openai/whisper) – the original speech recognition models
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) – a CTranslate2-based reimplementation used for the Local Whisper backend
- [Systran models on Hugging Face](https://huggingface.co/Systran) – converted Whisper and Distil-Whisper weights for most Local Whisper models
- [Mobius Labs models on Hugging Face](https://huggingface.co/mobiuslabsgmbh) – turbo model weights (`faster-whisper-large-v3-turbo`)

- [NVIDIA NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp) – native Parakeet and Nemotron inference
- [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) – Qwen voice models and Transformers runtime
- [Moonshine](https://github.com/moonshine-ai/moonshine) – CPU speech models and native streaming runtime

## License

OpenWhisper source is MIT licensed. Model weights and runtime dependencies retain their own licenses; see [Third-party notices](THIRD_PARTY_NOTICES.md) and the [model license table](docs/local-asr.md#download-and-disk-sizes).
