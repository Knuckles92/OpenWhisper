"""Lightweight startup timing hooks for launch diagnostics."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Tuple

logger = logging.getLogger(__name__)


@dataclass
class StartupProfiler:
    """Collect startup timing marks and write a compact log summary."""

    start_time: float = field(default_factory=time.perf_counter)
    events: List[Tuple[str, float]] = field(default_factory=list)

    def mark(self, name: str) -> None:
        """Record a named startup event.

        Args:
            name: Human-readable event name to include in startup logs.
        """
        self.events.append((name, time.perf_counter() - self.start_time))

    def log_summary(self) -> None:
        """Write startup timing totals and per-phase deltas to the log."""
        logger.info("Startup timing summary:")
        previous = 0.0
        for name, elapsed in self.events:
            logger.info(
                "  %-32s total=%7.3fs delta=%7.3fs",
                name,
                elapsed,
                elapsed - previous,
            )
            previous = elapsed
