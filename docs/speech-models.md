# Speech model details

See the [speech model comparison](../README.md#speech-models) for supported backends, platforms, and workflows. Choose models and install their required runtimes in **Settings → Downloads**.

## Languages

Optional local engines offer English, Russian, Spanish, French, Portuguese, Mandarin, and **Auto** where supported. Parakeet and Orukeet omit Mandarin; Moonshine and Parakeet Unified are English-only. Qwen supports all the listed presets. Nemotron includes Mandarin in its broader coverage tier, though accuracy may vary. **Auto** detects other languages supported by the selected model. Language selection transcribes speech; it does not translate it. Paired computers show the choices supported by the host engine.

## Downloads and platform notes

Model weights and optional runtimes are separate downloads. **Settings → Downloads** shows each model's details and required components and verifies component archives before installation.

On Apple Silicon, **Parakeet MLX** needs its runtime and roughly 2.5 GB of model weights. Choose **Auto** for the Apple GPU through Metal or **CPU** for the processor. Parakeet, Nemotron, Qwen3-ASR, and Moonshine also run on Apple Silicon; **Auto** uses the Apple GPU where supported. Moonshine requires macOS 15. Local Whisper uses the CPU on macOS; see [GPU acceleration](../README.md#gpu-acceleration) for Windows and Linux. Intel Macs running from source can use Parakeet and Nemotron on the CPU.

## Parakeet Unified

Parakeet Unified EN 0.6B is NVIDIA's English Parakeet trained to run both offline and as a stream. OpenWhisper runs it on [transcribe.cpp](https://github.com/handy-computer/transcribe.cpp), installed from **Downloads** as the transcribe.cpp runtime (about 30 MB). NVIDIA's own runtime has no Unified model. Its 731 MB Q8 weights come from [handy-computer's GGUF conversion](https://huggingface.co/handy-computer/parakeet-unified-en-0.6b-gguf). They are licensed under the [NVIDIA Open Model License](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/), and its text and NVIDIA's notice are saved beside them.

- **Device:** **Auto** uses a dedicated GPU from any vendor through Vulkan, else the CPU. Choosing the GPU also accepts an integrated one.
- **Live preview:** with live preview on, the preview follows the model's own stream instead of re-decoding 3-second windows. Words appear about 2 seconds into speaking, short dictations included. In a 2026-10-31 test on a GTX 1050 Ti, 16% of the words in its live text were wrong, against 55% for Parakeet v3's preview.
- **Final text:** the saved recording is decoded at the end, as with Parakeet v3. A dictation of 30 seconds or more pastes the stream's own final text instead, if the stream heard all of the recording. That text is ready about 0.3 seconds after you stop, however long you spoke.
- **Validated:** Windows x64 on the CPU and on an RTX 2060 through Vulkan, and Linux x86_64 on the CPU. A Linux GPU through Vulkan has not been validated with the release libraries. Meetings use other models for now.

## Orukeet

Orukeet is an optional 25-language adaptation of NVIDIA Parakeet by Oruk AI, including Russian and English. Its r3 native Q8 weights are 714 MB and use the existing NVIDIA Speech runtime. It has been tested with the pinned Windows and Linux CPU/CUDA runtimes; Apple Silicon validation is pending. Orukeet is a community model, so evaluate it on your recordings. Its weights are licensed under CC BY-SA 4.0, including attribution and applicable ShareAlike terms; see the [publisher's notice](https://huggingface.co/oruk/orukeet/blob/main/NOTICE.md).

## Sources and integrity

**Downloads** displays the publisher, source host, license, selected version, and download checks. Optional speech weights and runtime archives are pinned by version, size, and SHA-256. Built-in Whisper aliases use pinned Hugging Face commits; custom sources retain the publisher-selected version. Integrity checks do not guarantee model or runtime security. Keep OpenWhisper and its runtimes updated, and review important transcripts for errors.

After installation, local transcription runs on this computer. Downloading connects to the listed source host. API transcription (below), remote engines, and AI cleanup have their own network behavior; see [Network and privacy](network-and-privacy.md).

## API transcription servers

The **API** engine uploads each recording to a server that speaks OpenAI's `POST /audio/transcriptions`. Choose **API** as the recording engine on **Dictation → Voice model**, then pick a **Server** and **Model** there or in the main window. OpenAI stays the default, so existing settings keep sending audio where they did.

| Server | Preset address | Key | Models listed |
| --- | --- | --- | --- |
| OpenAI | `api.openai.com` | `OPENAI_API_KEY` | OpenAI's transcription models, with their retirement dates |
| Groq | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` | `whisper-large-v3-turbo` (default) and `whisper-large-v3` |
| Speaches | `http://127.0.0.1:8000/v1` | none | The speech models the server has downloaded |
| LocalAI | `http://127.0.0.1:8080/v1` | none | Installed models with the transcript capability |
| vLLM | `http://127.0.0.1:8000/v1` | none | The models `vllm serve` is running, for example `openai/whisper-large-v3-turbo` |
| whisper.cpp | `http://127.0.0.1:8080/v1` | none | None; the server ignores the model id |
| Custom server | your address | optional | What its `/models` route returns |

- **Edit server…** changes a self-hosted preset's or the Custom slot's address, API key variable and model id. An address without a path gets `/v1`; addresses with credentials or query parameters are refused. Speaches and vLLM share port 8000, and LocalAI and whisper.cpp share 8080, so change one when you run both.
- whisper.cpp's `whisper-server` answers on `/inference` unless it is started with `--inference-path /v1/audio/transcriptions`, so start it with that flag. It assumes English when no language is sent, so OpenWhisper sends `language: auto` when you have not chosen one.
- Dictionary words go as a `prompt` and the language as `language` to every server except OpenAI's `gpt-transcribe`, which takes `keywords` and `languages`. A request whose hints are rejected (HTTP 400 or 422) is retried once without them.
- Recordings over 23 MB are split into pieces and uploaded separately, on every server.
- Each upload waits up to 120 seconds for an answer from a hosted service (OpenAI, Groq, a Custom server elsewhere), and up to 600 seconds from a server you run (any self-hosted preset, or any address on this computer), which may load the model on the first request or run on a CPU. Cancel stops an upload at once.
- Audio sent to a server on `localhost` or `127.0.0.1` stays on this computer; anything else is uploaded to that host. The Server field and **Settings → App → API keys** name the destination and which key goes where.

Meeting speaker identification is separate: on-device labels (WeSpeaker) are the default, and the optional cloud pass always uses OpenAI's diarization model with the OpenAI key, whichever server dictation uses.
