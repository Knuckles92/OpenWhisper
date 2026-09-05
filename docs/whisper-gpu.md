# Whisper GPU acceleration

The additional Windows speech engines use their own [optional runtimes](local-asr.md#download-and-disk-sizes). This page covers **Local Whisper** (faster-whisper / CTranslate2) on Windows and Linux.

For everyday use, see the short [GPU note in the README](../README.md#gpu-acceleration). Native packages ship Whisper on CPU. On Windows, NVIDIA acceleration is an optional ~633 MB verified download from **Model Manager → Downloads → Components**. Linux packages do not install CUDA into the frozen runtime; use a source install plus `requirements-gpu.txt` below.

## Driver

**Prerequisite (Windows and Linux):** an NVIDIA driver providing CUDA 12 — version 525 or newer. The driver supplies `nvcuda.dll` / `libcuda.so.1` and never comes from pip. **You do not need the CUDA Toolkit installer.**

## Source install

Install the CUDA libraries faster-whisper's engine (CTranslate2) loads at runtime:

```bash
pip install -r requirements-gpu.txt
```

That pulls cuBLAS plus NVRTC and the CUDA 12 runtime — roughly 630 MB of wheels on Windows, a little more on Linux, and under 1 GB once installed. (The Windows component figures above are smaller because the component extracts only the DLLs, not the whole wheels.) CPU-only users should skip this file; transcription works without it. macOS users should skip it too: faster-whisper has no Metal/MPS backend, so transcription runs on CPU there.

**cuDNN is deliberately not installed.** CTranslate2 4.8 has no import-table entry and no `LoadLibrary`/`dlopen` name string for cuDNN on either platform, and a GPU transcription with cuDNN fully removed loads zero cuDNN modules — so the `nvidia-cudnn-cu12` wheel (737 MB on Windows, 799 MB on Linux) would be pure download weight.

## How the libraries are found

Linux has no mutable equivalent of the Windows DLL search path:

| Platform | Mechanism |
|----------|-----------|
| Windows | `app_qt._register_cuda_dll_directories()` registers the wheels' `bin` directories with `os.add_dll_directory` **and** prepends them to `PATH` — both are needed, because CTranslate2's loader ignores the former |
| Linux | `app_qt._preload_cuda_libraries()` loads the `.so` files with `RTLD_GLOBAL` before the model loads, so CTranslate2's later `dlopen("libcublas.so.12")` resolves to the already-loaded object. `LD_LIBRARY_PATH` is read by `ld.so` at process start and cannot be changed from inside a running process; `scripts/openwhisper` also exports it for good measure |

Either way, no PATH editing and no CUDA Toolkit. GPU auto-detection uses CTranslate2 directly, so **torch is not required**. With `device: auto`, the app detects the GPU and selects optimal settings (turbo model + float16 on GPU, base + int8 on CPU). If a GPU is detected but its libraries cannot be loaded, the model falls back to CPU with a warning in `openwhisper.log` rather than failing.

## Older GPUs (Pascal and earlier)

CTranslate2 does not support `float16` there, so `auto` falls back to `int8_float32` rather than `float32`. This matters more than it sounds: measured on a 4 GB GTX 1050 Ti, the auto-selected turbo model peaked at **3957/4096 MiB and ran out of memory** at `float32`, versus **1377 MiB with headroom to spare** at `int8_float32`. Turbo runs on the GPU on a 4 GB card as a result, and cold start dropped from ~113 s to ~27 s. Cards that support `float16` never reach this fallback and are unaffected. Verified with driver 580 / CUDA 13 — a newer driver than the CUDA 12 wheels, which is fine.

With CUDA enabled, faster-whisper runs 2-4x faster than CPU-only. Streaming transcription uses ~15-20% GPU vs 40-60% CPU.

To rebuild the Windows `gpu-accel` download after changing CUDA pins, see [Packaging](packaging.md#updating-the-gpu-component).
