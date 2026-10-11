"""Running saved programs on events (stream B fills this in).

Contract: docs/design/automation/design.md, "services/automation/programs.py".
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence


@dataclass(frozen=True)
class RunRecord:
    event_id: str
    at: str
    ok: bool
    exit_code: Optional[int]
    elapsed_ms: int
    error: str = ""


def start() -> None:
    """Begin running enabled programs on their events; idempotent."""


def stop() -> None:
    """Stop; running programs are left to finish within their time limit."""


def reload() -> None:
    """Saved programs changed."""


def run_test(program_id: str) -> Future:
    """Run one program with a test event; Future[RunRecord]."""
    future: Future = Future()
    future.set_exception(RuntimeError("Programs aren't available yet."))
    return future


def runs(program_id: str) -> list[RunRecord]:
    """Recent runs of one program, newest first (memory only)."""
    return []


def run_once(argv: Sequence[str], stdin: bytes, *, timeout_s: float, cwd: str = "",
             env: Optional[Mapping[str, str]] = None) -> tuple[Optional[int], bytes, str]:
    """Run ``argv`` without a shell or window; (exit code, stdout, error)."""
    return None, b"", "Programs aren't available yet."
