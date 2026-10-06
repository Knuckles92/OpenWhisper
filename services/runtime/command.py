"""Command Mode and transforms: rewrite the selection, or write at the cursor."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from services.application_controller import ApplicationController
    from services.transcript_cleanup import CleanupInfo

UNAVAILABLE_MESSAGE = "Command Mode isn't available yet"


class CommandRuntime:
    """Owns Command Mode recordings and transform runs for the controller."""

    def __init__(self, controller: "ApplicationController"):
        self.controller = controller

    def key_pressed(self, at: float) -> None:
        """The command_mode shortcut went down; ``at`` is the hook's monotonic time.

        Called on the hotkey dispatcher thread, in event order.
        """
        self.controller.status_update.emit(UNAVAILABLE_MESSAGE)

    def key_released(self, at: float) -> None:
        """The command_mode shortcut came up; dispatcher thread."""
        self.controller.status_update.emit(UNAVAILABLE_MESSAGE)

    def run_transform(self, transform_id: str) -> None:
        """Rewrite the current selection with a saved transform; Qt thread."""
        self.controller.status_update.emit(UNAVAILABLE_MESSAGE)

    def complete_recording(
        self, raw: str, job,
    ) -> tuple[str, Optional[str], Optional["CleanupInfo"]]:
        """Turn a command recording's transcript into the text to paste.

        Runs on the executor worker. Returns ``(text, raw_text, info)`` like
        the dictation path.

        Raises:
            RuntimeError: With a user-facing message when nothing can be pasted.
        """
        raise RuntimeError(UNAVAILABLE_MESSAGE)

    def cleanup(self) -> None:
        pass
