# OpenWhisper

A desktop app for recording audio and transcribing it to text using local Whisper models or OpenAI API. Features a modern PyQt6 GUI, system tray integration, global hotkeys, and auto-paste.


<p align="center">
  <img width="450" height="300" alt="h3eFEqIiLQ" src="https://github.com/user-attachments/assets/d9dcc898-9532-4489-b6ec-88bec4172e91" />
</p>

<p align="center">
  <img alt="Cursor_wTPeidZjsL" src="https://github.com/user-attachments/assets/ef87747a-4e41-47e4-b93a-20c9e833a570" />
</p>

<p align="center">
  <img alt="Cursor_eyykcjebiU" src="https://github.com/user-attachments/assets/c57070d4-69be-45f6-a73d-dcaa08294dac" />
</p>

<p align="center">
  <img width="984" height="841" alt="image" src="https://github.com/user-attachments/assets/840510c3-9f24-40c3-b846-38a5a5664a6b" />
</p>



## Features

- **Local Whisper** – Runs offline with `faster-whisper`, using optimized Whisper models (~150MB download on first use)
- **API Options** – OpenAI Whisper API, GPT-4o Transcribe, GPT-4o Mini Transcribe
- **Global Hotkeys** – Start/stop recording from any app (customizable)
- **Auto-paste** – Transcription automatically pastes to your active window
- **System Tray** – Minimize to tray, always accessible
- **Smart Splitting** – Large audio files split automatically to avoid API limits
- **Audio Device Selection** – Choose your preferred microphone input
- **Transcription History** – Browse past transcriptions with search/filter, retranscribe recordings
- **Audio Upload** – Import existing audio files for transcription
- **Real-time Visualization** – Animated waveform overlay shows recording status
- **Live Streaming** *(experimental)* – Real-time transcription preview while recording
- **Caret Indicator** *(experimental)* – Visual marker at cursor location when pasting
- **Window Memory** – Remembers window position and size between sessions

## GPU Acceleration

For significantly faster transcription speeds with an NVIDIA GPU, install CUDA support:

```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
```

With CUDA enabled, faster-whisper runs 2-4x faster than CPU-only. The app auto-detects GPU availability and selects optimal settings (turbo model on GPU, base on CPU). Streaming transcription uses ~15-20% GPU vs 40-60% CPU.

## Installation

**Note:** It's recommended to set up a virtual environment (venv) to avoid package version conflicts. I have found Python 3.12 to be pretty stable with this codebase.

```bash
git clone https://github.com/Knuckles92/OpenWhisper
cd OpenWhisper
python -m venv venv
# Windows
venv\Scripts\activate
# Linux/Mac
source venv/bin/activate
pip install -r requirements.txt
```

OPTIONAL: For cloud transcription, set your API key:
```bash
# Windows
set OPENAI_API_KEY=your-key

# Or create a .env file
OPENAI_API_KEY=your-key
```

## Quick Launch (Windows)

For everyday use, you can register `ow` and `openwhisper` as global commands so the app launches from any terminal in any directory — no need to `cd` into the repo or activate the venv first.

### One-time install

From the repo root, run:

```
install.cmd
```

This adds `scripts\` to your user PATH (via the registry, not `setx` — see note below). It's idempotent, so running it twice does nothing the second time. **Open a new terminal afterward** for the change to take effect.

After install, both commands work from anywhere:

```
ow              # short alias
openwhisper     # full name
```

The launcher invokes `venv\Scripts\pythonw.exe` directly, so the app always uses the project's venv regardless of which environment your shell has activated. Code changes are picked up live — no reinstall needed after `git pull`.

### Uninstall

```
uninstall.cmd
```

Removes the PATH entry only. Your venv, code, and the `scripts/` folder are left untouched, so re-running `install.cmd` later will restore the commands.

### Manual install (no scripts)

If you can't or don't want to run the installer (e.g., corporate execution-policy restrictions), add the path yourself in PowerShell:

```powershell
$dir = "D:\path\to\whisper_local\scripts"   # <-- adjust to your clone location
$current = [Environment]::GetEnvironmentVariable("Path", "User")
if ($current -split ";" -notcontains $dir) {
    [Environment]::SetEnvironmentVariable("Path", "$current;$dir", "User")
}
```

> **Why not `setx`?** `setx PATH ...` from a `.cmd` file silently truncates PATH at 1024 characters and can duplicate System PATH entries into User PATH. `install.cmd` shells out to PowerShell, which writes directly to `HKCU\Environment\Path` via `[Environment]::SetEnvironmentVariable` — no truncation, no leakage between User and System scopes.

### Alternative: skip PATH editing

If you'd rather not modify your PATH at all, drop a copy of [scripts/openwhisper.cmd](scripts/openwhisper.cmd) into `%LOCALAPPDATA%\Microsoft\WindowsApps\` (which is already on Windows PATH for every user). Caveat: this is a *copy*, so you'd need to refresh it whenever the launcher logic changes — which is rare, but worth knowing.

## Usage

If you ran `install.cmd`, just type `ow` or `openwhisper` from any terminal. Otherwise:

```bash
python app_qt.py
```

### Hotkeys

| Key | Action |
|-----|--------|
| `*` | Start/stop recording |
| `-` | Cancel |
| `Ctrl+Alt+*` | Enable/disable program |

All hotkeys can be remapped in the settings.

## Settings

Access settings via **File > Settings** or the system tray menu. Available options:

**General:** Default model, auto-paste, clipboard copy, minimize to tray, streaming transcription (experimental)

**Audio:** Sample rate, channels, silence threshold, input device selection

**Hotkeys:** Customize all keyboard shortcuts

**Advanced:** Whisper model selection (14+ options), compute device (auto/cuda/cpu), compute type (float16/float32/int8), max file size before splitting, streaming overlay positioning, logging

## Offline Usage

Local Whisper transcription works fully offline after the initial model download. However, on startup, the `faster-whisper` library makes a brief metadata check to HuggingFace to see if a newer model version is available. This is not a model download—just a lightweight API call. If you're offline, the check will fail silently and the cached local model loads normally.

To force fully offline operation (skip the metadata check), set this environment variable before running:

```bash
export HF_HUB_OFFLINE=1  # Linux/Mac
set HF_HUB_OFFLINE=1     # Windows
python app_qt.py
```

## Requirements

- Python 3.8+(3.12 recommended)
- Windows

**Note:** Caret paste indicator is Windows-only (uses Windows API).

## License

MIT License. Free to use, clone, and modify.

