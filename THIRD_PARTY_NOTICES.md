# Third-party notices

OpenWhisper includes third-party software. OpenWhisper's own source code is
provided under the MIT License in `LICENSE`; bundled dependencies remain under
their respective licenses.

The native Windows and Linux distributions include, among other dependencies:

- **PyQt6** — GNU General Public License v3 or a commercial Riverbank license.
  The redistributable build includes the wheel's license text at
  `_internal/third_party_licenses/PyQt6/LICENSE`.
- **Qt 6** — GNU Lesser General Public License v3 and other terms applicable to
  individual Qt modules. The redistributable build includes the wheel's
  license text at `_internal/third_party_licenses/Qt/LICENSE`.
- **Tabler Icons** — MIT License, Copyright (c) 2020-2026 Paweł Kuna. The
  icons and their license are in `ui_qt/assets/tabler`.
- **React and React DOM** — MIT License, built into the meeting dashboard
  bundle in `webui/dist`. Versions are pinned in `webui/package-lock.json`.
- **DM Sans and Instrument Serif fonts** — SIL Open Font License 1.1,
  Copyright 2014 The DM Sans Project Authors and Copyright 2022 The
  Instrument Serif Project Authors. Bundled into the meeting dashboard from
  the Fontsource packages; the license texts ship in `webui/dist/licenses`.

Additional Python distributions retain their upstream license files under
`_internal` (typically in `*.dist-info/licenses`, package-specific `LICENSE`
files, or both). Copyright and license terms belong to their respective
authors. This notice does not replace those terms.
## Optional speech runtimes and model weights

The native NVIDIA Speech runtimes are also available for Linux x86_64 (CPU and CUDA) and, in the unreleased source integration, Apple Silicon macOS (CPU). These are NeMo-Speech.cpp's own release archives; the Linux CUDA archive includes NVIDIA's CUDA runtime and cuBLAS libraries. Their upstream license files are retained in `nemo-speech/share/licenses`.

The additional Windows x64 backends are downloaded on demand; their model weights and SDKs are not part of the base application bundle. OpenWhisper's MIT license does not relicense these artifacts. Consult the linked upstream license and any notices retained in each downloaded archive before redistribution.

