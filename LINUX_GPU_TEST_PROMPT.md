# Linux GPU verification — prompt for a remote agent

Copy everything below the line into the agent on the Ubuntu laptop.

**Before you send it:** the branch `installer-downloadable` is not on GitHub yet and
the changes are uncommitted locally, so a fresh clone will not contain them. Commit
and push first:

```bash
git add -A && git commit -m "feat: Linux CUDA support, slim GPU component, GPU bug fixes"
git push -u origin installer-downloadable
```

The repo is public, so the agent needs no credentials.

---

# Task: verify Linux NVIDIA GPU support for OpenWhisper

You are testing an unverified code path on real hardware. OpenWhisper is a PyQt6
desktop app that transcribes audio with faster-whisper / CTranslate2. Linux GPU
support was just written but has **never been run on a Linux machine with an
NVIDIA GPU** — that is what you are checking. Report what actually happens,
including partial or confusing results. Do not "fix" the code; observe and report.

## Background

CTranslate2 4.8 loads exactly one CUDA library by name at runtime:
`libcublas.so.12` (plus `libcuda.so.1` from the driver). It does **not** use cuDNN.
These come from pip wheels, not the CUDA Toolkit.

The mechanism under test: Linux has no equivalent of Windows'
`os.add_dll_directory`, because `LD_LIBRARY_PATH` is read by `ld.so` at process
start and cannot be changed from inside a running process. So `app_qt.py` preloads
the wheels' `.so` files with `ctypes.CDLL(path, mode=RTLD_GLOBAL)` before the model
loads, relying on glibc resolving CTranslate2's later `dlopen("libcublas.so.12")`
against the already-loaded object.

**That SONAME resolution is the single unproven assumption. If anything fails, it
is most likely this.**

This is a laptop, so note two things: if it has hybrid graphics (Intel iGPU +
NVIDIA dGPU / Optimus), say so and report whether the app lands on the dGPU. And
the default record hotkey is numpad `*`, which most laptops lack — use the
on-screen Start button, or remap in Settings → Hotkeys.

## Setup

