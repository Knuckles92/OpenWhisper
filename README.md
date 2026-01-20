# OpenWhisper

A desktop app for recording audio and transcribing it to text using local Whisper models or OpenAI API. Features a modern PyQt6 GUI, system tray integration, global hotkeys, and auto-paste.

![Cursor_wTPeidZjsL](https://github.com/user-attachments/assets/ef87747a-4e41-47e4-b93a-20c9e833a570)

![Cursor_eyykcjebiU](https://github.com/user-attachments/assets/c57070d4-69be-45f6-a73d-dcaa08294dac)


<img width="924" height="700" alt="image" src="https://github.com/user-attachments/assets/b2f2d6c8-6f8c-424b-9add-8d6095108042" />


## Features

- **Local Whisper** – Runs offline using OpenAI's Whisper model (~150MB download on first use)
- **API Options** – Whisper API, GPT-4o Transcribe, GPT-4o Mini Transcribe
- **Global Hotkeys** – Start/stop recording from any app (customizable)
- **Auto-paste** – Transcription automatically pastes to your active window
- **System Tray** – Minimize to tray, always accessible
- **Smart Splitting** – Large audio files split automatically to avoid API limits
- **Audio Device Selection** – Choose your preferred microphone input
- **Transcription History** – Browse past transcriptions with search/filter, retranscribe recordings
- **Audio Upload** – Import existing audio files for transcription
- **Real-time Visualization** – Animated waveform overlay shows recording status
- **Live Streaming** – Real-time transcription preview while recording (experimental)
- **Meeting Mode** – Long-form transcription with live streaming and auto-save
- **Meeting Insights** – AI-powered summaries, action items, and custom analysis (OpenAI/OpenRouter)
- **Caret Indicator** – Visual marker at cursor location when pasting
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

For cloud transcription, set your API key:
```bash
# Windows
set OPENAI_API_KEY=your-key

# Or create a .env file
OPENAI_API_KEY=your-key

# Optional: For meeting insights via OpenRouter (access to Claude, Llama, Mistral, etc.)
OPENROUTER_API_KEY=your-openrouter-key
```

**Note:** API keys can also be configured in Settings > Insights for the meeting insights feature.

## Usage

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



