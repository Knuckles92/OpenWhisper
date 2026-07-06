# GPT-Realtime-Whisper Support Exploration

## Overview

Add optional support for OpenAI `gpt-realtime-whisper` as a live cloud transcription path. This should complement the existing local streaming preview rather than replace the current file-based OpenAI transcription backends.

Official OpenAI docs describe `gpt-realtime-whisper` as the lowest-latency realtime transcription path for live transcript deltas, while `gpt-4o-transcribe` remains the better fit for file or request-response transcription workflows.

Relevant sources:

- https://developers.openai.com/api/docs/guides/realtime-transcription
- https://developers.openai.com/api/docs/guides/realtime-websocket
- https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api/
- https://openai.com/api/pricing/

## Current State

- File transcription is handled by `transcriber/openai_backend.py` through `client.audio.transcriptions.create(...)`.
- Local live preview is handled by `services/streaming_transcriber.py`.
- The current streaming implementation batches microphone chunks, repeatedly re-transcribes the entire accumulated session with local `faster-whisper`, and emits replacement text through `StreamingRuntime.on_partial_transcription(...)`.
- Streaming is currently limited to `LocalWhisperBackend` in `services/runtime/streaming.py`.
- Audio is captured at `config.SAMPLE_RATE` (`44100`) as mono `np.int16`.

## Proposed Architecture

Keep three distinct transcription modes:

1. Local final transcription: existing `LocalWhisperBackend`.
2. File/request-response OpenAI transcription: existing `OpenAIBackend` with `whisper-1`, `gpt-4o-transcribe`, and `gpt-4o-mini-transcribe`.
3. Cloud realtime transcription: new websocket-based streaming component using `gpt-realtime-whisper`.

Do not add `gpt-realtime-whisper` as a normal `OpenAIBackend.transcribe(audio_path)` option. It is a session/streaming model, not a file upload model.

## Files To Modify

- `requirements.txt`
  - Add `websocket-client` unless the OpenAI SDK gains a suitable supported realtime Python helper in the installed version.
- `config.py`
  - Add cloud realtime settings:
    - realtime model ID, default `gpt-realtime-whisper`
    - input sample rate target, `24000`
    - optional VAD settings
    - optional latency target setting if exposed by the API docs/client events
- `services/realtime_whisper_transcriber.py`
  - New component owning websocket connection, audio queue, resampling, base64 PCM encoding, event parsing, cancellation, and cleanup.
- `services/runtime/streaming.py`
  - Choose local streaming or cloud realtime streaming based on model/settings.
  - Route transcript deltas/completions into the existing `partial_transcription` and streaming paste overlay signals.
- `services/application_controller.py`
  - Initialize and clean up the cloud streaming component.
- `ui_qt/dialogs/settings_dialog.py`
  - Add settings to choose local streaming vs OpenAI realtime streaming, with API-key availability feedback.
- `config.MODEL_CHOICES` / `MODEL_VALUE_MAP`
  - Optional: expose as `API: Realtime Whisper` only if the UI makes clear that it is for recording/live mode, not uploaded files.
- `tests/`
  - Add mocked websocket tests for session update payloads, audio append payloads, delta handling, completion handling, and cleanup.

## Implementation Notes

### Session setup

Realtime transcription sessions should use:

```json
{
  "type": "session.update",
  "session": {
    "type": "transcription",
    "audio": {
      "input": {
        "format": {
          "type": "audio/pcm",
          "rate": 24000
        },
        "transcription": {
          "model": "gpt-realtime-whisper",
          "language": "en"
        },
        "turn_detection": {
          "type": "server_vad",
          "threshold": 0.5,
          "prefix_padding_ms": 300,
          "silence_duration_ms": 500
        }
      }
    }
  }
}
```

### Audio path

The recorder emits 44.1 kHz mono `int16` chunks. The Realtime API docs call for 24 kHz mono PCM when using `audio/pcm`, so the cloud component should:

1. Convert incoming arrays to mono `int16`.
2. Resample from 44.1 kHz to 24 kHz.
3. Base64-encode raw PCM16 bytes.
4. Send `input_audio_buffer.append` events.

Example event:

```json
{
  "type": "input_audio_buffer.append",
  "audio": "<base64 PCM16>"
}
```

With server VAD enabled, commits should happen automatically at turn boundaries. If VAD is disabled, the component must send `input_audio_buffer.commit` when stopping or when local logic decides a turn is complete.

### Transcript events

Handle:

- `conversation.item.input_audio_transcription.delta`
  - Append/display partial text immediately.
- `conversation.item.input_audio_transcription.completed`
  - Store final transcript text for the matching item.
  - Use `item_id` to avoid ordering issues when multiple turns complete.

The existing UI currently treats streaming `is_final=True` as a replacement of the full rolling transcript. For realtime deltas, use a small accumulator that maintains final text plus the current partial delta so the UI can still receive a full display string.

### Stop flow

On stop:

1. Stop feeding microphone chunks.
2. Send a final commit if manual commits are enabled.
3. Wait briefly for a completed transcription event.
4. Close the websocket.
5. Return the accumulated transcript to `TranscriptionRuntime`.

The app should still save the local recording and run the selected final transcription backend unless the user explicitly chooses to trust realtime output as final. A first implementation can treat realtime Whisper as preview-only.

## Open Questions

- Should `gpt-realtime-whisper` be preview-only at first, or can it replace the final transcription result for push-to-talk recordings?
- Should the app expose latency presets, or keep the first version at the API defaults?
- Should realtime streaming be available only when an OpenAI API key is configured, or should it be selectable and show a clear disabled status?
- How should uploaded files behave if `API: Realtime Whisper` is selected? Recommended answer: uploaded files should use `gpt-4o-transcribe` or `gpt-4o-mini-transcribe`, not realtime.

## Verification Steps

1. Activate the virtual environment:

```bash
.\venv\Scripts\activate
```

2. Run unit tests:

```bash
python -m pytest tests/
```

3. Run the app:

```bash
python app_qt.py
```

4. Manual checks:

- Local Whisper recording/transcription still works.
- Existing OpenAI file transcription models still work.
- Local streaming mode still works.
- OpenAI realtime streaming shows deltas during recording and completes cleanly on stop.
- Canceling recording closes the websocket and stops audio queue processing.
- Missing API key shows a clear status and does not break local recording.
