# Unified settings

Option 2 is implemented on `codex/settings-unified` in
`D:/coding/whisper_local/.tmp/worktrees/settings-unified`.

Settings uses six categories: Dictation, Meetings, Personalize, Models,
Connections, and App. Each category contains expandable sections with saved
values in their collapsed headers. Speech engine selection sits inside
Recording. Closed sections build their original controls only when opened.

The Basic/Advanced switch is retired. Existing saved preferences remain
untouched. Stable destination keys, old Basic routes, search targets, and
controller shortcuts still open the corresponding section. Search expands
parent disclosures and nested Model options; disabled cleanup points users
to its enable switch without changing settings.

Dictation separates Recording, AI cleanup, and Output. Cleanup shows its
enable switch, preset, provider, model, and provider-specific privacy copy.
Endpoint management, catalog sorting/refresh, reasoning, and custom prompts
live under Model options. Actionable model errors remain visible outside
that disclosure. Window/update controls live under App.

Original save handlers, callbacks, shortcut capture cancellation, draft
validation, and backup navigation locks remain in use. Sections support
Enter and Space and expose expanded/collapsed descriptions. Tests explicitly
mount retained legacy Basic components to keep their shared-handler
regressions without restoring a second production settings mode.

Actual Qt screenshots are in `implemented/`: Dictation, Model options,
Connections, and a narrow window at 130% font size. They use isolated test
settings with catalog requests mocked. API-key copy reports configured
credentials; no key material appears in the images.

The optional first-run Audio → Text → Output setup is separate on
`codex/guided-setup`, in the sibling `guided-setup` worktree. It uses stable
settings routes and existing save handlers, so neither feature requires
the retired Basic presentation. Integrate the two branches independently;
neither worktree has been merged into the primary checkout.

Live audio, external model requests, and downloads were not part of this
UI validation.

## Validation

The final regression run passed 2,350 tests and 3,387 layout subtests.
It included every `test_settings*.py` and `test_personalize*.py` file plus
API keys, hotkeys, MCP, TypeSafe settings messaging, and text-LLM tests.
Ruff checks for undefined names, imports, and bugbear rules passed on changed
Python files. `git diff --check` passed. All four Qt previews were inspected.

Changes remain uncommitted and unmerged in the two designated worktrees.
The primary checkout's existing diagnostic and audio-benchmark changes were
preserved.
