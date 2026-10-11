"""``openwhisper --channel``: a Claude Code channel fed by OpenWhisper (stream E).

Contract: docs/design/automation/design.md, decision 11.
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    print("The Claude Code channel isn't available in this build yet.", file=sys.stderr)
    return 2
