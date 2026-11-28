# Speech model details

See the [speech model comparison](../README.md#speech-models) for supported backends, platforms, and workflows. Choose models and install their required runtimes in **Settings → Downloads**.

## Languages

Optional local engines offer English, Russian, Spanish, French, Portuguese, Mandarin, and **Auto** where supported. Parakeet and Orukeet omit Mandarin; Moonshine, Parakeet Unified, and Fourier 2B are English-only. Qwen3-ASR 0.6B and 1.7B support all the listed presets. Nemotron includes Mandarin in its broader coverage tier, though accuracy may vary. **Auto** detects other languages supported by the selected model. Language selection transcribes speech; it does not translate it. Paired computers show the choices supported by the host engine.

## Downloads and platform notes

Model weights and optional runtimes are separate downloads. **Settings → Downloads** shows each model's details and required components and verifies component archives before installation.

On Apple Silicon, **Parakeet MLX** needs its runtime and roughly 2.5 GB of model weights. Choose **Auto** for the Apple GPU through Metal or **CPU** for the processor. Parakeet, Nemotron, Qwen3-ASR, and Moonshine also run on Apple Silicon; **Auto** uses the Apple GPU where supported. Moonshine requires macOS 15. Local Whisper uses the CPU on macOS; see [GPU acceleration](../README.md#gpu-acceleration) for Windows and Linux. Intel Macs running from source can use Parakeet and Nemotron on the CPU.

## Parakeet Unified

Parakeet Unified is the default dictation engine on new Windows x64 and Linux x86_64 installs. OpenWhisper runs it on [transcribe.cpp](https://github.com/handy-computer/transcribe.cpp), which ships bundled inside the packaged Windows and Linux installers (source checkouts download it from **Downloads** as the transcribe.cpp runtime, about 30 MB). Non-English users can switch to a multilingual engine (**Parakeet TDT v3**, **Local Whisper**, or **Qwen3-ASR**) directly from Guided Setup or **Settings → Dictation → Voice model → Languages I dictate in**.

Two English models run on this engine:

- **Parakeet Unified EN 0.6B (731 MB Q8)** — default on standard installers; NVIDIA's unified streaming + offline model from [handy-computer's GGUF conversion](https://huggingface.co/handy-computer/parakeet-unified-en-0.6b-gguf). Licensed under the [NVIDIA Open Model License](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/), with its text and NVIDIA's notice saved beside the weights.
- **Parakeet TDT-CTC 110M (135 MB Q8)** — compact CC-BY-4.0 model from [handy-computer's GGUF conversion](https://huggingface.co/handy-computer/parakeet-tdt_ctc-110m-gguf). Included inside the optional `OpenWhisper-AllInOne` Windows and Linux installers so dictation, live preview, uploads, and meetings work completely offline on first launch with no model download. On an all-in-one install, OpenWhisper selects it automatically until you pick another model (such as upgrading to Parakeet Unified EN 0.6B).

- **Device:** **Auto** uses a dedicated GPU from any vendor through Vulkan, else the CPU. Choosing the GPU also accepts an integrated one.
- **Live preview:** On **Parakeet Unified EN 0.6B**, live preview follows the model's own stream instead of re-decoding 3-second windows. Words appear about 2 seconds into speaking, short dictations included. In a 2026-10-31 test on a GTX 1050 Ti, 16% of the words in its live text were wrong, against 55% for Parakeet v3's preview. On **Parakeet TDT-CTC 110M**, live preview re-decodes short windows with the same loaded model (no extra download).
- **Final text:** The saved recording is decoded at the end, as with Parakeet v3. On Parakeet Unified EN 0.6B, a dictation of 30 seconds or more pastes the stream's own final text instead, if the stream heard all of the recording (ready about 0.3 seconds after you stop).
- **Validated:** Windows x64 on the CPU and on an RTX 2060 through Vulkan, and Linux x86_64 on the CPU. A Linux GPU through Vulkan has not been validated with the release libraries. Parakeet TDT-CTC 110M also supports Meeting Mode chunks and preview; Parakeet Unified EN 0.6B is dictation/upload only for now.

## Qwen3-ASR

Windows x64 and Linux x86_64 use the same bundled transcribe.cpp runtime as Parakeet Unified. **Auto** uses a dedicated GPU through Vulkan when available and falls back to CPU. The Q8 GGUF weights are separate downloads of about 850 MB for 0.6B and 2.19 GB for 1.7B.

If you used Qwen3-ASR on Windows before this change, your model selection is kept. Open **Downloads** to get the GGUF weights for that model. You can also remove the old Qwen3-ASR PyTorch runtime there. On Apple Silicon, Qwen3-ASR uses the Safetensors weights and the optional PyTorch runtime for CPU or Apple GPU.

### Fourier 2B

Fourier 2B is an optional English fine-tune of Qwen3-ASR 1.7B by Oruk AI. Its pinned 8-bit GGUF weights (2.19 GB) run on the bundled transcribe.cpp runtime on Windows x64 and Linux x86_64 only, and its model card (`README.md`) downloads alongside the weights. Fourier 2B is a community model, so evaluate it on your recordings.

Fourier 2B by Oruk is licensed under CC BY-NC 4.0. Oruk permits personal, noncommercial use in OpenWhisper across devices you own, including Host Mode. Commercial use, commercial integration, and serving it to others require a separate license from Oruk; see the [publisher's model card and license](https://huggingface.co/oruk/oruk-fourier-2b/blob/8bcce21695e4ba13569c48de6cedffcb8a7576f5/README.md#license).

## Orukeet

Orukeet is an optional 25-language adaptation of NVIDIA Parakeet by Oruk AI, including Russian and English. Its r3 native Q8 weights are 714 MB and use the existing NVIDIA Speech runtime. It has been tested with the pinned Windows and Linux CPU/CUDA runtimes; Apple Silicon validation is pending. Orukeet is a community model, so evaluate it on your recordings. Its weights are licensed under CC BY-SA 4.0, including attribution and applicable ShareAlike terms; see the [publisher's notice](https://huggingface.co/oruk/orukeet/blob/main/NOTICE.md).

## Sources and integrity

**Downloads** displays the publisher, source host, license, selected version, and download checks. Optional speech weights and runtime archives are pinned by version, size, and SHA-256. Built-in Whisper aliases use pinned Hugging Face commits; custom sources retain the publisher-selected version. Integrity checks do not guarantee model or runtime security. Keep OpenWhisper and its runtimes updated, and review important transcripts for errors.

After installation, local transcription runs on this computer. Downloading connects to the listed source host. API transcription (below), remote engines, and AI cleanup have their own network behavior; see [Network and privacy](network-and-privacy.md).

## API transcription servers

The **API** engine uploads each recording to a server that speaks OpenAI's `POST /audio/transcriptions` or AssemblyAI's Dictation API (`POST /v1/transcribe`). Choose **API** as the recording engine on **Dictation → Voice model**, then pick a **Server** and **Model** there or in the main window. OpenAI stays the default, so existing settings keep sending audio where they did.

| Server | Preset address | Key | Models listed |
| --- | --- | --- | --- |
| OpenAI | `api.openai.com` | `OPENAI_API_KEY` | OpenAI's transcription models, with their retirement dates |
| Groq | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` | `whisper-large-v3-turbo` (default) and `whisper-large-v3` |
| AssemblyAI | `https://dictation.assemblyai.com/v1` | `ASSEMBLYAI_API_KEY` | `Cleaned (default)` (`llm_response`, falling back to `text`) and `Verbatim` (`text`) |
| Speaches | `http://127.0.0.1:8000/v1` | none | The speech models the server has downloaded |
| LocalAI | `http://127.0.0.1:8080/v1` | none | Installed models with the transcript capability |
| vLLM | `http://127.0.0.1:8000/v1` | none | The models `vllm serve` is running, for example `openai/whisper-large-v3-turbo` |
| whisper.cpp | `http://127.0.0.1:8080/v1` | none | None; the server ignores the model id |
| Custom server | your address | optional | What its `/models` route returns |

- **Edit server…** changes a self-hosted preset's or the Custom slot's address, API key variable and model id. An address without a path gets `/v1`; addresses with credentials or query parameters are refused. Speaches and vLLM share port 8000, and LocalAI and whisper.cpp share 8080, so change one when you run both.
- whisper.cpp's `whisper-server` answers on `/inference` unless it is started with `--inference-path /v1/audio/transcriptions`, so start it with that flag. It assumes English when no language is sent, so OpenWhisper sends `language: auto` when you have not chosen one.
- Dictionary words go as a `prompt` and the language as `language` to OpenAI-compatible servers, except OpenAI's `gpt-transcribe` (`keywords` and `languages`) and AssemblyAI's Dictation API (`keyterms_prompt` and `language_codes` in the JSON `config` part). A request whose hints are rejected (HTTP 400 or 422) is retried once without them.
- Recordings over 23 MB are split into pieces and uploaded separately, on every server.
- Each upload waits up to 120 seconds for an answer from a hosted service (OpenAI, Groq, AssemblyAI, a Custom server elsewhere), and up to 600 seconds from a server you run (any self-hosted preset, or any address on this computer), which may load the model on the first request or run on a CPU. Cancel stops an upload at once.
- Audio sent to a server on `localhost` or `127.0.0.1` stays on this computer; anything else is uploaded to that host. The Server field and **Settings → App → API keys** name the destination and which key goes where.

Meeting speaker identification is separate: on-device labels (WeSpeaker) are the default, and the optional cloud pass always uses OpenAI's diarization model with the OpenAI key, whichever server dictation uses.
