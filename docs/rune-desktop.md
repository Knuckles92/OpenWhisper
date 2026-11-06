# Rune desktop preview

OpenWhisper has an optional native desktop interface built with
[Anomalyco Rune](https://github.com/anomalyco/rune), pinned to revision
`781e2b6d87cd5f32d6f4365956fad4baaf24ee50`. It uses Rune's renderer, layout,
text, springs, accessibility support, and native file dialogs.

The workspace includes Quick record, Audio files, Meetings, Walkthroughs,
History, and Settings, with light and dark appearances and a compact navigation
rail for narrow windows.

## Launch

Set up the existing [Python environment](../README.md#run-from-source), then
install a current stable Rust toolchain with Cargo. Close any running OpenWhisper
instance before launching:

```sh
python main.py --rune
```

The launcher builds the locked native dependencies, selects this Python
interpreter for the audio service, and opens the Rune window. The first build
takes longer. Later launches rebuild only changed source. If Cargo is unavailable
but a debug binary already exists, the launcher uses that binary.

On Debian/Ubuntu, native builds additionally need a C toolchain, `pkg-config`,
`libxkbcommon-dev`, `libwayland-dev`, and a Vulkan driver. The Linux file chooser
uses the desktop's XDG FileChooser portal; install a portal backend appropriate
for your desktop. OpenWhisper's existing audio/Qt dependencies are still required
by its Python service and advanced dialogs.

Direct Cargo launches select the checkout's `.venv` Python automatically. Set
`OPENWHISPER_PYTHON` to use another interpreter and `OPENWHISPER_ROOT` if the binary
is moved outside its source checkout. `OPENWHISPER_DATA_DIR` continues to select
the application data directory.

## Controls

- Quick record starts, stops, and cancels the existing dictation pipeline. Its
  level display uses microphone measurements; live preview appears on supported
  engines. Finished transcripts can be selected, copied, or exported as text.
- Audio files accepts a dropped file or the native file chooser. Selecting a file
  does not start transcription; choose **Transcribe file** when ready.
- Meetings starts and ends capture through the existing meeting runtime. Consent
  and platform prompts remain in the existing dialogs. **Open live dashboard**
  opens the full meeting dashboard in the browser.
- Walkthroughs starts/finishes a walkthrough and captures screenshots through
  the existing runtime. The current recording mode controls which actions are
  enabled.
- History searches the titles and previews of the 60 most recent transcripts,
  with dictation/file filters. Selecting an entry loads its full text for copy,
  export, and original-text comparison. The existing desktop retains full history
  search, playback, editing, and deletion.
- Settings saves cleanup, auto-paste, clipboard, and local-language preferences.
  Appearance changes also update the existing Qt dialogs and survive restart.
  **Advanced settings** and **Model downloads** open the existing controls for
  providers, devices, hotkeys, dictionary, actions, storage, and model management.

Ctrl/Cmd+1–5 opens the five workflow pages, Ctrl/Cmd+, opens Settings, and
Ctrl/Cmd+K opens and focuses history search. Escape dismisses messages. Buttons,
switches, file controls, and search support keyboard focus. Existing global
recording hotkeys continue to work through the Python service.

This is a source-only preview. The standard desktop launcher and packaged
releases keep the existing Qt interface. The web meeting dashboard is unchanged.
Saved meeting/walkthrough libraries and advanced operations still use the
existing windows; this preview does not replace every Qt dialog.

## Architecture

The native process starts `main.py --rune-backend` as a private child and exchanges
newline-delimited JSON over its pipes. It creates no network listener and does
not enable API/MCP access. Stdout is reserved before native library imports;
application logs go to stderr. Requests are bounded and actions/settings are
allowlisted in `services/rune_bridge.py`.

The Python process uses the normal application controller, data lease,
single-instance guard, and shutdown path. Its main window starts hidden. All
bridge commands execute through the existing Qt-thread relay, so UI callbacks
and model/recording state keep their current threading contracts. Native UI work
and bridge I/O run on separate threads. The bridge requests a snapshot every
700 ms; an unchanged snapshot does not redraw an idle Rune window. The recent
history list refreshes after persistence events and at most every five seconds
for changes made in the existing dialogs.

Closing the native process closes the pipe, allowing Python to stop its services
and drain persistence. Starting another instance while shutdown is finishing
continues to respect the existing single-instance/data guards.

## Preview and validation

Synthetic preview data never starts the Python service or opens user history:

```sh
cargo run --locked --manifest-path native/Cargo.toml -- --demo
cargo run --locked --manifest-path native/Cargo.toml -- --scene studio --snap studio.png
cargo run --locked --manifest-path native/Cargo.toml -- --scene dark --snap dark.png
cargo run --locked --manifest-path native/Cargo.toml -- --scene history --snap history.png
cargo run --locked --manifest-path native/Cargo.toml -- --scene studio --size 760x860 --snap compact.png
```

Other named scenes: `recording`, `transcript`, `files`, `meetings`, `walkthrough`,
and `settings`. Rune's `--tree` option writes inspectable layout/accessibility
data alongside a snapshot.

```sh
cargo test --locked --manifest-path native/Cargo.toml
cargo fmt --manifest-path native/Cargo.toml -- --check
python -m pytest tests/test_rune_bridge.py tests/test_rune_launcher.py tests/test_bootstrap.py
python -m ruff check --select F .
```

Headless layout tests and synthetic previews do not verify physical microphone
capture, real speech-model inference, system-audio capture, OS-specific dialogs,
or native installer packaging.
