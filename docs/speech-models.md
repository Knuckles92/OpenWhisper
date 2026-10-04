# Speech model details

See the [speech model comparison](../README.md#speech-models) for supported backends, platforms, and workflows. Choose models and install their required runtimes in **Settings → Downloads**.

## Languages

Optional local engines offer English, Russian, Spanish, French, Portuguese, Mandarin, and **Auto** where supported. Parakeet and Orukeet omit Mandarin; Moonshine is English-only. Qwen supports all the listed presets. Nemotron includes Mandarin in its broader coverage tier, though accuracy may vary. **Auto** detects other languages supported by the selected model. Language selection transcribes speech; it does not translate it. Paired computers show the choices supported by the host engine.

## Downloads and platform notes

Model weights and optional runtimes are separate downloads. **Settings → Downloads** shows each model's details and required components and verifies component archives before installation.

On Apple Silicon, **Parakeet MLX** needs its runtime and roughly 2.5 GB of model weights. Choose **Auto** for the Apple GPU through Metal or **CPU** for the processor. Parakeet, Nemotron, Qwen3-ASR, and Moonshine also run on Apple Silicon; **Auto** uses the Apple GPU where supported. Moonshine requires macOS 15. Local Whisper uses the CPU on macOS; see [GPU acceleration](../README.md#gpu-acceleration) for Windows and Linux. Intel Macs running from source can use Parakeet and Nemotron on the CPU.

## Orukeet

Orukeet is an optional 25-language adaptation of NVIDIA Parakeet by Oruk AI, including Russian and English. Its r3 native Q8 weights are 714 MB and use the existing NVIDIA Speech runtime. It has been tested with the pinned Windows and Linux CPU/CUDA runtimes; Apple Silicon validation is pending. Orukeet is a community model, so evaluate it on your recordings. Its weights are licensed under CC BY-SA 4.0, including attribution and applicable ShareAlike terms; see the [publisher's notice](https://huggingface.co/oruk/orukeet/blob/main/NOTICE.md).

## Sources and integrity

**Downloads** displays the publisher, source host, license, selected version, and download checks. Optional speech weights and runtime archives are pinned by version, size, and SHA-256. Built-in Whisper aliases use pinned Hugging Face commits; custom sources retain the publisher-selected version. Integrity checks do not guarantee model or runtime security. Keep OpenWhisper and its runtimes updated, and review important transcripts for errors.

After installation, local transcription runs on this computer. Downloading connects to the listed source host. Cloud transcription, remote engines, and AI cleanup have their own network behavior.
