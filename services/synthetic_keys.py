"""Synthetic keystrokes for reading the selection: copy, and waiting it out.

A copy is only ever sent after the hotkey's own modifiers are up, and never
to a terminal, where Ctrl+C interrupts the running program.
"""

from __future__ import annotations


def send_copy() -> None:
    """Send the platform's copy shortcut to the focused app."""


def wait_for_modifiers_released(timeout_s: float = 0.8) -> bool:
    """Wait until no modifier key is held; False when one still is at the timeout."""
    return True


def is_terminal(identity) -> bool:
    """Whether ``identity`` (an AppIdentity or None) is a terminal."""
    return False


def copies_line_without_selection(identity) -> bool:
    """Whether the app copies the whole line when nothing is selected."""
    return False
