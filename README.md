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

## GPU Acceleration

For significantly faster transcription speeds, install CUDA support:

```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
```

With an NVIDIA GPU and CUDA enabled, local Whisper runs much faster than CPU-only. In my tests it was at least 2-3x faster. 

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
```

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

## Requirements

- Python 3.8+(3.12 recommended)
- Windows

## License

MIT License. Just use the thing.



