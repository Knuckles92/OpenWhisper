"""Passive macOS click observation on AppKit's main run loop.

NSEvent's global mouse monitor needs no keyboard or Accessibility grant and
never consumes input. It excludes this app; a local monitor only clears
presses so a drag across our windows cannot leave a stale button down.
AppKit and Quartz load only when starting, so other platforms can import it.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class _Run:
    def __init__(self):
        self.global_monitor = None
        self.local_monitor = None
        self.pressed: set[int] = set()
        # Keep the Python blocks alive until both native monitors are removed.
        self.global_handler = None
        self.local_handler = None


class MouseClickMonitor:
    def __init__(self, on_click: Callable[[int, int, float], None]):
        self._on_click = on_click
        self._run: _Run | None = None
        self._appkit = None

    @property
    def running(self) -> bool:
        run = self._run
        return (run is not None and run.global_monitor is not None
                and run.local_monitor is not None)

    def start(self) -> None:
        """Install on the main thread, where the Qt Cocoa event loop runs."""
        if self._run is not None:
            return
        try:
            import AppKit
            import Quartz

            self._appkit = AppKit
            presses = {
                AppKit.NSEventTypeLeftMouseUp: AppKit.NSEventTypeLeftMouseDown,
                AppKit.NSEventTypeRightMouseUp: AppKit.NSEventTypeRightMouseDown,
            }
            buttons = set(presses) | set(presses.values())
            mask = sum(1 << kind for kind in buttons)
            run = self._run = _Run()

            def global_event(event):
                if self._run is not run:
                    return
                try:
                    kind = event.type()
                    press = presses.get(kind, kind)
                    held = press in run.pressed
                    run.pressed.discard(press)
                    native = event.CGEvent()
                    if native is None or Quartz.CGEventGetIntegerValueField(
                        native, Quartz.kCGEventSourceUnixProcessID
                    ):
                        return  # Posted events are not physical mouse/trackpad input.
                    if kind in presses.values():
                        run.pressed.add(kind)
                    elif held:
                        # Quartz uses the same top-left desktop points as Qt
                        # on macOS, including negative origins; never scale by DPR.
                        point = Quartz.CGEventGetLocation(native)
                        self._on_click(math.floor(point.x), math.floor(point.y), time.monotonic())
                except Exception:
                    logger.exception("Could not observe a walkthrough click")

            def local_event(event):
                if self._run is run:
                    kind = event.type()
                    run.pressed.discard(presses.get(kind, kind))
                return event

            run.global_handler, run.local_handler = global_event, local_event
            run.local_monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                mask, local_event
            )
            run.global_monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                mask, global_event
            )
            if not self.running:
                raise OSError("macOS did not install the mouse monitors")
        except Exception as exc:
            self.stop()
            raise OSError("Could not start macOS click observation") from exc

    def wait_until_installed(self, timeout: float = 1.0) -> bool:
        # Unlike the Windows hook, AppKit installs synchronously.
        return self.running

    def stop(self) -> None:
        run, self._run = self._run, None
        if run is None:
            return
        for monitor in (run.global_monitor, run.local_monitor):
            if monitor is not None:
                try:
                    self._appkit.NSEvent.removeMonitor_(monitor)
                except Exception:
                    logger.exception("Could not remove a walkthrough mouse monitor")
        run.global_monitor = run.local_monitor = None
        run.global_handler = run.local_handler = None
        run.pressed.clear()
