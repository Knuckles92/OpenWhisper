# OpenWhisper for macOS

A macOS build of OpenWhisper: record audio and transcribe it to text using local Whisper models or the OpenAI API. Modern PyQt6 GUI, menu-bar (tray) integration, global hotkeys, and auto-paste.

This is a full, standalone copy of the app adapted for macOS. The original Windows app lives in the repository root; everything here under `Mac/` runs independently.

## What's different from the Windows version

| Area | Windows | macOS (this build) |
|------|---------|--------------------|
| Global hotkeys | `keyboard` library (per-key suppression) | [`pynput`](https://pypi.org/project/pynput/) (observe-only, no suppression) |
| Default hotkeys | Numpad keys (`kp *`, `kp -`) | Command combos (see below) |
| Auto-paste | `Ctrl+V` | `Cmd+V` (via pynput) |
| Caret paste indicator | Tracks real text caret (win32) | Follows the mouse cursor (no public caret API on macOS) |
| Launchers | `.cmd` + PowerShell, `pythonw.exe` | Shell scripts + `venv/bin/python` |
| GPU | CUDA (NVIDIA) | CPU only (faster-whisper has no Metal/MPS backend) |

## Requirements

- macOS
- Python 3.12 recommended (3.8+ supported)

## Installation

It is recommended to use a virtual environment.

```bash
cd Mac
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

OPTIONAL: For cloud transcription, set your API key:

```bash
export OPENAI_API_KEY=your-key
# Or create a .env file in the Mac/ folder
OPENAI_API_KEY=your-key
```

## Required macOS permissions

macOS gates the features this app relies on behind privacy permissions. Grant these to the app you launch OpenWhisper from (Terminal, iTerm, or a bundled app):

- **Microphone** — needed to record audio (System Settings > Privacy & Security > Microphone). You'll be prompted on first recording.
- **Accessibility** and/or **Input Monitoring** — needed for global hotkeys and the synthetic `Cmd+V` auto-paste (System Settings > Privacy & Security > Accessibility and Input Monitoring). Without this, hotkeys won't fire and auto-paste won't work.

If hotkeys or auto-paste silently do nothing, it's almost always a missing Accessibility/Input Monitoring grant. After granting, fully quit and relaunch the app.

## Usage

```bash
cd Mac
python app_qt.py
```

### Hotkeys

| Key | Action |
|-----|--------|
| `Cmd+Shift+Space` | Start/stop recording |
| `Cmd+Shift+Escape` | Cancel |
| `Cmd+Alt+Shift+Space` | Enable/disable program |

All hotkeys can be remapped in **Settings > Hotkeys**. Supported modifiers: `Cmd`, `Ctrl`, `Alt` (Option), `Shift`.

> Note: Unlike the Windows build, macOS does not allow selectively swallowing individual key events, so hotkey combinations also reach the focused app. The Command-based defaults are chosen to avoid clashing with normal typing.

## Quick Launch (optional)

Register `ow` and `openwhisper` as global commands so the app launches from any terminal:

```bash
cd Mac
./install.sh
```

This adds `Mac/scripts` to your `PATH` via `~/.zprofile` (idempotent). Open a new terminal afterward, then run:

```bash
ow              # short alias
openwhisper     # full name
```

The launcher invokes `venv/bin/python` directly, so the app always uses the project's venv. Code changes are picked up live -- no reinstall needed after `git pull`.

### Uninstall

```bash
cd Mac
./uninstall.sh
```

Removes the `PATH` entry only. Your venv, code, and the `scripts/` folder are left untouched.

## Transcription backends

- **Local Whisper** — runs offline with `faster-whisper`. On macOS this uses the CPU (int8). The first run downloads the model (~150MB).
- **API options** — OpenAI Whisper API, GPT-4o Transcribe, GPT-4o Mini Transcribe (requires `OPENAI_API_KEY`).

## Offline Usage

Local Whisper works fully offline after the initial model download. To skip the startup HuggingFace metadata check entirely:

```bash
export HF_HUB_OFFLINE=1
python app_qt.py
```

## License

MIT License. Free to use, clone, and modify.
