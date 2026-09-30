# OpenWhisper

A desktop app for dictation, audio-file transcription, and meeting notes on Windows, macOS, and Linux. Transcribe with local speech models or the OpenAI API, then optionally use AI to clean up the text.

[Website](https://openwhisper.fiorilabs.tech/) · [Download](https://github.com/Knuckles92/OpenWhisper/releases/latest) · [Run from source](#run-from-source) · [Install](#install) · [Changelog](CHANGELOG.md)

Use a [packaged release](#install) with bundled Python and dependencies, or [run from source](#run-from-source) using your own Python environment.

<p align="center">
  <img width="680" alt="OpenWhisper Quick Record" src="docs/screenshots/01-quick-record-idle.png" />
</p>

<p align="center">
  <img width="360" alt="Waveform overlay showing recording, transcription, and live preview" src="docs/screenshots/overlay-states.gif" />
</p>

<p align="center">
  <img width="880" alt="Remote engine: Quick Record on a laptop using a desktop's Parakeet engine, and the desktop's sharing settings with a pairing code and the connected laptop" src="docs/screenshots/remote-engine.png" />
</p>

## What it does

- **Dictation:** Record from any app with a global hotkey, see a live preview on supported engines, and paste the result into the active window.
- **Audio files:** Transcribe one file or a queue, keep results separate or combine them, and copy the output when ready.
- **Meetings:** Capture microphone and system audio, follow a live dashboard, review searchable transcripts and AI insights, play recordings, and export. Available on Windows and macOS 13+; Linux system audio is a [preview](docs/linux-system-audio.md).
- **AI cleanup:** Apply spelling and style rules or reusable [cleanup profiles](docs/cleanup-profiles.md), with separate text-model choices for dictation and meetings.
- **One Settings window:** An Overview of what is running, each model choice on the page for the feature it powers, local models and runtimes under Downloads, and Ctrl+K search across every setting, model, and help note.
- **History:** Search, retranscribe, and export transcripts as Markdown, plain text, or JSON. See [export format support](docs/export-support.md) for what each format includes.
- **Agent history API (preview):** Start an opt-in local, authenticated API to search saved transcriptions and meetings, retrieve notes, and cite transcript segments. See [API setup and reference](docs/agent-api.md).
- **MCP for your agent:** Enable **Settings → MCP** to connect an agent to saved history, transcripts, and meeting insights. Includes live server status, a local URL, and copyable setup prompts, commands, and client configuration. See [MCP setup](docs/mcp.md).

The app also includes microphone selection, a system tray where available, and dark, light, or system-matched themes.

## Install

Download the package for your platform from [Releases](https://github.com/Knuckles92/OpenWhisper/releases/latest). Native packages include Python and the app dependencies.

| Platform | Install |
| --- | --- |
| Windows | Run the `.exe` installer. It installs per-user without admin rights. |
| macOS | Open the `.dmg` and drag OpenWhisper into Applications. Requires Apple Silicon and macOS 14+. The preview is not notarized; approve first launch in **Privacy & Security → Open Anyway**. |
| Linux | Install the `.deb` on Debian 12+ / Ubuntu 22.04+, or `.pkg.tar.zst` on Arch-compatible distributions. Both packages require x86_64. |

On Linux, use the matching command, replacing `<version>` with your download's version:

```bash
sudo apt install "./OpenWhisper-<version>-linux-amd64.deb"
# or
sudo pacman -U "./OpenWhisper-<version>-linux-x86_64.pkg.tar.zst"
```

Launch from your app menu; Linux packages also provide `ow` and `openwhisper` commands.

Downloads include `SHA256SUMS.txt` for verification. Compare your file's hash using `Get-FileHash` in PowerShell, `shasum -a 256` on macOS, or `sha256sum` on Linux. The Windows installer is unsigned; if SmartScreen blocks it, verify the download before choosing **More info → Run anyway**.

### macOS permissions

Allow **Microphone** for recording, **Screen & System Audio Recording** for meeting system audio, and **Accessibility** for auto-paste under **System Settings → Privacy & Security**. Without Accessibility, you can still copy transcripts. **Settings → General → Set up auto-paste** shows the exact app to allow; if permission stops working after an update, remove its old entry, add that app again, and restart.

## Run from source

Run OpenWhisper directly from a source checkout on Windows, macOS, or Linux. You'll need **Git** and **Python 3.11 or 3.12**; the steps below install the app's dependencies in a virtual environment. This also supports Intel Macs and Linux distributions without native packages.

### One-time setup

Clone the repository and enter its folder:

```bash
git clone https://github.com/Knuckles92/OpenWhisper
cd OpenWhisper
```

Then create the virtual environment and install dependencies for your platform.

**Windows (PowerShell):**

```powershell
python -m venv venv
. .\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

**macOS / Linux:**

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
```

On Linux, install Python's venv and development packages and a C compiler if needed. The launcher checks audio and Qt system libraries and prints the package-manager command for any missing dependencies. Meeting capture also needs [PulseAudio or PipeWire-Pulse](docs/linux-system-audio.md).

### Launch the app

After setup, use these commands whenever you want to run OpenWhisper. Run them from your `OpenWhisper` checkout folder.

**Windows (PowerShell):**

```powershell
. .\venv\Scripts\Activate.ps1
python main.py
```

**macOS / Linux:**

```bash
./scripts/openwhisper
```

The macOS/Linux launcher selects the project's virtual environment automatically. On macOS, it also lets Accessibility setup identify the correct app bundle. After launch, follow [Get started](#get-started) to choose a model and microphone.

### Optional: launch from any terminal

To register `ow` and `openwhisper`, run `.\install.cmd` in PowerShell on Windows or `./install.sh` on macOS/Linux once from your checkout folder. Open a new terminal, then use `ow` or `openwhisper` from any folder without activating the virtual environment. See [source launchers](CONTRIBUTING.md#source-launchers) for details.

### Optional: develop with uv

Contributors can use `uv sync` and `uv run python main.py` without activating a virtual environment. See [development with uv](CONTRIBUTING.md#development-with-uv) for setup, tests, and NVIDIA GPU support. The pip instructions above and packaged releases do not require uv.

## GPU acceleration

For Local Whisper, install **GPU Acceleration** from **Downloads → Components** on Windows or Linux x86_64. When Local Whisper finds an NVIDIA GPU without these libraries, it offers **Use this GPU**, which installs them, downloads the model the card should run, and loads it on the GPU. Source installs can instead run `python -m pip install -r requirements-gpu.txt` in the activated virtual environment. Both need an NVIDIA driver providing CUDA 12 (525+); the CUDA Toolkit is not required. macOS uses CPU.

On **Auto**, Local Whisper picks the compute type the card supports and has room for: float16 on RTX cards, int8_float32 on GTX 10-series and older cards, which have no float16. The log names the choice before loading, and **Downloads** gives each model's memory estimate for this computer's GPU. For example, turbo needs about 1.4 GB on a 4 GB GTX 1050 Ti.

Other speech engines use their own runtimes from Downloads. The Meeting Intelligence Agent is also a separate component, available on Windows and Linux.

## Updates

Use **Help → Check for Updates**. Windows applies an in-app update or opens setup; macOS opens a verified DMG for replacement in Applications. On Linux, install the new package with the same command as above. Source users run `git pull --ff-only` and reinstall requirements if dependencies changed. Automatic checks and notifications are configurable in **Settings → General**.

## Get started

1. Open **Settings → Voice model** and choose a speech backend. New Windows x64 installs default to Parakeet; other platforms default to Local Whisper. Existing choices are preserved.
2. Download the selected model and any required runtime through **Settings → Downloads** (**Get models and runtimes** on the Voice model page opens it filtered to your engine), or add an OpenAI key in **Settings → API keys** for cloud transcription.
3. Choose your microphone in **Settings → Recording**. On macOS, grant the [required permissions](#macos-permissions).
4. Use **Quick Record** or the recording hotkey. Stop recording to transcribe; dictation follows your clipboard and auto-paste settings. **Upload File** results stay in the app and have Copy buttons.

For meetings, open **Meeting Mode**. Before starting, you can write an optional brief saying what you want out of the meeting — the AI note taker and the live copilot read it on every pass, so a request like "capture who objected to the vendor and why" is watched for as the meeting reaches it. The host can change it on the dashboard at any time. After local transcription finishes, **Continue in the background** lets you start another meeting while cleanup and reports finish; results remain in Past Meetings. Optional [insight review](docs/meeting-insight-review.md) uses TypeSafe to ask a few questions about uncertain commitments, owners, deadlines, or decisions; your answers update the insights and notes.

Optional [fast judgments](docs/typesafe-fast-judgments.md) add advisory citation checks, meaning-based history search, an open-question radar, and clickable highlight pulses. Enable them individually under **Meeting Mode → Fast judgments**. Spoken instructions also support recaps and reversible term corrections.

### Hotkeys

Change shortcuts and choose **Toggle** or **Push and hold** in **Settings → Hotkeys**. Toggle starts and stops recording with successive presses; push-and-hold records until you release the shortcut.

| Action | Windows / Linux | macOS |
| --- | --- | --- |
| Start/stop recording | Numpad `*` | `Control+Option+R` |
| Cancel | Numpad `-` | `Control+Option+Escape` |
| Enable/disable program | `Ctrl+Alt+Numpad *` | `Control+Option+Shift+R` |
| Minimize to tray | `Ctrl+Alt+M` | `Control+Option+M` |

On X11 Linux, hotkeys also reach the focused app. Omarchy uses compositor-owned desktop shortcuts and Hyprland's paste dispatcher. Other native Wayland desktops retain focused-window shortcuts and manual clipboard paste; blocking X11 hooks are disabled. On macOS, auto-paste requires Accessibility permission; normal global hotkeys do not.

### Omarchy / Hyprland

OpenWhisper automatically selects its Omarchy interface on Omarchy 3 and 4. It uses a compact application header, square controls, and the desktop's [shared palette](https://github.com/omacom/omarchy/blob/quattro/docs/theming.md). The existing recording, uploads, meetings, host dashboard, and settings remain available without a Quickshell extension or another UI runtime.

Hyprland owns window placement and sizing. Switching views, opening History, or using Compact Mode changes the content inside the current tile; it no longer animates or restores the outer window's geometry. Small tiles scroll, the splash uses an opaque surface, and shortcut hints use native Qt popups. The in-app recording indicator stays within the window; the optional native bar widget provides recording status and live text while working in another app.

Omarchy windows omit the top-right minimize, maximize, and close controls, including Qt's fallback titlebars on dialogs. Use Hyprland's window commands, the application menu, or the footer's Hide and Quit actions. Settings can shrink without forcing a larger surface: overflowing forms scroll, overview cards stack, and search fits the current window. Tray restore preserves maximized and fullscreen state.

New Omarchy installations follow the desktop colors automatically. An existing explicit Dark or Light preference is preserved: choose **Settings → General → Theme → Omarchy desktop** (or **Match system**) to follow the current Omarchy palette. Theme changes are picked up within about two seconds, including replacement of the theme directory. Both the Omarchy 4 XDG state path and Omarchy 3 config path are supported. Qt uses Fusion control metrics and the compositor's display scale independently of GTK's `GDK_SCALE`.

On Hyprland 0.55+, Omarchy mode registers the configured record, cancel, enable/disable, tray, meeting, and cleanup-profile shortcuts through the compositor. Both press and release are supported for push-and-hold. Existing Hyprland bindings are preserved: conflicting shortcuts remain available inside OpenWhisper, and Settings reports the conflict. Bindings refresh after a compositor configuration reload and are removed on normal app exit. This requires `hyprctl` and `gdbus`; it never edits the user's Hyprland configuration. Auto-paste dispatches Ctrl+V to the focused destination, or Ctrl+Shift+V in recognized terminals. Keep that destination focused when stopping dictation.

The Omarchy 4 bar companion is in [`integrations/omarchy`](integrations/omarchy). Copy that folder to `~/.config/omarchy/plugins/org.openwhisper.controls`, then run `omarchy-shell shell rescanPlugins` and `omarchy plugin enable org.openwhisper.controls --section right`. Click its microphone to record/stop, right-click to cancel, or middle-click to open the app. Bar clicks behave like the app's buttons, including when keyboard shortcuts are paused or use push-and-hold. Its live preview uses Omarchy's own anchored popup, palette, font, and sizing components. Status is private to the current user under `$XDG_RUNTIME_DIR/openwhisper`; previews expire when the app stops responding. Disable it with `omarchy plugin disable org.openwhisper.controls`.

For testing, launch with `OPENWHISPER_UI=omarchy ow` to select this interface explicitly, or `OPENWHISPER_UI=classic ow` for the classic appearance. Wayland sizing and popup protections remain enabled with either appearance. Omarchy 3 theme locations are supported; desktop shortcut and bar integration are validated on Omarchy 4 / Hyprland 0.56.

Implementation references include [Omawrite's live palette handling](https://github.com/omacom/omawrite/blob/master/src/backend.cpp), [Omacut's Qt application window](https://github.com/omacom/omacut/blob/master/src/Main.qml), and [Omarchy's anchored popup sizing](https://github.com/omacom/omarchy/blob/quattro/shell/Ui/PopupCard.qml). OpenWhisper uses its existing Qt Widgets runtime; shell plugins and application windows have different focus and placement requirements.

Developers can run `python scripts/qa_omarchy_ui.py --extended --output /tmp/openwhisper-ui-captures` inside a Hyprland 0.55+ session. It creates disposable settings/history, exercises every Settings destination, window modes, repeated resizing/reopening, tray restoration, common dialogs, menus, indicators, font scales, and themes. It compares Qt and compositor dimensions and verifies keypad callbacks through Hyprland, writing screenshots plus a JSON report without starting an engine or recording audio. The basic probe without `--extended` also works on earlier Hyprland versions.

With the main app stopped, `python scripts/qa_omarchy_controls.py` separately checks D-Bus actions, temporary compositor bindings, shortcut capture/re-registration, native Wayland paste into a disposable text field, and binding cleanup. It restores the clipboard and does not use the microphone.

## Speech models

| Backend | Models | Platform / device | Workflows |
| --- | --- | --- | --- |
| Local Whisper | Standard Whisper sizes, turbo, Distil-Whisper | All platforms, CPU; NVIDIA CUDA on Windows/Linux | Dictation with preview, uploads, meetings |
| Parakeet | TDT 0.6B v3 | Windows x64 and Linux x86_64 CPU / NVIDIA GPU; Apple Silicon CPU | Dictation with preview, uploads, meeting chunks |
| Qwen3-ASR | 0.6B, 1.7B | Windows x64 CPU / NVIDIA GPU | Dictation, uploads |
| Nemotron Streaming | 3.5 ASR Streaming 0.6B | Windows x64 and Linux x86_64 CPU / NVIDIA GPU | Dictation with preview, uploads, meetings with native preview |
| Moonshine | Streaming Small / Medium, English | Windows x64 CPU | Dictation, uploads, meetings with native preview |
| OpenAI API | GPT-Transcribe; GPT-4o Transcribe, GPT-4o Mini Transcribe, and Whisper until OpenAI retires them on February 26, 2027 | Cloud; API key and network required | Dictation, uploads |
| Remote computer | Whichever engine the paired computer has selected | Another computer running OpenWhisper on your network | Dictation with that engine's preview, uploads, meetings with a supported host model |

Local model weights and optional runtimes are separate downloads. **Settings → Downloads** shows model details and required components, and verifies component archives before installation. macOS transcription uses CPU; see [GPU acceleration](#gpu-acceleration) for Windows and Linux.

### Remote engine

Use another computer's OpenWhisper engine for dictation, uploads, or meetings:

1. On the computer with the engine, open **Settings → Dictation → Remote engine**, turn on **Share this computer's engine**, and click **Pair a device**. Allow OpenWhisper through the firewall if Windows asks.
2. On the other computer, open the same page, enter the host's address (shown under the switch) and the six-digit code, and click **Pair**. Check that both screens show the same identity code.
3. Click **Use for dictation**, or choose **Remote computer** as the recording engine.

The host's selected engine handles transcription. Traffic is encrypted and limited to paired computers; remove access under **Paired computers**.

- **Meetings:** Select **Remote computer** in **Settings → Meeting Mode → Voice & speakers → Speech engine**. Audio goes to the host for transcription; capture, recordings, and transcripts stay on the computer running the meeting.
- **Host models:** Use **Manage host models** to choose or download a model and install speech runtimes. The host must first enable **Allow paired computers to manage models**.
- **Away from home:** Install [Tailscale](https://tailscale.com/download) on both computers. Hosts on your tailnet appear in the app; computers on the same Tailscale account can connect without a pairing code when the host allows it.

### AI cleanup and meeting intelligence

Speech recognition and text processing use separate models. Choose the cleanup model on **Settings → AI cleanup** and the meeting model on **Settings → Intelligence**; each is set independently. Text providers include OpenAI, OpenRouter, Ollama, Groq, OpenCode Go/Zen, and custom OpenAI-compatible endpoints.

Add credentials in **Settings → API keys**; they are stored in the OS credential store. Environment variables or a `.env` file provide a fallback when no key is saved: `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `OPENCODE_GO_API_KEY`, and `OPENCODE_ZEN_API_KEY`. Add custom endpoints from either chat-model picker. Ollama requires a separately managed server.

Enable cleanup in **Settings → AI cleanup**, teach spelling and style in **Learned rules**, and use [Profiles](docs/cleanup-profiles.md) for formats such as emails or support tickets. Meeting settings control intelligence, end-of-meeting processing, and dashboard sharing.

### Offline use

Downloaded speech models load from the local cache without network metadata checks. Install any required runtime before going offline.

**Settings → Downloads → When a model is missing from this computer** controls missing-model downloads: ask first (default), always allow, or never connect unless you approve a one-time override. Setting `HF_HUB_OFFLINE=1` before launch blocks model downloads entirely. This controls model downloads; cloud transcription and remote text providers still require a network connection.

<details>
<summary>More screenshots</summary>

<p align="center">
  <img width="880" alt="Settings Overview: models in use, what stays local, and storage" src="docs/screenshots/01-settings-overview.png" />
</p>

<p align="center">
  <img width="480" alt="Meeting Mode after a call: finalization steps complete and the report ready" src="docs/screenshots/02-meeting-mode-ready.png" />
</p>

<p align="center">
  <img width="880" alt="Settings → AI cleanup: chat model, endpoint, and thinking level" src="docs/screenshots/03-settings-ai-cleanup.png" />
</p>

<p align="center">
  <img width="880" alt="Settings → Downloads: model catalog and technical details" src="docs/screenshots/06-downloads-model-profile.png" />
</p>

<p align="center">
  <img width="880" alt="General settings" src="docs/screenshots/01-general.png" />
</p>

</details>

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, validation, and backend changes. Release history lives in [CHANGELOG.md](CHANGELOG.md).

OpenWhisper builds on OpenAI Whisper, faster-whisper, NVIDIA NeMo-Speech.cpp, Qwen3-ASR, and Moonshine, with converted Whisper weights from Systran and Mobius Labs. The app source is [MIT licensed](LICENSE); model weights and dependencies retain their own licenses. See [Third-party notices](THIRD_PARTY_NOTICES.md).
