# Additional local speech engines

**Release status:** this guide documents the unreleased Windows backend expansion. The published 2.5.2 installers do not contain these additions. See the [complete model reference](models.md) for exact catalog and settings IDs.

OpenWhisper adds four optional local backends on **Windows x64**. Select them in the Backend field on Quick Record or Upload File, or in Model Manager → On-demand voice. Existing Whisper and API settings are preserved.

| Backend | Models | CPU | NVIDIA GPU | Meeting Mode |
| --- | --- | --- | --- | --- |
| Parakeet | TDT 0.6B v3 | Yes | Yes | Timestamped chunk transcription |
| Qwen3-ASR | 0.6B, 1.7B | Yes | Yes | Not offered in this integration |
| Nemotron Streaming | 3.5 ASR 0.6B | Yes | Yes | Native live preview and timestamped chunks |
| Moonshine | Streaming Small, Medium (English) | Yes | No | Native live preview and timestamped chunks |

These are additional voice recognizers. Text cleanup, speaker identification, and meeting intelligence keep their own selections. The optional runtimes are currently packaged for Windows x64; Whisper remains the local option on the other supported platforms.

## Install and select

1. Open **Model Manager → Downloads** (or **Get models and runtimes** beneath an optional backend).
2. Install the matching component and download a model from the speech catalog. Parakeet and Nemotron share the NVIDIA Speech runtime. CPU and GPU builds are separate components; install both to switch between them.
3. Select the backend, model, and device. **auto** chooses a detected NVIDIA GPU, using the installed CPU runtime when the native GPU component is absent. Explicit **cuda** reports a load failure instead of silently switching to CPU. Moonshine always uses CPU.
4. Leave **Language** on English for English dictation, or use Auto with Parakeet, Nemotron, or Qwen. The Moonshine models supplied here are English only.
5. For meetings, select a compatible model under **Model Manager → Meeting Mode → Voice**. This model selection is independent of on-demand voice. Optional meeting engines use the device preference configured for their family under On-demand voice.

An installed runtime can be activated in the running app. A missing model or runtime reports what to install, and model switches reload in the background. Runtime replacement and removal are refused during recording, transcription, or an active meeting. Canceling inference terminates its worker; the next job reloads through the normal readiness check.

### Download and disk sizes

Approximate decimal sizes, from the pinned artifact manifests. A runtime is installed once per component, independently of model weights. Installation temporarily needs space for downloaded archives, extracted files, and any previous installation kept for rollback.

| Component | Download | Installed runtime | Models it serves |
| --- | ---: | ---: | --- |
| NVIDIA Speech CPU | 16 MB | 49 MB | Parakeet, Nemotron |
| NVIDIA Speech GPU | 117 MB | 454 MB | Parakeet, Nemotron |
| Qwen3-ASR runtime | 2.79 GB | 5.38 GB | Both Qwen models, CPU or CUDA |
| Moonshine runtime | 42 MB | 103 MB | Both Moonshine models |

| Model | Weights download | Weight license |
| --- | ---: | --- |
| Parakeet TDT 0.6B v3 | 714 MB | CC-BY-4.0 |
| Qwen3-ASR 0.6B | 1.88 GB | Apache-2.0 |
| Qwen3-ASR 1.7B | 4.70 GB | Apache-2.0 |
| Nemotron 3.5 ASR 0.6B | 742 MB | OpenMDW-1.1 |
| Moonshine Streaming Small | 142 MB | MIT |
| Moonshine Streaming Medium | 269 MB | MIT |

Qwen shares one CUDA-capable PyTorch runtime for CPU and GPU, so selecting CPU does not reduce its runtime download. Its CPU path uses float32 and substantially more RAM than the native engines. The 1.7B model passed both CPU and GPU tests on the machine below, but the CPU run put noticeable pressure on Windows committed memory. Prefer the smaller model or GPU when memory is constrained. A working NVIDIA driver is required for CUDA; these components include their user-space libraries.

## Local measurements