| Artifact | Upstream project / model card | License |
| --- | --- | --- |
| Native Parakeet/Nemotron runtime | [NVIDIA NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp) | Apache-2.0; bundled third-party components retain their own notices |
| Orukeet TDT 0.6B r3 Q8 weights | [Oruk AI Orukeet](https://huggingface.co/oruk/orukeet), adapted from NVIDIA Parakeet; [weight license and attribution](https://huggingface.co/oruk/orukeet/blob/main/NOTICE.md) | CC-BY-SA-4.0; retain attribution and applicable ShareAlike terms |
| Parakeet TDT 0.6B v3 weights | [NVIDIA Parakeet](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) | CC-BY-4.0 |
| Parakeet TDT 0.6B v3 MLX weights | [MLX Community conversion](https://huggingface.co/mlx-community/parakeet-tdt-0.6b-v3) | CC-BY-4.0 |
| Parakeet MLX runtime | [parakeet-mlx](https://github.com/senstella/parakeet-mlx), [Apple MLX](https://github.com/ml-explore/mlx) | Apache-2.0; MIT; dependency licenses retained in installed wheels |
| Nemotron 3.5 ASR Streaming 0.6B weights | [NVIDIA Nemotron](https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b) | OpenMDW-1.1 |
| Qwen3-ASR runtime and 0.6B / 1.7B weights | [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR), [0.6B](https://huggingface.co/Qwen/Qwen3-ASR-0.6B), [1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) | Apache-2.0 |
| Moonshine runtime and English Small / Medium weights | [Moonshine](https://github.com/moonshine-ai/moonshine), [Small](https://huggingface.co/moonshine-ai/moonshine-streaming-small), [Medium](https://huggingface.co/moonshine-ai/moonshine-streaming-medium) | MIT |

The Windows optional runtimes include a Python embedded distribution under the Python Software Foundation license. The Linux and macOS native runtimes use the app's Python interpreter. Qwen also includes PyTorch, Transformers, and their pinned wheel dependencies; Moonshine includes its native SDK and wheel dependencies. Package license files remain in the extracted runtime (including distribution metadata where supplied). NVIDIA CUDA libraries and other native dependencies retain their respective upstream terms. Model attribution identifies NVIDIA, Oruk AI, the Qwen team, and Moonshine AI above; OpenWhisper uses their inference artifacts without retraining them.

Exact versions, source URLs, hashes, and artifact sizes are recorded in [`services/local_asr/models.json`](services/local_asr/models.json) and the `*runtime.json` manifests in that directory. Weight storage sizes and format choices are documented under [Speech models](README.md#speech-models).

## Other components downloaded on demand

These are installed from **Downloads → Components**, or fetched the first time a feature needs them. Each download is pinned to an exact URL, size, and SHA-256 in [`services/components.py`](services/components.py). OpenWhisper keeps only the files it runs from the Node.js and NVIDIA archives (the `node` executable and the CUDA libraries), so those archives' license files are not copied; their terms are at the links below.

| Component | Artifact | Upstream | License |
| --- | --- | --- | --- |
| GPU Acceleration | NVIDIA cuBLAS, NVRTC, and CUDA runtime 12.9 libraries, from NVIDIA's PyPI wheels | [nvidia-cublas-cu12](https://pypi.org/project/nvidia-cublas-cu12/), [CUDA EULA](https://docs.nvidia.com/cuda/eula/index.html) | NVIDIA proprietary (CUDA Toolkit EULA); these libraries are listed as redistributable in its Attachment A |
| Meeting Intelligence Agent | Node.js 22 LTS runtime | [Node.js](https://nodejs.org), [license](https://github.com/nodejs/node/blob/main/LICENSE) | MIT; bundled libraries such as OpenSSL, libuv, and ICU retain their own licenses |
| Meeting Intelligence Agent | OpenWhisper's Pi sidecar (`bundle.cjs`), built from [`sidecar/`](sidecar/) | [Pi coding agent](https://github.com/earendil-works/pi) (`@earendil-works/pi-coding-agent`, `@earendil-works/pi-ai`), [TypeBox](https://github.com/sinclairzx81/typebox) | Pi and TypeBox: MIT. The bundle also contains their npm dependencies under MIT, Apache-2.0, BSD-3-Clause, ISC, BlueOak-1.0.0, and 0BSD; exact versions are in [`sidecar/package-lock.json`](sidecar/package-lock.json) |
| Speaker identification (Meeting Mode) | WeSpeaker ResNet34-LM ONNX model, trained on VoxCeleb2 | [Wespeaker/wespeaker-voxceleb-resnet34-LM](https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM), [WeSpeaker](https://github.com/wenet-e2e/wespeaker) | CC-BY-4.0; model by the WeSpeaker project |

Built-in Whisper aliases use the exact Hugging Face commits in
[`services/whisper_models.json`](services/whisper_models.json). Standard Whisper
and Distil-Whisper weights retain their upstream MIT licenses and attribution;
their CTranslate2 conversions come from the publishers linked in the model catalog.
Model version pins and checksums establish artifact identity, not a security certification.

The optional developer quality check uses a subset of [LibriSpeech](https://www.openslr.org/12), by Vassil Panayotov, Guoguo Chen, Daniel Povey, and Sanjeev Khudanpur, under CC BY 4.0, obtained via [Hugging Face's test subset](https://huggingface.co/datasets/hf-internal-testing/librispeech_asr_dummy). Its test preparation selects utterances and adds synthetic noise to a second copy. Those audio files are not bundled with the app. Benchmark provenance and limitations are recorded in [`docs/benchmarks/`](docs/benchmarks/).
