# Contributing

## Pull requests

- Branch from `main` and keep each pull request to one topic. Split unrelated fixes, refactors, and dependency updates into their own pull requests.
- For a larger feature or a new dependency, open an issue or a discussion first so the approach can be agreed before you build it.
- Run the tests for the code you touched and `ruff check --select F .` (see [Validation](#validation)). UI changes need both interfaces checked (see [Desktop feature parity](#desktop-feature-parity)) and before/after screenshots in the pull request.
- Add a user-visible change to `CHANGELOG.md` under `## [Unreleased]`, in the matching `### Added`, `### Changed`, or `### Fixed` group. Follow the existing entries: a bold summary, then what changed for the user and any limits. Internal refactors and test-only changes need no entry.
- Update the README or the page in `docs/` that describes the behavior you changed.
- Keep API keys, tokens, private hostnames, and transcript content out of code, tests, logs, and screenshots.
- Report security problems privately; see [SECURITY.md](SECURITY.md).

By contributing, you agree that your contribution is licensed under the project's [MIT License](LICENSE).

## Comments and Docstrings

Code is the primary source of truth. Add prose only when it records information the code cannot express clearly.

- Keep non-obvious rationale, invariants, concurrency or lifetime rules, security boundaries, platform constraints, compatibility contracts, and measured choices.
- Do not narrate the next statement, restate a symbol's name or signature, add decorative section banners, leave commented-out code, or preserve implementation history that belongs in version control.
- Public APIs need docstrings only when they have a meaningful contract beyond their typed signature. Keep them concise and use Google style when parameter, return, or exception details add information.
- Tests should explain only non-obvious setup or why a behavior matters. Let test names and assertions describe ordinary cases.
- Preserve required directives, licenses, shebangs, active TODOs, and user-facing or model-facing strings.
- Update or remove nearby comments whenever behavior changes. Review comments for future staleness with the same care as code.

Before submitting a change, remove temporary notes and commented-out experiments, then verify that every remaining comment explains why rather than what.

## Desktop feature parity

The main desktop and Omarchy interfaces must offer the same features and actions.
Keep feature logic and controls shared; Omarchy-specific changes should adapt
styling, layout, and desktop integration. Verify new or changed controls with
`OPENWHISPER_UI=classic` and `OPENWHISPER_UI=omarchy`, including narrow tiled
windows, and cover both modes in the relevant UI regression tests.

## Validation

Use `uv run` as described below, or activate the repository virtual environment
before Python commands. Run tests through pytest so the shared fixtures isolate
settings, manage Qt, and handle
native-runtime teardown:

```powershell
. .\venv\Scripts\Activate.ps1
python -m pytest tests/ -n auto
python -m pytest tests/test_recorder.py tests/test_settings.py
```

`-n auto` (pytest-xdist, from `requirements-build.txt`) runs the full suite on
every core: about 1.5 minutes instead of 7 on an 8-core desktop. Leave it off
for a few files, where starting the workers takes longer than the tests.

CI checks Python correctness and unused code with Ruff 0.16.4:

```powershell
python -m pip install ruff==0.16.4
python -m ruff check --select F .
```

Keep imports that perform required runtime initialization or verify optional
libraries, with a specific `noqa: F401` explanation. Avoid broad automatic fixes
for typing or import order as part of behavior changes.

Dashboard changes also require `npm test` in `webui` before the locked build
below. It covers transcript loading, action acknowledgements, and speech previews.

## Development with uv

[Install uv](https://docs.astral.sh/uv/getting-started/installation/) once. On Windows:

```powershell
winget install --id astral-sh.uv -e
```

Open a new terminal after installation. From the repository root:

```powershell
uv sync --locked
uv run python main.py
uv run python -m pytest tests/ -n auto
uv run ruff check --select F .
```

No activation is needed. uv uses Python 3.12 (matching the release builders),
creates `.venv`, and installs the locked app dependencies, pytest, Ruff, and build
tools. It can download Python 3.12 if it is missing. An existing `venv` is retained;
the `ow`/`openwhisper` launchers and installer scripts still use that environment.
Use `uv run python main.py` when you want the uv development environment. On
macOS, the existing source launcher also handles app-bundle identification for
Accessibility permissions; use the documented pip/launcher workflow when testing
that integration. Linux still needs the audio and Qt system libraries described
in the README.

On Windows, install **GPU Acceleration** through **Downloads → Components** in the
app. Source launches activate installed components automatically, so the normal
`uv run python main.py` command supports GPU use without an extra flag. Other
speech runtimes and downloaded models also use the app's component manager.

The `gpu` extra is an alternative way to put CUDA wheels in `.venv`, intended for
Linux source installs (the downloadable GPU component is Windows-only) or for
explicit testing of wheel-based GPU setups:

```powershell
uv sync --locked --extra gpu
uv run --extra gpu python main.py
```

Keep `--extra gpu` on sync/run commands only when choosing that wheel-based setup.
Point your editor at `.venv/Scripts/python.exe` on Windows or `.venv/bin/python`
on macOS/Linux. JavaScript work in `webui` and the sidecars retains its existing
Node/Bun workflow.

### Updating dependencies

The existing requirements files remain the source of truth for app, GPU, build,
and release dependencies. `scripts/sync_uv_dependencies.py` mirrors them into the
marked block in `pyproject.toml`, including release constraints, so development
does not silently upgrade packages used by the installers. Edit the appropriate
requirements file first, then run:

```powershell
python scripts/sync_uv_dependencies.py
uv lock
uv sync --locked
python scripts/sync_uv_dependencies.py --check
uv lock --check
```

Commit the requirements changes, generated `pyproject.toml` block, and `uv.lock`
together. CI checks that the generated declarations match the requirements files
and that the lockfile is current.
Use `python3` for the standalone maintenance script if that is your platform's
Python command. It uses only the standard library and does not require activation.
Release pins should only change as part of a deliberate dependency update. Use
`uv add` only for experiments: permanent dependency changes must also go through
the requirements files and generator. uv itself is optional for source users and
is not required by the release builders.

## Adding a provider or engine

Most new integrations are an OpenAI-compatible endpoint, so start from the closest existing one and follow the same files.

**Text model providers** (AI cleanup, Command Mode, transforms, meeting intelligence, and Fast judgments on a text model):

- Built-in endpoints are the `TextLLMProfile` entries returned by `builtin_profiles()` in `services/text_llm.py`. Anything a user can reach with a custom endpoint does not need one; add a built-in profile when the provider needs a known address, key name, headers, or model list.
- **A local inference server** is one entry in `LOCAL_SERVER_DEFAULT_URLS` (its documented default address, ending in `/v1`) and `LOCAL_SERVER_NAMES`; `builtin_profiles()` builds the profile from them, and the user's address and optional key variable are saved as `<id>_base_url` and `<id>_api_key_env`. Being in `LOCAL_SERVER_PROFILE_IDS` makes the profile `self_hosted`: the 120 s cleanup timeout (`services/transcript_cleanup.py`) and model limits from what the server reports (`model_spec()` in `services/text_model_catalog.py`). Like every provider outside `HOSTED_CATALOG_PROFILE_IDS`, it keeps the caller's sampling options and gets no forced `max_tokens` (`generate()` in `services/text_generation.py`).
- Teach `services/local_text_servers.py` about the server: a handler in `_DESCRIBE` that reads its model routes into `TextModelSpec`s (context size, tools, thinking, and `thinking_levels` for the `reasoning_effort` values it accepts; return `None` for embedding, speech and rerank models so they stay out of the chat list), and a check in `_identify` using a route that needs no key, so **Running** detection and the "answering at another address" hint work. Write down the upstream routes you relied on in the module docstring, with the date you checked them.
- **A hosted provider** names the environment variable that holds its key (`api_key_env`), which **Settings → App → API keys** stores in the OS credential store. If its request shape is reviewed per model, add it to `HOSTED_CATALOG_PROFILE_IDS` and its models to `_ROUTES` in `services/text_model_catalog.py`; the catalog then decides sampling, reasoning and the output limit. Session headers go in `provider_headers()`.
- Add the provider ID to `TranscriptCleanupProvider` (and its `ALL`) in `services/settings.py` so saved choices validate, give it a description in `_DESCRIPTIONS` in `ui_qt/widgets/text_model_picker.py`, and check the meeting agents in `meeting/agent/` (every endpoint except OpenAI and OpenRouter must pass a real tool-call probe; see `TextLLMProfile.refuses_unsupported_tools`). Endpoint settings live in `ui_qt/dialogs/settings_models.py` and `ui_qt/dialogs/text_endpoint_dialog.py`.
- Fast judgments need nothing extra: `text_model_route()` in `services/typesafe.py` builds a judge from whichever profile the AI insights or AI cleanup model uses, and `services/text_judge.py` asks it the questions. Whether a judge counts as on this computer, and so skips the meeting's AI insights consent, comes only from `TextLLMProfile.is_local` (a loopback address); don't add another rule for it. A judge that is not a chat model would be a new `JudgeRoute` kind, a `TypeSafeProvider` value, and a `_RouteJudge` subclass handled in `route_from_settings`, `judge_from_settings` and `verify_key`.
- Cover it in `tests/test_text_llm.py`, `tests/test_local_text_servers.py`, `tests/test_settings_models.py`, `tests/test_text_generation.py`, and for judging behavior `tests/test_text_judge.py`.

**Speech engines:**

- Engines that run on this computer (Whisper, Parakeet, Nemotron, Qwen3-ASR, Moonshine) follow [Local speech backends and models](#local-speech-backends-and-models) below: the family catalog in `services/local_asr/catalog.py`, pinned weights in `services/local_asr/models.json`, runtimes as components (`services/components.py`, `services/component_catalog.py`), and the worker in `transcriber/optional_backend.py`.
- **A speech server with an OpenAI-compatible `/audio/transcriptions` route** is a preset, not a new backend: one `_endpoint(...)` entry in `ENDPOINTS` in `services/speech_endpoints.py`, with its default address, key variable (empty for none), default model, `hosted=True` for a service the user cannot re-address, and where its model list is (`models_path`, or `known_models` when it has no usable catalog). Set `auto_language` if it assumes one language when `language` is absent. Request differences (hint fields, `response_format`) belong in `transcription_hints()` and `response_format()` in `transcriber/openai_backend.py`. Presets the user runs get the 600 s upload timeout. Uploads run in the worker process in `services/isolated_worker.py`, so a stalled connection can be killed. Cover it in `tests/test_speech_endpoints.py`, `tests/test_openai_backend.py`, and `tests/test_speech_server_controls.py`.
- Other network speech engines implement the backend interface in `transcriber/base.py`; `transcriber/openai_backend.py` and `transcriber/remote_backend.py` (a paired computer) are the existing examples. Engine IDs and labels are in `config.py`.

For any new network destination, add it to [Network and privacy](docs/network-and-privacy.md), keep it off until the user chooses it, and add it to the bug report form in `.github/ISSUE_TEMPLATE/bug_report.yml`. Privacy copy reads the destination from the profile or endpoint (`consent_destination()`, `speech_endpoints.destination()`), so a correct address is enough for the app to say where text and audio go.

## Local speech backends and models

Local agents should read `AGENTS.md` when present (kept untracked by repository preference). Use [Speech models](README.md#speech-models) for exact supported IDs and user-visible behavior. The optional runtimes target Windows x64. NVIDIA Speech CPU and GPU are also packaged for Linux x86_64 (verified under Ubuntu 24.04 in WSL2 with CUDA), and NVIDIA Speech CPU for Apple Silicon macOS. Do not describe other upstream platform support (Linux aarch64, Vulkan) as support provided by this integration.

A backend or model change must keep these surfaces consistent:

| Concern | Source of truth / files to update |
| --- | --- |
| Family identity, defaults, meeting/streaming capability | `services/local_asr/catalog.py`; backend labels/maps in `config.py` |
| Weights and revisions | `services/local_asr/models.json`; include URL, exact bytes, SHA-256, and the intended inference format |
| Isolated runtime and dependency versions | `services/local_asr/*runtime.json`; component version, archive pins, and measured extracted size |
| UI technical profiles and optional component descriptions | `services/model_catalog.py`, `services/component_catalog.py` |
| Worker inference and lifecycle | `transcriber/optional_backend.py`, `services/local_asr/`, controller engine lease and readiness |
| User and developer docs | README (Speech models, GPU acceleration), AGENTS, CHANGELOG, `THIRD_PARTY_NOTICES.md` |
| GitHub-facing material | README, `.github/ISSUE_TEMPLATE/bug_report.yml`, repository About description, and the next release's notes |

Do not install optional SDKs into the base app requirements. Test the exact pinned runtime through the normal component installer, with its embedded Python and local model paths. Preserve archive license metadata. If a dependency or runtime changes, update its version and measured install size so updates and disk checks remain accurate. Keep model/runtime downloads separate and respect the existing model-download policy and hard offline override.

After activating the repository venv, run the appropriate Python regressions for the changed adapter, model/component catalog, download flow, controller lifecycle, and selectors. Changes to meeting previews also require the dashboard build and `node webui/tests/speech-preview.cjs`. Before advertising support, exercise actual installed models on each advertised device, cancellation/reload, silence, long files, and streaming final flush where applicable. Use `scripts/benchmark_local_asr.py` and `scripts/benchmark_local_asr_corpus.py` for comparable raw-ASR results; record hardware, actual device, model/runtime revisions, normalization, and sample limitations. Do not present a short sample's decode time as full hotkey latency or a leaderboard accuracy claim.

For doc-only changes, check relative links, exact catalog IDs, platform claims, and release status; no model download or full regression run is needed. Historical release notes describe the binaries actually released. Announce the new families under Unreleased until compatible artifacts ship, then update the version-specific guidance and GitHub About description together with the release.

## Dashboard bundle

The meeting dashboard's `webui/dist` is committed because the Python app serves
it directly. After changing dashboard source or its lockfile, rebuild with the
locked dependencies before committing:

```bash
cd webui
npm ci
npm run build
```

Include all changes under `webui/dist`, including new hashed assets and deleted
old assets. `npm run build` alone can use stale packages already in
`node_modules`, producing a different bundle from CI. CI installs the lockfile
with `npm ci` and requires the rebuilt directory to match the committed files.

## Source launchers

From a source checkout you can register `ow` and `openwhisper` so the app launches from any terminal — no need to `cd` into the repo or activate the venv first. Native packages already provide those commands.

### Windows

From the repo root, run:

```
install.cmd
```

This adds `scripts\` to your user PATH (via the registry, not `setx` — see note below). It is idempotent. **Open a new terminal afterward** for the change to take effect.

```
ow              # short alias
openwhisper     # full name
```

The launcher invokes `venv\Scripts\pythonw.exe` directly, so the app always uses the project's venv regardless of which environment your shell has activated. Code changes are picked up live — no reinstall needed after `git pull`.

```
uninstall.cmd
```

Removes the PATH entry only. Your venv, code, and the `scripts/` folder are left untouched, so re-running `install.cmd` later will restore the commands.

If you cannot run the installer (for example, corporate execution-policy restrictions), add the path yourself in PowerShell:

```powershell
$dir = "D:\path\to\OpenWhisper\scripts"   # <-- adjust to your clone location
$current = [Environment]::GetEnvironmentVariable("Path", "User")
if ($current -split ";" -notcontains $dir) {
    [Environment]::SetEnvironmentVariable("Path", "$current;$dir", "User")
}
```

`setx PATH ...` from a `.cmd` file silently truncates PATH at 1024 characters and can duplicate System PATH entries into User PATH. `install.cmd` shells out to PowerShell, which writes directly to `HKCU\Environment\Path` via `[Environment]::SetEnvironmentVariable` — no truncation, no leakage between User and System scopes.

If you would rather not modify PATH, drop a copy of [scripts/openwhisper.cmd](scripts/openwhisper.cmd) into `%LOCALAPPDATA%\Microsoft\WindowsApps\` (already on Windows PATH for every user). That is a *copy*, so refresh it if the launcher logic changes.

### macOS / Linux

From the repo root:

```bash
./install.sh
```

This adds the `scripts/` folder to your `PATH` in your shell profile files (`~/.bashrc`, `~/.zprofile`, and on macOS also `~/.bash_profile`; fish users get `~/.config/fish/config.fish`). It is idempotent. The installer prints the exact `source …` command for your shell — run that in your current terminal, or open a new one, then run `ow` or `openwhisper`.

The launcher invokes `venv/bin/python` directly. Code changes are picked up live. To remove the PATH entry, run `./uninstall.sh`.
