"""Snap: freeze the screen, pick a region, mark it up, keep it.

``start_snap()`` captures the screen under the pointer and opens a
full-screen overlay on it; the returned ``SnapSession`` reports the kept
image (device pixels), a cancel, or a failure. ``snap_support()`` says
whether this desktop can capture at all, and ``keep_out_of_screenshots()``
lets our own windows stay visible yet out of every capture on Windows.
"""

from ui_qt.snap.affinity import keep_out_of_screenshots
from ui_qt.snap.session import SnapSession, start_snap
from ui_qt.snap.support import snap_support

__all__ = ["SnapSession", "keep_out_of_screenshots", "snap_support", "start_snap"]
