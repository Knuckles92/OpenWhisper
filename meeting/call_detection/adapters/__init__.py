"""Cross-platform audio activity adapters, lazily imported per OS."""

from __future__ import annotations

import logging
import sys
from abc import ABC, abstractmethod
from typing import Optional

from meeting.call_detection.models import ActiveAudioProcess, BrowserContext

logger = logging.getLogger(__name__)


class AudioActivityAdapter(ABC):
    """OS-specific interface for inspecting active mic/audio streams."""

    @abstractmethod
    def active_microphone_processes(self) -> list[ActiveAudioProcess]:
        """Return processes currently holding an active microphone input stream.

        Should exclude OpenWhisper's own process. Raises on OS read error so
        callers can distinguish "no apps on mic" from "could not inspect".
        """

    @abstractmethod
    def is_app_playing_audio(self, canonical_id: str) -> bool:
        """Return True if any process of ``canonical_id`` has active audio output."""

    def browser_context(self, canonical_browser_id: str) -> Optional[BrowserContext]:
        """Inspect the active tab URL/title for ``canonical_browser_id``, if supported."""
        return None

    def close(self) -> None:
        """Release any background threads, subprocesses, or OS handles."""


class NullAudioActivityAdapter(AudioActivityAdapter):
    """Safe no-op fallback on unsupported platforms or missing system tools."""

    def active_microphone_processes(self) -> list[ActiveAudioProcess]:
        return []

    def is_app_playing_audio(self, canonical_id: str) -> bool:
        return False


def create_platform_adapter(platform: Optional[str] = None) -> AudioActivityAdapter:
    """Lazily import and instantiate the audio activity adapter for ``platform``."""
    target = (platform or sys.platform).lower()
    try:
        if target == "darwin":
            from meeting.call_detection.adapters._mac import MacAudioActivityAdapter

            return MacAudioActivityAdapter()
        if target == "win32":
            from meeting.call_detection.adapters._win import WindowsAudioActivityAdapter

            return WindowsAudioActivityAdapter()
        if target.startswith("linux"):
            from meeting.call_detection.adapters._linux import LinuxPipeWireAdapter

            return LinuxPipeWireAdapter()
    except Exception:
        logger.debug(
            "Could not initialize call-detection adapter for %s; using fallback",
            target,
            exc_info=True,
        )
    return NullAudioActivityAdapter()
