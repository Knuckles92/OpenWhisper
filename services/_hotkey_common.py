"""Shared helpers for the platform hotkey backends.

The keyboard (Windows) and pynput (macOS/Linux) backends parse and format
hotkey strings identically apart from their platform modifier vocabulary, so
the shared logic lives here and each backend binds its own alias maps.
Display formatting stays in each backend because presentation is genuinely
platform-specific (plain "Ctrl+Alt+R" text vs macOS ⌃⌥R symbols), as does
event matching (different input libraries).

This module must stay dependency-light (stdlib only): it is imported by both
backends, including under the test harness that stubs out ``keyboard`` and
``config``.
"""
import logging
import time
from typing import Dict, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


def parse_hotkey_string(
    hotkey_string: str,
    modifier_aliases: Dict[str, str],
    main_key_aliases: Optional[Dict[str, str]] = None,
) -> Tuple[frozenset, Optional[str]]:
    """Parse a hotkey string into ``(modifier_set, main_key_name)``.

    Args:
        hotkey_string: Hotkey string such as ``"ctrl+alt+r"``.
        modifier_aliases: Modifier token -> canonical modifier name for the
            calling backend's platform.
        main_key_aliases: Optional main-key token -> canonical name map
            (e.g. ``"escape"`` -> ``"esc"`` for pynput round-tripping).

    Returns:
        Tuple of the canonical modifier frozenset and the main key name, or
        ``(frozenset(), None)`` for an empty string. Unknown modifier tokens
        are ignored; the last token is always the main key.
    """
    if not hotkey_string:
        return frozenset(), None

    parts = [p.strip().lower() for p in hotkey_string.split("+") if p.strip()]
    if not parts:
        return frozenset(), None

    main_raw = parts[-1]
    main_key = main_key_aliases.get(main_raw, main_raw) if main_key_aliases else main_raw

    modifiers = set()
    for token in parts[:-1]:
        canonical = modifier_aliases.get(token)
        if canonical:
            modifiers.add(canonical)

    return frozenset(modifiers), main_key


def format_hotkey_string(
    modifiers,
    main_key: Optional[str],
    modifier_order: Sequence[str],
) -> str:
    """Build a canonical hotkey string from a modifier set and a main key name.

    Args:
        modifiers: Iterable of canonical modifier names.
        main_key: Main key name, or ``None`` for a modifiers-only string.
        modifier_order: Canonical ordering of the platform's modifiers.

    Returns:
        ``"+"``-joined canonical hotkey string.
    """
    ordered = [m for m in modifier_order if m in modifiers]
    if main_key:
        ordered.append(main_key)
    return "+".join(ordered)


class Debouncer:
    """Debounce repeated triggers using a monotonic clock.

    Wall-clock independent so system clock jumps (sleep/resume, NTP) cannot
    swallow or double-fire hotkeys.
    """

    def __init__(self, interval_ms: int):
        """Initialize the debouncer.

        Args:
            interval_ms: Minimum interval between accepted triggers.
        """
        self.interval_ms = interval_ms
        self._last_trigger_time: Optional[float] = None

    def should_trigger(self) -> bool:
        """Return True (and start a new interval) if enough time has passed."""
        current_time = time.monotonic()
        if self._last_trigger_time is None:
            self._last_trigger_time = current_time
            return True

        if current_time - self._last_trigger_time > (self.interval_ms / 1000.0):
            self._last_trigger_time = current_time
            return True
        return False

    def reset(self) -> None:
        """Clear debounce state so the next trigger fires immediately."""
        self._last_trigger_time = None


def notify_stt_toggle(
    program_enabled: bool,
    on_status_update_auto_hide,
    on_status_update,
) -> None:
    """Emit the STT Enabled/Disabled status via the preferred callback.

    Args:
        program_enabled: New enabled state after the toggle.
        on_status_update_auto_hide: Preferred auto-hiding status callback.
        on_status_update: Plain status callback fallback.
    """
    status = "STT Enabled" if program_enabled else "STT Disabled"
    if on_status_update_auto_hide:
        on_status_update_auto_hide(status)
    elif on_status_update:
        on_status_update(status)
        logger.info(f"STT has been {'enabled' if program_enabled else 'disabled'}")
