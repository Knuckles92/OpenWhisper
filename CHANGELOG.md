# Changelog

All notable changes to OpenWhisper will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Linux NVIDIA GPU Acceleration** - `pip install -r requirements-gpu.txt` now installs the CUDA libraries on Linux as well as Windows (previously every wheel was marked `sys_platform == "win32"`, so the command silently installed nothing). Because `LD_LIBRARY_PATH` is read by `ld.so` at process start and cannot be changed from inside a running process, the libraries are preloaded with `RTLD_GLOBAL` at startup so CTranslate2's `dlopen("libcublas.so.12")` resolves to the already-loaded object; `scripts/openwhisper` also exports `LD_LIBRARY_PATH`. Requires an NVIDIA driver providing CUDA 12 (>= 525); no CUDA Toolkit needed

### Changed
- **GPUs without float16 support now prefer int8 over float32** - Pascal and older cards support neither float16 compute type, so they fell back to float32, which roughly doubles the weights in VRAM — enough that the auto-selected turbo model exhausted a 4 GB card and dropped to CPU. The GPU fallback order now tries `int8_float32` before `float32`, trading a little accuracy for keeping transcription on the GPU. Cards that support float16 are unaffected
- **GPU component more than halved: ~1.4 GB → ~633 MB download** (2.08 GB → 959 MB installed). CTranslate2 4.8 never loads cuDNN — it has no import-table entry and no `LoadLibrary`/`dlopen` name string for it on either platform, and a GPU transcription with cuDNN fully removed loads zero cuDNN modules — so the ~740 MB cuDNN wheel has been dropped from the payload. Existing installs keep working and are *offered* the slimmer update rather than forced onto it, with the row stating how much disk it frees

### Fixed
- **GPU component installation unavailable** - Model Manager now falls back to a release-pinned, SHA-256-verified set of NVIDIA CUDA wheels when the remote component catalog is unavailable or returns the website HTML shell, so the Install action remains usable
- **Existing CUDA setups shown as missing** - Working system, pip-wheel, and older bundled CUDA installations are detected as `Available — Existing setup`; installer upgrades also keep bundle-owned NVIDIA DLL directories registered instead of silently falling back to CPU
- **Local transcription failed on every attempt when CUDA libraries were missing** - CTranslate2 reports a CUDA device from the driver alone and resolves cuBLAS lazily on the first encoder pass, so on a machine with a driver but no CUDA libraries (no GPU component on Windows, no `requirements-gpu.txt` on Linux) the GPU model loaded successfully and then raised `Library cublas64_12.dll is not found or cannot be loaded` on every transcription. The libraries are now probed before the device is chosen — about 50 ms once, nothing afterwards — so such a machine loads on the CPU with the same model, logs why, and shows `GPU unavailable, using CPU` in the device display. A GPU load that fails despite the probe also falls back rather than leaving the backend unavailable
- **GPU capability probe gave order-dependent answers** - The check required `cudnn64_9.dll`, which the ctranslate2 wheel ships as a 266 KB stub in a directory it registers with `os.add_dll_directory` at import time. The probe therefore always passed after ctranslate2 was imported and always failed before it, so a working cuBLAS-only CUDA setup could be reported as no GPU at all
- **Spurious "Update available" while offline** - The built-in fallback catalog published a different version string than `scripts/build_component.py` emitted, so every unreachable-catalog fetch made an installed component look outdated. Both now read one shared constant, and an update is never offered from a fallback catalog
- **Components section offered a Windows payload on Linux and macOS** - The section is now hidden where no component can be activated
- **Linux CUDA preload was never logged** - The startup summary looked up `sys.modules["app_qt"]`, but `python app_qt.py` registers the entry module as `__main__`, so neither the success nor the "no CUDA libraries" message ever appeared in a real launch. Found during Linux hardware verification
- **GPU out-of-memory reported as missing libraries** - A GPU load that failed for lack of VRAM told the user to install CUDA packages they already had, and the device display read `GPU unavailable` even though the GPU was found and working. The cause is now classified, with distinct advice for exhausted memory, absent libraries, and unrecognized failures
- **Remove button hidden on components with a pending update** - Updating is the user's choice, so removing the component outright stays available

