# Architecture Overview

Service-oriented layout to keep UI orchestration thin and move logic into reusable services.

## Layers
- UI (`ui_qt/`): widgets, dialogs, overlay visuals. No business logic.
- Controllers (`app_qt.py`, `controllers/`): glue between UI signals/callbacks and services; orchestrate workflows and state presentation.
- Services (`services/`): logic/stateful helpers (audio processing, recording, transcription workflow, history, hotkeys). No UI imports.
- Backends (`transcriber/`): concrete transcription engines; expose a small interface used by services.
- Config (`config.py`): shared configuration constants.

## Roles
- Controller: reacts to UI/hotkeys, sequences service calls, updates UI/status. Should be thin.
- Manager/Service: owns a domain (audio processing, history, hotkeys, recording, transcription). Pure logic and resources; no UI.
- Processor/Backend: lowest-level data work (e.g., splitting audio, whisper/OpenAI inference).

## Current Mapping (after refactor)
- Controllers: `app_qt.ApplicationController`, `ui_qt/ui_controller.py`.
- Services: `services/audio_processing_service.py`, `services/history_service.py`, `services/hotkey_service.py`, `services/recording_service.py`, `services/transcription_service.py`, `services/workflow_service.py`, `services/settings_service.py`, `services/completion_service.py`.
- Backends: `transcriber/base.py`, `transcriber/local_backend.py`, `transcriber/openai_backend.py`.

## Guidelines
- Controllers should call services via methods or signals; avoid direct backend use.
- Services must not import PyQt UI classes; emit callbacks/signals upward if needed.
- Keep file names aligned to responsibilities (e.g., `workflow_service` for multi-step transcription).
- Shared DTOs/types live with their domain service.

## Adding Features
1) Put domain logic in a service (new file under `services/`).
2) Expose small methods; keep threading/executor decisions in the service.
3) Wire controllers to services; update UI through controller callbacks/signals.
4) Keep backends swappable by depending on `transcriber.TranscriptionBackend` interfaces.

## Testing Focus
- Services: unit tests without UI.
- Controllers: light integration tests (signals/callbacks).
- UI: manual/Qt tests for interactions only.

