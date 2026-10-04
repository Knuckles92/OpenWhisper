# macOS parity validation — October 3, 2026

This closes the gaps between the macOS and Windows builds in meeting AI, speech engines and updates, and records what was run on real hardware. It follows the [Parakeet MLX validation](parakeet-mlx-macos-validation-2026-10-03.md) on the same Mac. Synthetic speech from macOS `say` (Samantha, 165 words/minute) was used throughout: 9.3 s, plus a 55 s clip of six repetitions. These are functional checks, not accuracy benchmarks.

| Item | Value |
|---|---|
| Hardware | Mac17,4, Apple M5, 16 GiB, arm64 |
| OS | macOS 26.6.2 (25G83) |
| Source Python | 3.12.13 |
| Intel checks | x86_64 CPython 3.12.13 under Rosetta 2 |

Every install below used the production component installer (`install_component`) and model downloader in an isolated data root, and every transcription went through `LocalSpeechBackend` and the real worker process.

## Meeting agents

| Component | Result |
|---|---|
| Pi (`meeting-agent`, Apple Silicon) | Node 22.23.2 darwin-arm64 (OpenJS-signed, no quarantine) plus the existing platform-neutral `bundle.cjs`. Installed, passed `node --version`/`--check`, completed the sidecar hello and `initialize` (Pi 0.84.1). |
| Pi (Intel) | Node darwin-x64 installed and validated under Rosetta. |
| OpenCode SDK (Apple Silicon) | `build_component.py meeting-agent-opencode` built the payload and passed its offline SDK self-test (106 MB archive, 408 MB installed). The sidecar initialized, and `test_python_supervisor_with_packaged_sdk_all_meeting_passes` passed with the real payload. **Not offered in Downloads yet:** the archive must be uploaded under `component-opencode-2.0.18-1` and pinned, as on Windows and Linux. |

`tests/test_meeting_sidecar.py`, including the real Pi TypeScript integration suite, passes on macOS.

## Speech engines

| Engine | Runtime | Device | 9.3 s clip | 55 s clip |
|---|---|---|---|---|
| Moonshine Small | `asr-moonshine`, 14 wheels, 26 MB (macOS 15+) | CPU | 0.47 s | — |
| Qwen3-ASR 0.6B | `asr-qwen`, 93 wheels, 280 MB (torch 2.6.0) | Apple GPU (MPS) | 1.69 s | 8.1–8.4 s |
| Qwen3-ASR 0.6B | same | CPU | 1.64 s | 12.6 s |
| Nemotron 3.5 | `asr-nvidia-metal` (NeMo-Speech.cpp 0.1.0 Metal) | Apple GPU | 0.15 s | 1.16 s |
| Nemotron 3.5 | `asr-nvidia-cpu` | CPU | 0.50 s | 3.87 s |
| Parakeet TDT 0.6B v3 | `asr-nvidia-metal` | Apple GPU | — | 0.59 s |
| Parakeet TDT 0.6B v3 | `asr-nvidia-cpu` | CPU | — | 1.63 s |
| Nemotron 3.5 (Intel) | `asr-nvidia-cpu` (macos-x86_64) | CPU, Rosetta | 1.81 s | — |

Warm timings, second run. Within each engine, both devices produced identical text. Nemotron and Moonshine streaming (1 s pushes, then finish) also returned complete final transcripts.

Two failures were found and fixed:

1. **Qwen crashed on MPS.** torch 2.6's MPS matmul cannot broadcast grouped-query attention (16 query over 8 key/value heads) as the SDPA path requests, which aborts the worker with `LLVM ERROR: Failed to infer result type(s)`. Passing `attn_implementation` did not reach Qwen's sub-models; the worker now sets eager attention on every sub-config when the device is MPS.
2. **macOS 15 wheels.** moonshine-voice publishes only a `macosx_15_0_arm64` wheel. The runtime records `macos_min`, so Downloads and the download prompt hide it on macOS 14, and selecting Moonshine there explains the requirement.

## In-place updates

A frozen 2.6.13 build (`build_installer_macos.sh`: self-test, release health, signature and Mach-O audits passed) was copied to a scratch Applications folder. A 2.6.14 bundle (same build, version changed, re-signed ad hoc) was packed into a real UDZO DMG. All of this ran with a throwaway `HOME` and data directory.

- **Update:** `prepare_in_place_update` mounted the DMG, verified the bundle, and staged it beside the installed app. The staged 2.6.14 executable started as the helper and was ready in 1.1 s. After the staging process exited, the helper swapped the bundles with `renamex_np(RENAME_SWAP)` and started 2.6.14 with a health token. 2.6.14 acknowledged a healthy start 3.4 s later, and the previous bundle and transaction were removed, 11 s in all.
- **Rollback:** a 2.6.15 bundle with `QtWidgets` removed (re-signed so it passes verification) exited at startup. The helper swapped 2.6.14 back, relaunched it and left the failure message, which 2.6.14 consumed at startup.

Unit tests also cover the helper making no imports after the swap. That matters because the swap moves the frozen helper's own files.

## Not validated

- Notarization: the build and CI paths are in place but need a Developer ID certificate and App Store Connect API key, which were not available.
- An Intel DMG: `onnxruntime` 1.29.0 and `cryptography` 50.0.1 publish no macOS x86_64 wheels, so Intel Macs still run from source.
- Moonshine on macOS 14, Qwen3-ASR 1.7B, physical microphone capture, and update permission prompts on a Developer ID build.
