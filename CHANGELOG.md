# Changelog

All notable changes to OpenWhisper will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Live host-only Pi activity strip** - The meeting dashboard shows what the agent is doing (thinking, writing, tools) as it happens, for every pass — cards, notes, polish, and consolidation. Ticks arrive on the existing WebSocket as ``agent_activity`` (not SSE), carry labels only, and are never sent to guests
- **Selectable post-meeting report views** - After a meeting ends, the dashboard offers Ribbon, Brief, and Signal views of the same meeting document. Settings checkboxes choose which views to show; only Ribbon trims the final consolidation prompt (timeline beats and polished minutes). The enabled set is stored on the meeting so History shows the tabs that session was recorded with
- **AI note taker** - Meeting Mode's dashboard gains a dedicated "Meeting Notes" section: a note-taker agent pass runs alongside the card checkpoints and writes flowing, timestamped, evidence-linked minutes blocks (short heading, prose body, meeting-clock stamp) that accumulate live on an auto-following notes page. Works with both agent cores (direct and Pi sidecar); tool calls are structurally gated to the notes card on both sides of the bridge. Every block is a first-class card item — participants can edit, confirm, pin, remove, and host-undo them, human edits stay protected from agent rewrites, and the final consolidation pass reconciles the page against the complete final recording (fixing blocks that later discussion superseded or clarified, and rebuilding the page after a transcript re-decode). Notes export to Markdown and appear in the fallback archive view
- **Meeting finalization status** - After capture ends, Meeting Mode keeps a persistent finalization result (running / completed / disabled / unavailable / failed) in the desktop tab and browser dashboard. The microphone and other tabs unlock once the transcript is durable; Open Dashboard stays available while optional cloud consolidation finishes or reports a non-modal outcome. No fabricated percent progress — running is indeterminate only
- **Meeting Mode** - Capture system audio and microphone with durable live ASR, crash recovery, recording playback, and a browser dashboard other participants can join over the LAN. Optional cloud intelligence tracks evidence-linked topics, summaries, key points, decisions, action items, risks, and questions. Finished sessions support full transcript history/search, audit and host undo, re-run insights, rename/delete, playback, and Markdown/JSON/plain-text export. The unpublished Meeting Intelligence Agent and Speaker Identification payloads are gated out of the component UI; the shipped Direct agent and Me/Others channel labels remain fully usable without them
- **Meeting spoken-language setting** - Meetings can keep automatic detection or pin a known language, avoiding repeated detection work and giving strongly accented or short speech an explicit fallback
- **Reproducible real-meeting ASR benchmark** - A resumable harness runs the production chunking and persistence paths against ten naturally occurring AMI meetings with manual references, strict time-constrained WER, overlap diagnostics, runtime factor, and an explicit release-quality gate

