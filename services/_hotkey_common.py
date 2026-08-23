"""Dependency-light helpers shared by the platform hotkey backends."""
import logging
import time
from typing import Dict, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


def parse_hotkey_string(
    hotkey_string: str,
    modifier_aliases: Dict[str, str],
    main_key_aliases: Optional[Dict[str, str]] = None,
) -> Tuple[frozenset, Optional[str]]:
    """Return canonical modifiers and main key parsed from a hotkey string."""
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
    """Build a canonical hotkey string in platform modifier order."""
    ordered = [m for m in modifier_order if m in modifiers]
    if main_key:
        ordered.append(main_key)
    return "+".join(ordered)


class Debouncer:
    """Debounce triggers independently of wall-clock jumps."""

    def __init__(self, interval_ms: int):
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
    """Emit the enabled state through the preferred status callback."""
    status = "STT Enabled" if program_enabled else "STT Disabled"
    if on_status_update_auto_hide:
        on_status_update_auto_hide(status)
    elif on_status_update:
        on_status_update(status)
        logger.info(f"STT has been {'enabled' if program_enabled else 'disabled'}")
