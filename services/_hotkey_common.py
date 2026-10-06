"""Dependency-light helpers shared by the platform hotkey backends."""
import logging
import queue
import threading
import time
from typing import Callable, Dict, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Side mouse buttons a shortcut may use as its main key.
MOUSE_KEYS: Dict[str, str] = {"mouse4": "Mouse 4", "mouse5": "Mouse 5"}

#: Actions that fire once per press, with the backend callback attribute each
#: one calls. Record and Command Mode are not here: they also act on release.
PRESS_ACTIONS: Tuple[Tuple[str, str], ...] = (
    ("meeting_toggle", "on_meeting_toggle"),
    ("cancel", "on_cancel"),
    ("minimize_tray", "on_minimize_tray"),
    ("scratchpad_toggle", "on_scratchpad_toggle"),
    ("cycle_language", "on_cycle_language"),
    ("paste_last_original", "on_paste_last_original"),
)

#: Families of shortcuts registered at runtime, as "<namespace>:<id>" actions.
DYNAMIC_NAMESPACES: Tuple[str, ...] = ("profile", "transform")


def is_mouse_key(main_key: Optional[str]) -> bool:
    return main_key in MOUSE_KEYS


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


_STOP = object()
_WAKE = object()


class OrderedDispatcher:
    """Run calls one at a time, in submission order, on one daemon thread.

    Hook threads only enqueue, so a slow handler never delays the next key
    event's timestamp, and a release can never overtake its press. One
    deadline call may be pending; it runs once its time has come and every
    call queued before it has run, so handlers can settle a race between a
    deadline and an event by comparing the event's own timestamp.
    """

    def __init__(self, name: str, clock: Callable[[], float] = time.monotonic):
        self._name = name
        self._clock = clock
        self._queue: "queue.Queue" = queue.Queue()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._deadline = None

    def submit(self, fn: Callable, *args) -> None:
        self._worker_queue().put((fn, args))

    def call_at(self, when: float, fn: Callable, *args) -> None:
        """Run ``fn(*args)`` at ``when`` on the clock, replacing a pending one."""
        with self._lock:
            self._deadline = (when, fn, args)
        self._worker_queue().put(_WAKE)

    def cancel_deadline(self) -> None:
        with self._lock:
            self._deadline = None

    def stop(self) -> None:
        """End the worker after the calls already queued; a later submit starts a new one."""
        with self._lock:
            thread, self._thread = self._thread, None
            self._deadline = None
            pending, self._queue = self._queue, queue.Queue()
        if thread is not None:
            pending.put(_STOP)

    def _worker_queue(self) -> "queue.Queue":
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, args=(self._queue,), name=self._name, daemon=True
                )
                self._thread.start()
            return self._queue

    def _run(self, items: "queue.Queue") -> None:
        while True:
            with self._lock:
                deadline = self._deadline if items is self._queue else None
            timeout = None
            if deadline is not None:
                timeout = max(0.0, deadline[0] - self._clock())
            try:
                item = items.get(timeout=timeout)
            except queue.Empty:
                with self._lock:
                    if self._deadline is not deadline:
                        continue
                    self._deadline = None
                self._call(deadline[1], deadline[2])
                continue
            if item is _STOP:
                return
            if item is not _WAKE:
                self._call(*item)

    @staticmethod
    def _call(fn: Callable, args) -> None:
        try:
            fn(*args)
        except Exception:
            logger.exception("Hotkey handler failed")


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