### Fixed
- **Final cloud insights could time out while the model was still working** - DeepSeek V4 Flash regularly spends 2–3 minutes thinking before the first tool call (138s and 178s successes against a 180s budget). The sidecar now requests OpenRouter reasoning tokens (``reasoning: true`` + ``thinkingFormat: openrouter``) and forwards Pi's documented ``session.subscribe`` events (including ``thinking_delta``) so a stall timer resets while the turn is alive, with a 15-minute hard wall. A healthy sidecar is no longer restarted on timeout.
- **Meeting Mode finalization checklist was crushed into overlapping rows** - The window kept its compact minimum height while the finalization card grew step rows and summary stats, so the checklist was handed less height than its text needs. Two causes: the window never asked for the extra room, and word-wrapped labels report a size hint for a guessed width, so Qt paid for the mismatch out of a sibling widget's height. Wrapped labels now report the height their text actually occupies, and the window raises its floor to fit the Meeting Mode page while that tab is selected, returning the borrowed height when the card clears. Step rows also get horizontal padding, so the status column no longer sits against the list border
- **Meeting Mode** - Agent ops whose evidence ids fail exact-match validation are no longer lost wholesale. Flash-class models sometimes reconstruct the 23-hex-character segment ids from memory instead of copying them (observed losing 28 of 29 consolidation ops in one benchmark run); both agent cores now repair a truncated or typo'd id to its unique citable match before validation, while invented ids still reject honestly. The system prompt also asks for small `patch_state` batches after repeated malformed-JSON tool calls on giant batches
- **Meeting Mode** - The offline re-decode no longer discards the live meeting's dashboard. Evidence anchors on *all* card items (proposed ones included) are remapped onto the new transcript via interval overlap, and only proposed items whose anchors all died are stripped — so the final consolidation pass reconciles grounded live content instead of rebuilding cards from scratch, which the product-package benchmark showed hallucinated names and counts. Note blocks also keep their heading and meeting-clock stamp when the note taker re-sends partial data on an update
- **Meeting Mode** - A malformed agent tool call now gets instructive feedback (re-emit as valid JSON, split long batches) instead of a bare parser error, so a large consolidation batch is recovered rather than dropped
- **Short live chunks lost linguistic context** - Each meeting channel now carries a bounded 50-word durable transcript prompt into its next Whisper decode. Across 8.16 hours of difficult natural meetings this improved strict tcWER on all ten, from 31.52% to 28.86% micro-average, with no throughput penalty
- **Rolling transcript revision could progressively erase words** - A revision window that bisected a long Whisper segment replaced the whole stored row with only its trailing suffix. Mutable windows now align to persisted segment boundaries; automatic rolling rewrites remain disabled because corpus evaluation also found meeting-dependent accuracy regressions
- **Meeting transcription could be marked complete before its segments were durable** - Segment persistence and chunk completion now commit atomically with stable retry-safe IDs. Failed or interrupted drains remain recoverable and never run final consolidation over an incomplete transcript
- **Meeting state could be broadcast before SQLite accepted it** - State patches now use persistence-first copy-on-write semantics; a failed transaction rejects the operation without advancing sequence numbers, mutating live state, or notifying clients
- **Meeting history silently truncated long transcripts** - Live and historical transcript reads now use deterministic keyset pagination, evidence links can hydrate individual scoped segments, and search opens the complete stored meeting
- **Meeting links could not be revoked** - Hosts can regenerate both capability links; old REST tokens stop working immediately and existing WebSockets are disconnected for re-authentication
- **Capture loss looked like silence forever** - A per-channel watchdog restarts failed streams and follows default microphone/system-audio changes without interrupting the healthy channel, while persisting a visible degradation status
- **Meeting deletion could orphan either database rows or recordings** - Spools are tombstoned before database deletion, restored on transaction failure, and purged only after the database commit succeeds

### Changed
- **Meeting dashboard header owns report view, Download, and Copy guest link** - After a meeting ends, Ribbon / Brief / Signal is a header dropdown (left of Download) instead of an in-page tab bar. Download sits next to it; the guest URL field is gone and hosts copy the invite with a single header button. History still keeps the view switcher and Download on the selected meeting's report
- **Meeting dashboard uses a three-column workspace** - Conversation, the live summary or finished report (with the host-only Pi activity strip on top), and Captured sit side by side at about 20 / 60 / 20 on a wider shell. The center column sets the page length; the side rails get their own scroller only when they outgrow the center, and they stay sticky in the viewport when the center is long. Narrow viewports stack summary first, then conversation, then captured
- **Voice and text models now share one Model Manager** - The manager has dedicated Voice and Text tabs, with a guided provider → model picker for cleanup chat models. Provider, catalog sorting, and active text-model selection moved out of Settings → Cleanup; that tab now focuses on cleanup behavior, prompts, and learned rules while showing the active text model as a read-only summary

## [2.1.1] - 2026-08-05

### Fixed
- **GPU component required an app restart to take effect** - Installing *GPU Acceleration* from the Model Manager only registered the CUDA DLL directory at the next startup, so the running session kept transcribing on the CPU with no way to use what was just downloaded. A successful install is now activated in the running process — Windows resolves DLL names fresh on every load attempt, so registering the directory mid-session is sufficient — and the whisper engine reloads automatically, so the first transcription after the download already runs on the GPU. The "Restart OpenWhisper" message remains only for the rare case where in-session activation fails
- **Device setting kept claiming CUDA after a GPU→CPU fallback** - When a GPU load fell back to the CPU, the main-window Device combo and the persisted setting still read `cuda`/`auto` while transcription actually ran on the CPU. The setting now reverts to `cpu` and the inline combos refresh to match; installing the GPU component moves a `cpu` device back to `auto` automatically so the download immediately restores acceleration
- **Fallback message named the symptom but not the fix** - The status line said the app was using the CPU without saying what would bring the GPU back. It now gives cause-specific guidance: install *GPU Acceleration* under *Manage models* (missing CUDA libraries, Windows), install `requirements-gpu.txt` (Linux), or pick a smaller model / int8 quantization (GPU out of memory)
- **Stale "GPU unavailable, using CPU" note survived a successful GPU reload** - The fallback state was never cleared on reload, so even after the cause was fixed and the model loaded on the GPU, the device readout kept reporting the old failure. Every model load now starts from a clean slate

