# Rune desktop

OpenWhisper's native desktop uses [Anomalyco Rune](https://github.com/anomalyco/rune),
pinned to `781e2b6d87cd5f32d6f4365956fad4baaf24ee50`, with the existing Python
speech, meeting, automation, and persistence services. The native service does
not import Qt. The original Qt launcher remains available for compatibility.

## Run from source

Install Python 3.12, a current stable Rust toolchain, and the native service
dependencies in a virtual environment. An existing OpenWhisper environment also
works; the native service selects its Qt-free runtime before importing services.

```sh
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-native.txt
python main.py --rune
```

Close any existing OpenWhisper instance first. The launcher builds locked Rust
dependencies and passes its Python interpreter to the audio service. Later
launches rebuild changed source. Without Cargo, an existing debug binary can be
used. `python main.py` continues to start the Qt desktop and requires the full
`requirements.txt` environment.

Linux builds need a C toolchain, `pkg-config`, ALSA, xkbcommon, Wayland development
libraries, PortAudio, and a Vulkan driver. For Debian/Ubuntu:

```sh
sudo apt-get install build-essential pkg-config libasound2-dev libxkbcommon-dev libwayland-dev libportaudio2 libvulkan1 mesa-vulkan-drivers
```

The Linux file chooser uses the XDG FileChooser portal; install a backend for
your desktop. X11 clipboard exchange uses `xclip`; Wayland uses the bundled
native clipboard helper. Global shortcut and paste support depends on your
desktop/compositor permissions. Meeting system audio needs
[PulseAudio or PipeWire-Pulse](linux-system-audio.md). macOS requires microphone,
screen recording, and Accessibility permission for the corresponding features.

Direct Cargo launches find the checkout's `.venv` or `venv`. Override with
`OPENWHISPER_PYTHON`; set `OPENWHISPER_ROOT` when moving a source binary.
`OPENWHISPER_DATA_DIR` selects application data. Both desktops use the same
settings, history, meeting, and walkthrough formats; run one instance at a time.

## Native workspace

- **Quick record:** dictation, live levels/preview, cleaned/original text,
  profiles, actions, copy/export, global shortcuts, and a compact recording HUD
  that leaves the current app focused.
- **Audio files:** native file selection and drag/drop, multiple-file queues,
  ordering, related/independent files, combined output, and custom instructions.
- **Meetings:** capture, pause/resume, live transcript, saved search, participants,
  brief, evidence/cards, questions, insight review, corrections, tailored reports,
  and exports. The existing browser dashboard remains available.
- **Walkthroughs:** screenshots, note hold, pause, step ordering/removal, saved
  search, screenshot galleries, text review, write-ups, and PDF/DOCX/Markdown/ZIP
  exports. Native crop/annotation tools provide arrows, boxes, pen, highlight,
  labels, numbered steps, zoom, and undo/redo.
- **History:** full database search and pagination, transcript editing/deletion,
  retained-audio playback, original-text comparison, and TXT/Markdown/JSON/SRT.
- **Scratchpad:** editable dictation destination with atomic autosave to the
  existing scratchpad file. Insertion requires the scratchpad window to be
  active. Copy, export, and clear are available.
- **Settings:** typed preferences, models/components/downloads, microphones,
  providers/credentials, shortcut recording, dictionary/snippets, profiles,
  actions/transforms, app overrides, automation libraries, remote connections,
  backups, updates, usage, setup, and MCP controls. Credentials are write-only
  in the service protocol. Settings coverage is inventoried by
  `settings.coverage`; internal/legacy keys include an explicit reason.

Light/dark appearance and text scaling persist. The navigation rail collapses
at narrow widths. Ctrl/Cmd+1–5 opens the workflow pages, Ctrl/Cmd+, opens Settings,
and Ctrl/Cmd+K focuses history search. Global capture shortcuts retain their
existing press/release semantics. Closing the window respects the tray setting;
quitting during capture asks before discarding it.

## Standalone packages

The native package includes the Rust desktop, a frozen Qt-free Python service,
document fonts, and platform helpers. End users do not need Python or Rust.
Speech models and optional runtimes still download through model management.

From an environment containing `requirements-native.txt` and PyInstaller:

```sh
python -m pip install pyinstaller
python scripts/build_rune_desktop.py
```

Output is under `dist/rune/`: ZIP on Windows, tar.gz on Linux/macOS. Extract the
whole folder, then run `OpenWhisper.exe`, `OpenWhisper`, or `OpenWhisper.app`.
macOS builds are ad-hoc signed for testing; public distribution still needs
Developer ID signing/notarization. Packages built on Linux should target the
oldest supported distribution to keep glibc requirements compatible.

The [native CI workflow](../.github/workflows/rune-desktop.yml) builds on
Ubuntu 22.04, Windows 2022, and macOS 14. Each job installs an environment without
Qt, tests clipboard/runtime behavior, builds a package, relocates it to a path
with spaces, exercises the frozen service, and renders the native desktop.
Artifacts are retained for 14 days. Source-only packaging is also available
with `--source-only` and still requires a Python environment.

## Architecture and validation

The Rust desktop starts `main.py --rune-backend` or the packaged service as a
private child. Bounded, allowlisted JSON requests and asynchronous events use
stdin/stdout; logs go to stderr. This transport creates no network listener and
does not enable remote/MCP access. Separate reader/writer threads keep prompts
and background progress responsive. Unchanged snapshots avoid idle redraws.

`services.desktop_runtime` chooses either the original Qt classes or the native
owner-thread event loop, signals, timers, and workers. Native startup owns the
existing single-instance/data lease, controller, and shutdown sequence.
EOF drains persistence and stops services. Backup restore restarts the service
after the restore gate while retaining the native window. Document exports use
Pillow, ReportLab/HarfBuzz, Markdown, and python-docx, with bundled multilingual
fonts. See the [command reference](native-desktop-commands.md).

Synthetic scenes never start the service or access user history:

```sh
cargo run --locked --manifest-path native/Cargo.toml -- --demo
cargo run --locked --manifest-path native/Cargo.toml -- --scene studio --snap studio.png
cargo run --locked --manifest-path native/Cargo.toml -- --scene dark --size 760x860 --snap compact.png
cargo test --locked --manifest-path native/Cargo.toml
cargo fmt --manifest-path native/Cargo.toml -- --check
python -m pytest
python -m ruff check --select F .
python scripts/smoke_rune_package.py dist/rune/OpenWhisper-native-linux-x86_64.tar.gz
```

Scenes also include recording, transcript, files, meetings, walkthrough, history,
settings, settings-models, settings-keys, settings-remote, settings-backup,
settings-updates, settings-stats, settings-setup, settings-mcp,
settings-dictionary, and meeting-review. `--tree` emits accessibility/layout data.
Linux manual verification uses real pointer/keyboard input, X11 and headless
Wayland sessions, clipboard ownership, and a relocated frozen package. CI and
fixtures do not establish physical microphone quality, speech-model accuracy,
or every permission/compositor combination; those require testing on the target
desktop with its actual devices.
