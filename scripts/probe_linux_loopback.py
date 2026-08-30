#!/usr/bin/env python3
"""Probe Linux Meeting Mode system-audio readiness.

Uses the same production capability probe as the app. Prints only actionable
fields and exits nonzero when dual-channel capture is not ready.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Skip opening the monitor recorder",
    )
    args = parser.parse_args()

    from meeting.capture.linux_audio import probe_linux_audio
    from meeting.platform import normalize_linux_machine
    import platform as platform_module

    started = time.monotonic()
    capability = probe_linux_audio(verify_open=not args.no_open)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    print(f"ready: {capability.ready}")
    print(f"reason: {capability.reason}")
    print(f"architecture: {normalize_linux_machine(platform_module.machine())}")
    print(f"package_family: {capability.package_family}")
    print(f"server_kind: {capability.server_kind}")
    print(f"default_sink: {capability.default_sink or '-'}")
    print(f"monitor_source: {capability.monitor_source or '-'}")
    print(f"probe_ms: {elapsed_ms}")
    if capability.detail:
        print(f"detail: {capability.detail}")
    print(
        "note: silence is not failure; a validated quiet monitor can still be ready"
    )
    return 0 if capability.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