Measured September 4, 2026 on Windows 11, Ryzen 7 9800X3D, approximately 48 GB RAM, RTX 2060 6 GB, driver 610.74, Python 3.12.10. No AI cleanup was applied. Each engine was loaded alone, and device selection was checked before accepting a result.

The table is the median of three warm calls transcribing the same **11-second** public [JFK sample distributed by NVIDIA](https://github.com/NVIDIA/NeMo-Speech.cpp/blob/v0.1.0/test_files/asr/wav/test/jfk.wav). Lower is faster. Calls include file decoding and worker IPC, but exclude loading weights, microphone post-roll, pasting, and cleanup. They do not measure full hotkey Stop-to-text latency.

| Model | CPU warm time | GPU warm time |
| --- | ---: | ---: |
| Whisper Base, int8 | 0.668 s | — |
| Whisper Turbo, float16 | — | 0.671 s |
| Parakeet v3 | 0.768 s | 0.066 s |
| Qwen 0.6B | 3.231 s | 0.766 s |
| Qwen 1.7B | 7.295 s | 0.818 s |
| Nemotron 3.5 | 0.958 s | 0.079 s |
| Moonshine Small | 0.703 s | — |
| Moonshine Medium | 0.968 s | — |

All eight models produced zero normalized word errors on that short clip; punctuation differed. This is a useful latency smoke test, not an accuracy ranking. The native NVIDIA models were particularly fast on this GPU, while Moonshine Small was close to the existing Base CPU baseline. Cold loading and the first decode are separately recorded in [the measured results](benchmarks/local-asr-windows-2026-09-04.json).

### Small clean/noisy quality check

A second check used 16 evenly spaced utterances from the public [LibriSpeech test subset](https://huggingface.co/datasets/hf-internal-testing/librispeech_asr_dummy): one speaker, 116.48 seconds, and 298 reference word tokens. Each utterance was also tested with deterministic white noise added at 10 dB whole-clip RMS SNR. The source [LibriSpeech corpus is CC BY 4.0](https://www.openslr.org/12).

The table reports corpus-weighted word error rate (WER); lower is better. Scoring lowercases and compares Unicode word tokens, ignoring punctuation. Abbreviation, spelling, and number-format differences still count. This is raw ASR before cleanup.

| Model | Clean WER | Added noise WER |
| --- | ---: | ---: |
| Whisper Base (CPU) | 10.4% | 24.8% |
| Whisper Turbo (GPU) | 5.7% | 10.4% |
| Parakeet v3 (GPU) | 4.7% | 11.1% |
| Qwen 0.6B (GPU) | 5.7% | 10.1% |
| Qwen 1.7B (GPU) | 6.4% | 6.0% |
| Nemotron 3.5 (GPU) | 6.0% | 16.8% |
| Moonshine Small (CPU) | 9.1% | 19.1% |
| Moonshine Medium (CPU) | 6.0% | 14.8% |

Parakeet had the fewest errors on the clean sample; Qwen 1.7B had the fewest under this synthetic noise condition. That supports trying Parakeet first for GPU dictation and keeping Qwen as an alternative for difficult audio. The sample is too small and narrow to establish a general quality ranking. It does not cover multiple accents, conversational overlap, microphone differences, or realistic meeting noise. [Exact counts and corpus identity](benchmarks/local-asr-corpus-2026-09-04.json) are recorded separately. Corpus timing was not controlled for other host workload, so the short-clip table above is the intended speed comparison.

### Reproduce a comparison

Install the models and runtime components to compare, then run from the repository:

```powershell
. .\venv\Scripts\Activate.ps1
python -m pip install psutil
python scripts/benchmark_local_asr.py audio.wav --reference reference.txt --output results.json
```

Use `--models base,turbo,parakeet-v3,moonshine-small` to limit the matrix and `--repeats 10` for more warm samples. The script never downloads models. `psutil` is a developer-only dependency; its recorded peak is sampled process RSS including the benchmark host, not model-only RAM or GPU VRAM. A three-sample p95 is descriptive and should not be treated as a stable tail-latency estimate.

For a corpus, `scripts/benchmark_local_asr_corpus.py manifest.json --output results.json` accepts a JSON object with a `clips` list. Each entry has `audio_path` (relative to the manifest), `reference`, and an optional `group`. It reports per-clip errors, corpus-weighted WER, and total decode time divided by audio duration, with one warm-up before measurement. `--cpu-only` forces CPU for the matrix.

To reproduce the public quality check, also install the optional developer reader and download the small dataset:

```powershell
. .\venv\Scripts\Activate.ps1
python -m pip install pyarrow
hf download hf-internal-testing/librispeech_asr_dummy --repo-type dataset --local-dir .tmp/asr-corpus
python scripts/prepare_local_asr_corpus.py .tmp/asr-corpus/clean/validation-00000-of-00001.parquet --output .tmp/asr-eval
python scripts/benchmark_local_asr_corpus.py .tmp/asr-eval/manifest.json --output corpus-results.json
```

## Runtime and inference behavior

The GUI imports only the lightweight adapter. Persistent embedded-Python workers own the native libraries and weights, using a serialized JSON-lines protocol. Qwen's PyTorch/Transformers packages remain outside the application's environment. The native C++ adapters use the release C ABI, and Moonshine uses its native streaming API. No compiler, Docker, WSL, vLLM, or service endpoint is required.

Pinned runtimes are NeMo-Speech.cpp 0.1.0, qwen-asr 0.0.6 with Torch 2.6.0+cu124 / Transformers 4.57.6, and moonshine-voice 0.1.5. Exact wheel URLs, revisions, sizes, and SHA-256 values live in `services/local_asr/*runtime.json` and `models.json`. Downloads verify size and hash before publishing a complete installation; canceled downloads retain resumable partial files. Runtime installation restores the previous installation if publishing fails and tolerates temporary Windows file locks.

Models are stored under `%LOCALAPPDATA%\OpenWhisper\speech-models`; optional runtimes use the existing Components directory. The existing ask/always/never model-download policy applies, and `HF_HUB_OFFLINE=1` is a hard override, including for Moonshine's non-Hub model host. Workers receive local paths with both Hub and Transformers offline modes enabled. Local ASR does not send audio to a service; separately enabled cloud cleanup or meeting intelligence still follows its existing settings.

Uploads are decoded in bounded windows with quiet cut points, so long files do not load all decoded audio into RAM. Native models return timestamps; Qwen returns text with coarse window timestamps and detects its generation limit rather than silently accepting truncated output. Qwen alignment and its vLLM streaming path are not included.

Nemotron and Moonshine meeting previews are temporary dashboard state. They update separately from saved segments, are cleared as durable chunks arrive, and never enter exports or trigger intelligence on their own. Capture feeds a bounded queue without blocking the audio callback. Pending durable transcription takes priority over previews. Stop flushes remaining preview audio; the normal final pass remains authoritative. Parakeet uses the existing chunk workflow. Whisper's existing dictation preview remains available when Whisper is selected.

The engine lease releases on-demand weights before meeting ASR starts, and restores them after meeting ASR releases its model, including post-meeting reprocessing. Model identity is preserved for recovery and refinalization. Tests cover switching, stale worker responses, cancellation, download integrity, runtime rollback, and preview reset behavior. Real checks additionally covered all six new models, CPU/GPU loading, silence, two-minute repeated speech, native stream finalization, and cancel/reload; observed cancellation was 13–166 ms on this machine.

## Upstream sources

- [NVIDIA NeMo-Speech.cpp runtime and supported models](https://github.com/NVIDIA/NeMo-Speech.cpp)
- [Parakeet TDT 0.6B v3 model card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)
- [Nemotron 3.5 ASR Streaming 0.6B model card](https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b)
- [Qwen3-ASR runtime documentation](https://github.com/QwenLM/Qwen3-ASR)
- [Moonshine native runtime](https://github.com/moonshine-ai/moonshine)
- [Moonshine Small model card](https://huggingface.co/moonshine-ai/moonshine-streaming-small) and [Medium model card](https://huggingface.co/moonshine-ai/moonshine-streaming-medium)
