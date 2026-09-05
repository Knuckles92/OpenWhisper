# Transcription model reference

The Windows backend expansion is **unreleased**; published 2.5.2 installers do not contain it. This reference covers the current implementation. [Setup and measurements](local-asr.md) · [Documentation index](README.md)

## Optional local models (Windows x64)

Model keys are persisted identifiers; the app displays the friendly names. Each family remembers its own on-demand model and device preference. Parakeet is the default backend for new installations on Windows x64; Local Whisper remains the default on the other platforms.

| Backend | Model | Catalog key | Parameters | Languages | Devices |
| --- | --- | --- | --- | --- | --- |
| Parakeet | Parakeet TDT 0.6B v3 | `parakeet-v3` | 600 million | 25 European languages | CPU / NVIDIA CUDA |
| Qwen3-ASR | Qwen3-ASR 0.6B | `qwen-0.6b` | 600 million | 30 languages and 22 Chinese dialects | CPU / NVIDIA CUDA |
| Qwen3-ASR | Qwen3-ASR 1.7B | `qwen-1.7b` | 1.7 billion | 30 languages and 22 Chinese dialects | CPU / NVIDIA CUDA |
| Nemotron Streaming | Nemotron 3.5 ASR 0.6B | `nemotron-3.5` | 600 million | Multilingual; coverage varies by locale | CPU / NVIDIA CUDA |
| Moonshine | Moonshine Streaming Small | `moonshine-small` | 123 million | English | CPU |
| Moonshine | Moonshine Streaming Medium | `moonshine-medium` | 245 million | English | CPU |

Parakeet supports timestamped meeting chunks. Nemotron and Moonshine also provide native live meeting previews. Qwen is available for dictation and uploads; this integration does not provide its forced alignment or vLLM streaming path. The original Whisper dictation preview remains specific to Whisper.

Download the selected weights and the matching optional runtime separately. Parakeet and Nemotron share NVIDIA Speech CPU/GPU components; Qwen uses one shared CPU/CUDA runtime; Moonshine uses its own CPU runtime. Runtime versions, sizes, weight licenses, and device tradeoffs are in [the installation guide](local-asr.md#download-and-disk-sizes).

## Local Whisper

Available through faster-whisper on the base application platforms. CUDA is supported on Windows/Linux; this integration does not supply Metal/MPS on macOS. `auto` resolves to Turbo on GPU and Base on CPU; it does not select another backend family.

| Model ID | Parameters | Languages | Approximate weight download |
| --- | --- | --- | --- |
| `tiny` | 39 million | Multilingual | ~76 MB |
| `tiny.en` | 39 million | English only | ~76 MB |
| `base` | 74 million | Multilingual | ~145 MB |
| `base.en` | 74 million | English only | ~145 MB |
| `small` | 244 million | Multilingual | ~484 MB |
| `small.en` | 244 million | English only | ~484 MB |
| `medium` | 769 million | Multilingual | ~1.5 GB |
| `medium.en` | 769 million | English only | ~1.5 GB |
| `large-v1` | 1.55 billion | Multilingual | ~3.1 GB |
| `large-v2` | 1.55 billion | Multilingual | ~3.1 GB |
| `large-v3` | 1.55 billion | Multilingual | ~3.1 GB |
| `turbo` | 809 million | Multilingual | ~1.6 GB |
| `distil-small.en` | 166 million | English only | ~330 MB |
| `distil-medium.en` | 394 million | English only | ~790 MB |
| `distil-large-v2` | 756 million | English only | ~1.5 GB |
| `distil-large-v3` | 756 million | English only | ~1.5 GB |

Whisper keeps its existing device and quantization controls. Standard, distilled, and Turbo checkpoints have different capabilities; consult the model profile in Downloads before choosing translation or multilingual workflows.

## API transcription

The Backend field shows **API**, with a separate Model selector. These choices require an API key and network access; they are not local model downloads.

| Display/model ID | Provider |
| --- | --- |
| `gpt-transcribe` | OpenAI |
| `gpt-4o-transcribe` | OpenAI |
| `gpt-4o-mini-transcribe` | OpenAI |
| `whisper-1` | OpenAI |

New API selections default to `gpt-transcribe`. API dictation/uploads use the existing upload-size splitting path. Meeting voice selection remains local and separate from cloud meeting intelligence.

## Selection and persistence

| Setting | Meaning |
| --- | --- |
| `selected_model` | Backend ID: `local_whisper`, `api`, `parakeet`, `qwen_asr`, `nemotron`, or `moonshine`. Unset or invalid values resolve to `config.DEFAULT_BACKEND`: `parakeet` on Windows x64, `local_whisper` elsewhere |
| `whisper_model`, `whisper_device`, `whisper_compute_type` | Existing on-demand Whisper controls |
| `local_asr_models` | Map from optional backend ID to its selected catalog key |
| `local_asr_devices` | Map from optional backend ID to `auto`, `cpu`, or `cuda`; Moonshine resolves to CPU |
| `local_asr_language` | Shared optional on-demand language preference: `en` or `auto`; Moonshine remains English |
| `meeting_asr_model` | Independent optional meeting model key |
| `meeting_whisper_model` | Existing independent Whisper meeting selection when no optional meeting model is selected |
| `meeting_language` | Meeting language preference |
| `api_transcription_model` | Model chosen within the API backend |

Optional meeting models use their family's device preference from On-demand voice. Shared → Runtime continues to own Whisper device/quantization settings. Voice model selection does not change transcript-cleanup, speaker-identification, or meeting-intelligence models.

## Maintaining this reference

Canonical identities and capabilities are in `config.py` and `services/local_asr/catalog.py`; profiles are in `services/model_catalog.py`; exact download artifacts and revisions are in `services/local_asr/models.json`. Follow [Contributing](../CONTRIBUTING.md#local-speech-backends-and-models) when updating any of them.
