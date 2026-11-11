"""The app updater: scheduled GitHub release checks and verified downloads.

Checks run on the controller's shared executor and downloads on its
component executor, so a multi-hundred-megabyte installer never starves
transcription. Both report through this object's signals, which are emitted
on those worker threads and delivered here on the Qt thread (this object is
created there), where the results are handed to the UI controller.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import TYPE_CHECKING, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from config import config

if TYPE_CHECKING:
    from services.application_controller import ApplicationController

logger = logging.getLogger(__name__)

#: Least wait after an update check before the next one, whatever the stored
#: times say: a copy that cannot save them must not ask GitHub again at once.
UPDATE_RECHECK_FLOOR_MS = 60 * 60 * 1000


class UpdaterRuntime(QObject):
    """Owns update checks, update downloads and their leftovers; Qt thread."""

    # (result_or_none, error, manual), emitted from the check worker.
    update_check_finished = pyqtSignal(object, str, bool)
    # (attempt, phase, done, total), emitted from the download worker.
    update_download_progress = pyqtSignal(object, str, int, int)
    # (attempt, handoff_or_none, error), emitted from the download worker.
    update_download_finished = pyqtSignal(object, object, str)

    def __init__(self, controller: "ApplicationController"):
        super().__init__()
        self.controller = controller
        self._check_timer = QTimer()
        self._check_timer.setSingleShot(True)
        self._check_timer.timeout.connect(self._maybe_start_check)
        # A launch asks GitHub without waiting out the interval since the
        # last successful check; that interval only spaces a running app's.
        self._launch_check_pending = False
        self._cancel = threading.Event()
        self._attempt = None
        self._download_lock = threading.Lock()

    def connect_signals(self) -> None:
        self.update_check_finished.connect(self._on_check_finished)
        self.update_download_progress.connect(self._on_download_progress)
        self.update_download_finished.connect(self._on_download_finished)

    @property
    def ui_controller(self):
        return self.controller.ui_controller

    # ---- checks ----

    def schedule_check(self, launch: bool = False) -> None:
        """Check once startup is out of the way, or when it is next due.

        Args:
            launch: The app just started, so check whatever the time since
                the last successful check.
        """
        if launch:
            self._launch_check_pending = True
        self._check_timer.start(config.UPDATE_CHECK_DELAY_MS)

    def request_check(self, manual: bool = True) -> None:
        """Run a GitHub latest-release check on a worker thread."""
        self.controller.executor.submit(self._check_worker, manual)

    def _maybe_start_check(self) -> None:
        """Run the automatic check if it is due, else wait until it is."""
        wait_ms = self._check_wait_ms(
            interval_s=0 if self._launch_check_pending else None
        )
        if wait_ms == 0:
            self._launch_check_pending = False
            self.request_check(manual=False)
        elif wait_ms is not None:
            self._check_timer.start(wait_ms)

    def _check_wait_ms(self, interval_s: Optional[int] = None) -> Optional[int]:
        """Milliseconds until the automatic check is due; None when it is off.

        ``interval_s`` replaces the usual wait since the last successful check.
        """
        from services.app_update import seconds_until_auto_check

        try:
            wait_s = seconds_until_auto_check(interval_s=interval_s)
        except Exception as exc:
            logger.warning("Could not read the update-check schedule: %s", exc)
            return None
        return None if wait_s is None else int(wait_s * 1000)

    def _check_worker(self, manual: bool) -> None:
        from services.app_update import AppUpdateError, check_for_update

        try:
            result = check_for_update()
        except Exception as exc:
            logger.warning("Update check failed: %s", exc)
            message = str(exc) if isinstance(exc, AppUpdateError) else (
                "Could not check for updates."
            )
            self.update_check_finished.emit(None, message, manual)
            return
        self.update_check_finished.emit(result, "", manual)

    def _on_check_finished(self, result, error: str, manual: bool) -> None:
        # The check saved its time, success or failure; wait from there.
        wait_ms = self._check_wait_ms()
        if wait_ms is not None:
            self._check_timer.start(max(wait_ms, UPDATE_RECHECK_FLOOR_MS))
        if error and not manual:
            from services.app_update import update_check_failure_status

            # Background news: offline, it used to land two seconds after
            # "Model download failed" and hide the reason the app can't work.
            show = getattr(self.ui_controller, "set_ambient_status", None)
            if show is not None:
                show(update_check_failure_status(error))
        handler = getattr(self.ui_controller, "on_update_check_finished", None)
        if handler:
            handler(result, error, manual)

    # ---- downloads ----

    def request_download(self, result, force_setup: bool = False) -> None:
        """Download and prepare one verified update attempt on the worker."""
        # Never clear and reuse a prior token: a quick retry must not revive a
        # canceled worker that has not unwound yet.
        self._cancel.set()
        cancel = threading.Event()
        attempt = object()
        self._cancel = cancel
        self._attempt = attempt
        self.controller.component_executor.submit(
            self._download_worker,
            result,
            force_setup,
            cancel,
            attempt,
        )

    def cancel_download(self) -> None:
        self._cancel.set()

    def _download_worker(
        self,
        result,
        force_setup: bool = False,
        cancel: Optional[threading.Event] = None,
        attempt=None,
    ) -> None:
        token = cancel if cancel is not None else self._cancel

        def is_current() -> bool:
            return attempt is None or self._attempt is attempt

        release = getattr(result, "release", None)
        if release is None:
            if is_current():
                self.update_download_finished.emit(attempt, None, "No installer is available.")
            return

        def progress(phase: str, done: int, total: int) -> None:
            if is_current() and not token.is_set():
                self.update_download_progress.emit(attempt, phase, done, total)

        from services.app_update import apply_update

        try:
            with self._download_lock:
                handoff = apply_update(
                    result,
                    progress=progress,
                    cancel=token,
                    force_setup=force_setup,
                )
        except Exception as exc:
            logger.warning("Update download failed: %s", exc)
            message = str(exc) if str(exc) else "The download failed."
            if is_current():
                self.update_download_finished.emit(attempt, None, message)
            return
        if token.is_set() or not is_current():
            from services.app_update import discard_prepared_result

            discard_prepared_result(handoff)
            if is_current():
                self.update_download_finished.emit(attempt, None, "The download was canceled.")
            return
        self.update_download_finished.emit(attempt, handoff, "")

    def discard_handoff(self, handoff: str) -> None:
        from services.app_update import discard_prepared_result

        self.controller.component_executor.submit(discard_prepared_result, handoff)

    def _on_download_progress(self, attempt, phase, done, total) -> None:
        if attempt is self._attempt and not self._cancel.is_set():
            self.ui_controller.on_update_download_progress(phase, done, total)

    def _on_download_finished(self, attempt, handoff, error) -> None:
        if attempt is not self._attempt or self._cancel.is_set():
            if handoff:
                self.discard_handoff(handoff)
            if attempt is self._attempt:
                self.ui_controller.on_update_download_finished(
                    None, "The update was canceled."
                )
            return
        self.ui_controller.on_update_download_finished(handoff, error)

    # ---- leftovers and shutdown ----

    def prune_leftovers(self) -> None:
        """Collect the downloads and transactions an earlier update left behind."""
        from services.app_update import prune_stale_downloads
        from services.app_update_apply import prune_abandoned_transactions

        try:
            removed = prune_stale_downloads()
        except Exception:
            logger.exception("Could not prune finished update downloads")
        else:
            if removed:
                logger.info(
                    "Removed finished update downloads: %s", ", ".join(removed)
                )
        if sys.platform == "darwin":
            from services.app_update_macos import prune_transactions

            try:
                removed = prune_transactions()
            except Exception:
                logger.exception("Could not prune Mac update transactions")
            else:
                if removed:
                    logger.info("Removed Mac update transactions: %s", ", ".join(removed))
        try:
            transactions = prune_abandoned_transactions()
            from services.setup_update import cleanup_setup_backup
            from services.app_update_apply import running_app_dir
            cleanup_setup_backup(running_app_dir())
        except Exception:
            logger.exception("Could not prune abandoned update transactions")
            return
        if transactions:
            logger.info(
                "Removed abandoned update transactions: %s", ", ".join(transactions)
            )

    def stop(self) -> None:
        """Stop the check timer and cancel a download in progress; Qt thread."""
        self._check_timer.stop()
        self._cancel.set()
