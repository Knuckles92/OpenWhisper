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

Additional Python distributions retain their upstream license files under
`_internal` (typically in `*.dist-info/licenses`, package-specific `LICENSE`
files, or both). Copyright and license terms belong to their respective
authors. This notice does not replace those terms.
## Optional speech runtimes and model weights

The additional Windows x64 backends are downloaded on demand; their model weights and SDKs are not part of the base application bundle. OpenWhisper's MIT license does not relicense these artifacts. Consult the linked upstream license and any notices retained in each downloaded archive before redistribution.

| Artifact | Upstream project / model card | License |
| --- | --- | --- |
| Native Parakeet/Nemotron runtime | [NVIDIA NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp) | Apache-2.0; bundled third-party components retain their own notices |
| Parakeet TDT 0.6B v3 weights | [NVIDIA Parakeet](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) | CC-BY-4.0 |
| Nemotron 3.5 ASR Streaming 0.6B weights | [NVIDIA Nemotron](https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b) | OpenMDW-1.1 |
| Qwen3-ASR runtime and 0.6B / 1.7B weights | [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR), [0.6B](https://huggingface.co/Qwen/Qwen3-ASR-0.6B), [1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) | Apache-2.0 |
| Moonshine runtime and English Small / Medium weights | [Moonshine](https://github.com/moonshine-ai/moonshine), [Small](https://huggingface.co/moonshine-ai/moonshine-streaming-small), [Medium](https://huggingface.co/moonshine-ai/moonshine-streaming-medium) | MIT |

Each optional runtime includes a Python embedded distribution under the Python Software Foundation license. Qwen also includes PyTorch, Transformers, and their pinned wheel dependencies; Moonshine includes its native SDK and wheel dependencies. Package license files remain in the extracted runtime (including distribution metadata where supplied). NVIDIA CUDA libraries and other native dependencies retain their respective upstream terms. Model attribution identifies NVIDIA, the Qwen team, and Moonshine AI above; OpenWhisper uses their inference artifacts without retraining them.

Exact versions, source URLs, hashes, and artifact sizes are recorded in [`services/local_asr/models.json`](services/local_asr/models.json) and the four `*runtime.json` manifests in that directory. Weight storage sizes and format choices are documented in [the local speech guide](docs/local-asr.md#download-and-disk-sizes).

The optional developer quality check uses a subset of [LibriSpeech](https://www.openslr.org/12), by Vassil Panayotov, Guoguo Chen, Daniel Povey, and Sanjeev Khudanpur, under CC BY 4.0, obtained via [Hugging Face's test subset](https://huggingface.co/datasets/hf-internal-testing/librispeech_asr_dummy). Its test preparation selects utterances and adds synthetic noise to a second copy. Those audio files are not bundled with the app. Benchmark provenance and limitations are recorded in [`docs/benchmarks/`](docs/benchmarks/).
