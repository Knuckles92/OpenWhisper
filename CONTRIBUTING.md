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