## [2.1.0] - 2026-08-02

### Added
- **Linux NVIDIA GPU Acceleration** - `pip install -r requirements-gpu.txt` now installs the CUDA libraries on Linux as well as Windows (previously every wheel was marked `sys_platform == "win32"`, so the command silently installed nothing). Because `LD_LIBRARY_PATH` is read by `ld.so` at process start and cannot be changed from inside a running process, the libraries are preloaded with `RTLD_GLOBAL` at startup so CTranslate2's `dlopen("libcublas.so.12")` resolves to the already-loaded object; `scripts/openwhisper` also exports `LD_LIBRARY_PATH`. Requires an NVIDIA driver providing CUDA 12 (>= 525); no CUDA Toolkit needed

### Changed
- **GPUs without float16 support now prefer int8 over float32** - Pascal and older cards support neither float16 compute type, so they fell back to float32 and the auto-selected turbo model exhausted VRAM. The GPU fallback order now tries `int8_float32` before `float32`. Measured on a 4 GB GTX 1050 Ti: peak VRAM went from 3957/4096 MiB **with an out-of-memory failure** to 1377 MiB with headroom, so turbo now runs on the GPU there, and cold start fell from ~113 s to ~27 s. Cards that support float16 never reach this fallback and are unaffected
- **GPU component more than halved: ~1.4 GB → ~633 MB download** (2.08 GB → 959 MB installed). CTranslate2 4.8 never loads cuDNN — it has no import-table entry and no `LoadLibrary`/`dlopen` name string for it on either platform, and a GPU transcription with cuDNN fully removed loads zero cuDNN modules — so the ~740 MB cuDNN wheel has been dropped from the payload. Existing installs keep working and are *offered* the slimmer update rather than forced onto it, with the row stating how much disk it frees

- **Component catalog now ships in the application instead of being fetched** - It was requested from the project website with the in-app copy as a fallback, but the site serves its SPA shell for unknown paths, so the remote branch never once succeeded: every session paid a wasted request and logged a warning for what was normal operation. `_BUILTIN_CATALOG` is now the catalog. Its entries point at PyPI, which needs no hosting and cannot rot — a published wheel's URL and bytes are immutable, and the pinned SHA-256 is verified before extraction. The Install action can no longer be blocked by an unreachable catalog

### Fixed
- **No install was ever offered an update** - Update detection was suppressed whenever the catalog fetch had failed, which was always, so nobody with the older payload was offered the cuDNN-free one and everyone kept ~1 GB of unused libraries on disk. Removing the fetch removed the condition; a shipped-version difference now reaches the user as a choice
- **Existing CUDA setups shown as missing** - Working system, pip-wheel, and older bundled CUDA installations are detected as `Available — Existing setup`; installer upgrades also keep bundle-owned NVIDIA DLL directories registered instead of silently falling back to CPU
- **Local transcription failed on every attempt when CUDA libraries were missing** - CTranslate2 reports a CUDA device from the driver alone and resolves cuBLAS lazily on the first encoder pass, so on a machine with a driver but no CUDA libraries (no GPU component on Windows, no `requirements-gpu.txt` on Linux) the GPU model loaded successfully and then raised `Library cublas64_12.dll is not found or cannot be loaded` on every transcription. The libraries are now probed before the app commits to the GPU — about 50 ms once, nothing afterwards — so such a machine loads on the CPU with the same model, logs why, and shows `GPU unavailable, using CPU` in the device display. A GPU load that fails despite the probe also falls back rather than leaving the backend unavailable
- **GPU capability probe gave order-dependent answers** - The check required `cudnn64_9.dll`, which the ctranslate2 wheel ships as a 266 KB stub in a directory it registers with `os.add_dll_directory` at import time. The probe therefore always passed after ctranslate2 was imported and always failed before it, so a working cuBLAS-only CUDA setup could be reported as no GPU at all
- **Component version strings could disagree** - The in-app catalog and `scripts/build_component.py` derived the payload version independently, so an installed component could look outdated for no reason. Both now read one shared `GPU_COMPONENT_VERSION` constant
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
