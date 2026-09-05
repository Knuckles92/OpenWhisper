# Contributing

## Comments and Docstrings

Code is the primary source of truth. Add prose only when it records information the code cannot express clearly.

- Keep non-obvious rationale, invariants, concurrency or lifetime rules, security boundaries, platform constraints, compatibility contracts, and measured choices.
- Do not narrate the next statement, restate a symbol's name or signature, add decorative section banners, leave commented-out code, or preserve implementation history that belongs in version control.
- Public APIs need docstrings only when they have a meaningful contract beyond their typed signature. Keep them concise and use Google style when parameter, return, or exception details add information.
- Tests should explain only non-obvious setup or why a behavior matters. Let test names and assertions describe ordinary cases.
- Preserve required directives, licenses, shebangs, active TODOs, and user-facing or model-facing strings.
- Update or remove nearby comments whenever behavior changes. Review comments for future staleness with the same care as code.

Before submitting a change, remove temporary notes and commented-out experiments, then verify that every remaining comment explains why rather than what.

## Local speech backends and models

Local agents should read `AGENTS.md` when present (kept untracked by repository preference). Use [the model reference](docs/models.md) for exact supported IDs, and [the local speech guide](docs/local-asr.md) for user-visible behavior. The new optional runtimes currently target Windows x64; do not describe upstream platform support as support provided by this integration.

A backend or model change must keep these surfaces consistent:

| Concern | Source of truth / files to update |
| --- | --- |
| Family identity, defaults, meeting/streaming capability | `services/local_asr/catalog.py`; backend labels/maps in `config.py` |
| Weights and revisions | `services/local_asr/models.json`; include URL, exact bytes, SHA-256, and the intended inference format |
| Isolated runtime and dependency versions | `services/local_asr/*runtime.json`; component version, archive pins, and measured extracted size |
| UI technical profiles and optional component descriptions | `services/model_catalog.py`, `services/component_catalog.py` |
| Worker inference and lifecycle | `transcriber/optional_backend.py`, `services/local_asr/`, controller engine lease and readiness |
| User and developer docs | README, `docs/models.md`, `docs/local-asr.md`, AGENTS, CHANGELOG, `THIRD_PARTY_NOTICES.md` |
| GitHub-facing material | README, `.github/ISSUE_TEMPLATE/bug_report.yml`, repository About description, and the next release's notes |

Do not install optional SDKs into the base app requirements. Test the exact pinned runtime through the normal component installer, with its embedded Python and local model paths. Preserve archive license metadata. If a dependency or runtime changes, update its version and measured install size so updates and disk checks remain accurate. Keep model/runtime downloads separate and respect the existing model-download policy and hard offline override.

After activating the repository venv, run the appropriate Python regressions for the changed adapter, model/component catalog, download flow, controller lifecycle, and selectors. Changes to meeting previews also require the dashboard build and `node webui/tests/speech-preview.cjs`. Before advertising support, exercise actual installed models on each advertised device, cancellation/reload, silence, long files, and streaming final flush where applicable. Use `scripts/benchmark_local_asr.py` and `scripts/benchmark_local_asr_corpus.py` for comparable raw-ASR results; record hardware, actual device, model/runtime revisions, normalization, and sample limitations. Do not present a short sample's decode time as full hotkey latency or a leaderboard accuracy claim.

For doc-only changes, check relative links, exact catalog IDs, platform claims, and release status; no model download or full regression run is needed. Historical release notes describe the binaries actually released. Announce the new families under Unreleased until compatible artifacts ship, then update the version-specific guidance and GitHub About description together with the release.