```bash
nvidia-smi                       # must show a GPU and CUDA >= 12 (driver >= 525)
python3 --version                # 3.10-3.12
sudo apt install -y libportaudio2 espeak-ng

git clone https://github.com/Knuckles92/OpenWhisper.git ~/openwhisper-gputest
cd ~/openwhisper-gputest
git checkout installer-downloadable
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Confirm you are on the right commit before testing — `git log --oneline -1` should
mention Linux CUDA support. If `requirements-gpu.txt` still marks every wheel
`sys_platform == "win32"`, you have the wrong branch; stop and say so.

## Test 1 — does the GPU requirements file install anything on Linux?

Before this change it installed *nothing* on Linux (every wheel was marked
`sys_platform == "win32"`).

```bash
pip install -r requirements-gpu.txt
pip list | grep -i nvidia
ls venv/lib/python3.*/site-packages/nvidia/*/lib/
```

Expected: `nvidia-cublas-cu12`, `nvidia-cuda-nvrtc-cu12`,
`nvidia-cuda-runtime-cu12`, and `libcublas.so.12` present. There must be **no**
`nvidia-cudnn-cu12`. Report the exact list and versions.

## Test 2 — does the preload make cuBLAS resolvable by bare SONAME?

The core mechanism.

```bash
python - <<'PY'
import ctypes
try:
    ctypes.CDLL("libcublas.so.12"); print("BEFORE preload: resolved (only expected if system CUDA is installed)")
except OSError as e:
    print("BEFORE preload: not resolvable ->", e)

import app_qt   # runs _preload_cuda_libraries() at import
print("preloaded:", app_qt.CUDA_PRELOADED_LIBRARIES)

try:
    ctypes.CDLL("libcublas.so.12"); print("AFTER preload: RESOLVED BY SONAME  <-- the thing being tested")
except OSError as e:
    print("AFTER preload: STILL FAILS ->", e)
PY
```

**Report all four lines verbatim.** If "AFTER preload" fails, stop and report — the
approach is wrong and everything below will fail too.

## Test 3 — platform-specific behavior

```bash
python -c "
import app_qt
from services.components import gpu_runtime_available, available_component_ids, component_coordinator
print('gpu_runtime_available :', gpu_runtime_available())   # expect True
print('component ids         :', available_component_ids()) # expect ()  (Windows-only feature)
print('list_components       :', component_coordinator.list_components())  # expect ()
"
```

## Test 4 — real GPU transcription

Transcript *accuracy* does not matter; which device is used does.

```bash
espeak-ng -w /tmp/test.wav "The quick brown fox jumps over the lazy dog."
python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu')"

python - <<'PY'
import time, app_qt
from transcriber.local_backend import LocalWhisperBackend
t0 = time.perf_counter(); b = LocalWhisperBackend(model_name="tiny")
print(f"load: {time.perf_counter()-t0:.2f}s")
print("device_info    :", b.device_info)          # expect "tiny | cuda (float16)"
print("fallback_reason:", b.gpu_fallback_reason)  # expect None
t0 = time.perf_counter(); print("transcript:", repr(b.transcribe("/tmp/test.wav")))
print(f"transcribe: {time.perf_counter()-t0:.2f}s")
PY
```

Then repeat with `model_name="auto"`, which is what real users get — it should
resolve to the **turbo** model on GPU (~1.6 GB download, needs ~2 GB VRAM). Report
the VRAM figure from `nvidia-smi` while it runs. If VRAM is tight on this laptop,
say so rather than forcing it.

## Test 5 — negative test (important)

With the driver present but cuBLAS gone, the app must fall back to CPU and still
transcribe — not die with "model is not available".

```bash
pip uninstall -y nvidia-cublas-cu12
python - <<'PY'
import app_qt, ctranslate2
print("driver still reports devices:", ctranslate2.get_cuda_device_count())  # expect >= 1
from transcriber.local_backend import LocalWhisperBackend
b = LocalWhisperBackend(model_name="tiny")
print("device_info    :", b.device_info)          # expect "tiny | cpu (int8) - GPU unavailable, using CPU"
print("fallback_reason:", b.gpu_fallback_reason)  # expect a cuBLAS message
print("transcript     :", repr(b.transcribe("/tmp/test.wav")))  # must still produce text
PY
pip install -r requirements-gpu.txt   # restore
```

## Test 6 — the actual GUI

```bash
python app_qt.py        # then also try: ./scripts/ow
```

Check and report:

1. The app window opens and the system tray icon appears.
2. **Manage models** — the "Components" section must be **absent entirely** (no
   heading, no "GPU Acceleration" row). It is a Windows-only feature; previously
   Linux was shown a row offering a 1.4 GB download of Windows DLLs. Screenshot this.
3. Record something real — click **Start**, speak, click **Stop**. Confirm you get
   a transcript and that auto-paste works.
4. `grep -iE "cuda|preload|device" openwhisper.log | tail -20` — expect a
   `Preloaded N CUDA library/libraries` line and `device=cuda`.
5. Whether `./scripts/ow` (which exports `LD_LIBRARY_PATH`) behaves the same as
   `python app_qt.py`. Both must work.

Report any Qt/Wayland warnings even if the app still runs, and note whether you are
on Wayland or X11 (`echo $XDG_SESSION_TYPE`) — global hotkeys and the overlay
behave differently on Wayland.

## Test 7 — test suite on Linux

This suite has only ever run on Windows, so it may expose platform assumptions.

```bash
python -m pytest tests/ -q      # add QT_QPA_PLATFORM=offscreen if Qt tests misbehave
```

Expected ~294 passing. Report every failure with its full traceback and say whether
it looks platform-related or like a real bug.

## Report back

Per test: command, actual output, pass/fail. Then answer:

1. Did `libcublas.so.12` resolve by bare SONAME after the preload? (Test 2)
2. Did transcription actually run on `cuda`? (Test 4)
3. Did the CPU fallback work when cuBLAS was removed? (Test 5)
4. Was the Components section absent from Manage models? (Test 6)
5. Anything surprising, slow, or confusing — include it even if unsure it matters.

Include `nvidia-smi`, `ldd --version`, `echo $XDG_SESSION_TYPE`, the Python version,
and whether this is a hybrid-graphics laptop.
