#!/usr/bin/env python3
"""Probe Linux Meeting Mode system-audio readiness.

Uses the same production capability probe as the app. Prints only actionable
fields and exits nonzero when dual-channel capture is not ready.

``--capture SECONDS`` then records the default output through the production
capture source and checks the timeline the spool will see: any jump over the
spool's 120 ms tolerance is audio it would trim or pad with silence. Play
something while it runs to also confirm the monitor carries the output mix.
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
    parser.add_argument(
        "--capture",
        type=float,
        metavar="SECONDS",
        default=0.0,
        help="Also record this long through the production capture source",
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
    if not capability.ready:
        return 1
    if args.capture > 0:
        return _capture_check(args.capture)
    return 0


def _capture_check(seconds: float) -> int:
    import numpy as np

    from meeting.capture.soundcard_stream import SoundcardLoopbackSource

    tolerance_s = 0.12  # meeting.capture.spool.GAP_TOLERANCE_S
    blocks = []
    source = SoundcardLoopbackSource()
    started = time.monotonic()
    source.start(lambda block: blocks.append(
        (block.t_mono, len(block.frames), block.sample_rate,
         float(np.sqrt(np.mean(block.frames.astype(np.float64) ** 2))))
    ))
    start_ms = int((time.monotonic() - started) * 1000)
    active = source.is_active()
    time.sleep(seconds)
    source.stop()

    captured_s = sum(n / rate for _t, n, rate, _rms in blocks)
    jumps = [
        nxt[0] - (cur[0] + cur[1] / cur[2])
        for cur, nxt in zip(blocks, blocks[1:])
    ]
    broken = [j for j in jumps if abs(j) > tolerance_s]
    peak_rms = max((rms for *_rest, rms in blocks), default=0.0)
    print(f"capture_start_ms: {start_ms}")
    print(f"capture_active: {active}")
    print(f"captured_s: {captured_s:.2f} of {seconds:.2f}")
    print(f"timeline_breaks_over_{int(tolerance_s * 1000)}ms: {len(broken)}"
          + (f" (largest {max(broken, key=abs):+.3f}s)" if broken else ""))
    print(f"peak_rms_int16: {peak_rms:.0f}"
          + ("  (silent: play audio to confirm the output mix)"
             if peak_rms < 50 else ""))
    ok = active and captured_s >= 0.9 * seconds and not broken
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
