# OpenAI Realtime Dictation Beta

## Overview

Add a second beta recording path that uses OpenAI Realtime transcription from start to finish:

- stream microphone audio to `gpt-realtime-whisper` while recording
- show transcript deltas in the existing partial transcript UI
- on stop, use the accumulated realtime transcript as the final transcript
- save the local WAV and history entry as usual
- paste once at the end through the existing final transcript path

This is separate from the existing local streaming preview beta.

## Important API Notes

OpenAI realtime transcription uses:

- WebSocket connection
- `session.update` with `session.type = "transcription"`
- `audio.input.format = { "type": "audio/pcm", "rate": 24000 }`
- `audio.input.transcription.model = "gpt-realtime-whisper"`
- `input_audio_buffer.append` events containing base64 PCM bytes
- `conversation.item.input_audio_transcription.delta`
- `conversation.item.input_audio_transcription.completed`

When sending `audio/pcm`, OpenAI documents 24 kHz mono PCM. The beta should try to capture directly at 24 kHz mono int16 first. If the selected device does not support that, fall back to the app's normal recorder settings and resample in the realtime transcriber.

## Files

- `config.py`
- `services/settings.py`
- `services/openai_realtime_transcriber.py`
- `services/runtime/streaming.py`
- `services/runtime/transcription.py`
- `services/application_controller.py`
- `ui_qt/dialogs/settings_dialog.py`
- `requirements.txt`
- `tests/test_openai_realtime_transcriber.py`
- `tests/test_application_controller.py`

## Verification

Use the project venv:

```bash
.\venv\Scripts\activate
python -m pytest tests\
```