## [2.0.0] - 2026-07-31

### Added
- **Windows Installer** - First binary distribution. `OpenWhisper-Setup-2.0.0.exe` installs per-user to `%LOCALAPPDATA%\Programs\OpenWhisper` with no admin rights and no UAC prompt; Start Menu, optional desktop, and optional sign-in-startup shortcuts; proper Add/Remove Programs registration. Built with PyInstaller (onedir) plus Inno Setup via `scripts\build_installer.ps1`. Not yet code-signed, so SmartScreen warns on first run
- **Downloadable Components** - Optional add-ons are fetched on demand instead of shipping in the installer, keeping the download near 135 MB rather than over 1 GB. **GPU Acceleration** (NVIDIA cuBLAS + cuDNN, ~2 GB) is the first component, installed from *Manage models → Components* with determinate progress, cancel, and removal. Downloads resume from a partial file, verify SHA-256 before use, and are committed with two atomic renames so an interruption leaves either the old install or the new one — never a mixture. Components live in `%LOCALAPPDATA%\OpenWhisper\components`, outside the install directory, so upgrading the app does not delete them
- **Application Icon** - A real multi-resolution icon (16-256 px) for the executable, taskbar, window, and system tray, rendered from the same microphone mark as the loading screen. Small sizes use a simplified solid glyph so the shape still reads at 16 px. Regenerate with `python scripts/generate_icon.py`
- **Project website** - [openwhisper.fiorilabs.tech](https://openwhisper.fiorilabs.tech/)
- **Model Technical Profiles** - Model Manager tiles now open bundled, offline technical profiles with model origin, practical guidance, specifications, limitations, and explicit links to the conversion and upstream model pages
- **Explicit Hugging Face Download Consent** - Model loading is now cache-first: cached models always load locally with zero network checks. A missing model triggers a consent dialog (Download once / Always allow / Cancel) governed by a three-value policy in Settings → Advanced (`ask`/`always`/`never`). The legacy fully-offline toggle migrates automatically (`true`→`never`, otherwise `ask`); `HF_HUB_OFFLINE=1` in the environment remains a hard override that disables downloads entirely
- **Fully Offline Setting** - Settings → Advanced toggle to skip HuggingFace Hub metadata checks on startup (same effect as `HF_HUB_OFFLINE=1`, without needing an environment variable); superseded in this cycle by the download-consent policy above
- **Cross-Platform Support** - macOS fork merged into a single codebase: Carbon global hotkeys, Accessibility trust handling for auto-paste, persistent overlay visibility, platform-specific default hotkeys
- **Minimize-to-Tray Hotkey** - `Ctrl+Alt+M` global shortcut
- **CLI Launchers** - `ow`/`openwhisper` commands with PATH installer scripts
- **Database-Backed History** - SQLite (SQLAlchemy) persistence replaces flat JSON history files, with one-time automatic migration
- **Faster Startup** - Startup profiling and lazy imports
- **Streaming Tiny-Model Option** - Dedicated tiny model for real-time streaming transcription
- **Collapsible UI Sections** - Collapsible transcription panel and section headers with smooth window resizing
- **Inline Local-Engine Controls** - Model/device/quantization controls in the main window with debounced engine reloads
- **Hotkey Watchdog** - Detects sleep/resume gaps and re-registers keyboard hooks automatically
- **History Search** - Debounced search box filtering transcription history by text or timestamp

### Fixed
- **Cleanup model dropdown type-to-filter** - Settings → Cleanup → General model picker now filters its own dropdown as you type (case-insensitive substring match) instead of appending characters to the current model id with no filtering
- **GPU transcription "cublas64_12.dll is not found" on Windows** - CTranslate2 loads CUDA libraries via `LoadLibrary`, which consults `PATH`, but the DLL dirs were only registered with `os.add_dll_directory` (ignored by that loader). Startup now also prepends the NVIDIA wheel `bin` dirs to `PATH`.
- **GPU never auto-detected** - Hardware detection used `import torch`, which is not a dependency, so `device: auto` always fell back to CPU on GPU machines. Detection now uses CTranslate2's `get_cuda_device_count()`.

### Added
- **`requirements-gpu.txt`** - Opt-in NVIDIA CUDA wheels (cuDNN 9, cuBLAS, CUDA 12 runtime) so GPU acceleration works without installing the CUDA Toolkit.

### Changed
- **User data location in installed builds** - Settings, the history database, logs, and saved recordings resolve to `%LOCALAPPDATA%\OpenWhisper` when running from the installer, since the install directory must be treated as read-only. Running from source is unchanged: paths stay relative to the working directory
- **`format_size_bytes` moved to `services/format_utils.py`** - It is generic display formatting, not Hugging Face logic, and its previous home forced unrelated modules to import `services.hf_access` just to render a size. Still re-exported from `services.hf_access` so existing imports keep working
- **Stylesheet asset paths are resolved absolutely** - Qt resolves relative `url()` references in a stylesheet against the process working directory, so the check-mark icon only loaded when the app was launched from the repository root. Paths are now made absolute at load time, and a missing stylesheet is logged instead of failing silently
- **"Transcription Engine" naming** - The main-window backend picker is now labeled "Transcription Engine" (was "Transcription Model"), reserving the word "model" for actual models (local Whisper checkpoints, cleanup chat models)
- **History Sidebar Redesign** - Single animation clock drives both the sidebar and window resize in lockstep (no more main-content wobble), fixed-width content is clipped instead of re-laid-out every frame, content populates before the first expand (no pop-in), section headers show counts, history cards show a model badge, and both sections share one scroll area
- Explicit overlay state routing via `OverlayState` enum and naming standardization
- Centralized module-level logging across services and UI
- Default hotkeys are numpad-aware on Windows/Linux (`kp *`, `kp -`)

### Removed
- **scipy dependency** - It was pulled in for a single `signal.resample` call that downsamples streaming preview chunks to 16 kHz, at a cost of ~110 MB installed. Replaced with an equivalent FFT resampler built on numpy (`services.streaming_transcriber.fft_resample`), verified to match `scipy.signal.resample` to float32 precision across up-, down-, and equal-length cases
- Experimental Meeting Mode and meeting insights (added and removed during this cycle; never in a tagged release)
- **Duplicate model controls in Settings** - "Default Model" (General tab) and the Whisper Model/Device/Compute combos (Advanced tab) duplicated the main window's engine dropdown and inline Engine Settings panel while writing the same settings keys; each choice now has a single home (engine dropdown; Engine Settings panel / Model Manager)
- Experimental live typing into the focused window (settings toggle and keystroke injection)

## [1.0.0] - 2026-01-10

### Added
- **Real-time Streaming Transcription** - Live text preview while recording with draggable overlay
- **Caret Paste Indicator** - Visual feedback showing where text will be pasted
- **Dynamic Streaming Settings** - Reconfigure streaming behavior without restart
- **Enhanced Crash Diagnostics** - Improved logging with Qt message handling for debugging
- **Window Geometry Persistence** - App remembers size and position between sessions
- **Audio Input Device Selection** - Choose your preferred microphone from settings

### Fixed
- Window vertical resizing not working properly
- Numpad hotkeys now correctly distinguished from regular number keys(Thanks meonester)
- Crashes on workstations without GPU or unsupported compute configurations
- Various stability improvements for CPU-only systems

### Changed
- Optimized CUDA/GPU detection and fallback behavior
- Improved model benchmark tooling
- Updated Python 3.12 recommendation for best compatibility
