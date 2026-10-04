"""Application-owned workers and opt-in schedules for local backups."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

logger = logging.getLogger(__name__)
PREFERENCES_FILE = "backup-preferences.json"
_AUTO_NAME = re.compile(r"OpenWhisper-auto-\d{8}T\d{6}Z-[0-9a-f]{12}\.owbackup\Z")
_INTERVALS = {"daily": 86400, "weekly": 7 * 86400}


class BackupCoordinator(QObject):
    progress = pyqtSignal(str, int, int)
    backup_finished = pyqtSignal(object, str)
    inspection_finished = pyqtSignal(object, str)
    restore_staged = pyqtSignal(str, str)
    schedule_changed = pyqtSignal()
    busy_changed = pyqtSignal(bool)
    _worker_finished = pyqtSignal(str, object, str, bool)

    def __init__(self, parent=None, *, data_dir=None, acquire=None, release=None,
                 service=None, clock=time.time, worker_context=None):
        super().__init__(parent)
        if data_dir is None:
            from config import data_root
            data_dir = data_root()
        if service is None:
            from services import backup
            service = backup
        self.data_dir = Path(data_dir).resolve()
        self._service = service
        self._acquire = acquire or (lambda: None)
        self._release = release or (lambda: None)
        self._clock = clock
        self._worker_context = worker_context or nullcontext
        self._progress_at = 0.0
        self._progress_phase = ""
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="backup")
        self._cancel = threading.Event()
        self._busy = False
        self._reserved = False
        self._closed = False
        self._operation = ""
        self._last_error = ""
        self._retry_after = 0.0
        self._preferences = self._load_preferences()
        self._worker_finished.connect(self._finish)
        self._timer = QTimer(self)
        self._timer.setInterval(60_000)
        self._timer.timeout.connect(self._scheduled_tick)

    @property
    def busy(self):
        return self._busy

    @property
    def operation(self):
        return self._operation

    def start(self):
        if not self._closed:
            self._timer.start()

    def snapshot(self):
        previous_data_dir = ""
        lookup = getattr(self._service, "previous_data_dir", None)
        if lookup is not None:
            try:
                previous = lookup(self.data_dir)
                previous_data_dir = str(previous) if previous else ""
            except Exception:
                logger.exception("Could not locate data retained before the last restore")
        interval = _INTERVALS.get(self._preferences["frequency"])
        next_backup_at = None
        if interval is not None:
            last_auto_at = self._preferences["last_auto_at"]
            next_backup_at = max(
                self._clock(), last_auto_at + interval if last_auto_at else 0,
                self._retry_after,
            )
        return {
            "last_backup": self._preferences.get("last_backup"),
            "frequency": self._preferences["frequency"],
            "retention_count": self._preferences["retention_count"],
            "destination": self._preferences["destination"],
            "busy": self._busy, "operation": self._operation,
            "last_error": self._last_error,
            "previous_data_dir": previous_data_dir,
            "next_backup_at": next_backup_at,
        }

    def _load_preferences(self):
        defaults = {"frequency": "off", "retention_count": 5, "destination": "",
                    "last_backup": None, "last_auto_at": 0, "scheduled_archives": []}
        try:
            source = self.data_dir / PREFERENCES_FILE
            if source.is_symlink() or source.stat().st_size > 1024 * 1024:
                return defaults
            value = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return defaults
            if value.get("frequency") in ("off", "daily", "weekly"):
                defaults["frequency"] = value["frequency"]
            count = value.get("retention_count")
            if type(count) is int and 1 <= count <= 100:
                defaults["retention_count"] = count
            if isinstance(value.get("destination"), str):
                defaults["destination"] = value["destination"]
            last = value.get("last_backup")
            if isinstance(last, dict) and isinstance(last.get("path"), str):
                defaults["last_backup"] = last
            stamp = value.get("last_auto_at", 0)
            if isinstance(stamp, (int, float)) and 0 <= stamp <= self._clock() + 86400:
                defaults["last_auto_at"] = stamp
            archives = value.get("scheduled_archives")
            if isinstance(archives, list):
                defaults["scheduled_archives"] = [p for p in archives[-1000:]
                                                    if isinstance(p, str)]
        except (OSError, ValueError, TypeError):
            pass
        return defaults

    def _save_preferences(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        destination = self.data_dir / PREFERENCES_FILE
        if destination.is_symlink():
            raise ValueError("The backup preferences file must not be a symbolic link.")
        temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(self._preferences, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def set_schedule(self, frequency, retention_count, destination):
        if self._busy:
            raise ValueError("Wait for the current backup operation to finish.")
        if frequency not in ("off", "daily", "weekly"):
            raise ValueError("Choose Off, Daily, or Weekly.")
        if type(retention_count) is not int or not 1 <= retention_count <= 100:
            raise ValueError("Keep between 1 and 100 automatic backups.")
        destination = str(destination).strip()
        if frequency != "off":
            folder = Path(destination).expanduser().resolve()
            if not destination or not folder.is_dir():
                raise ValueError("Choose an existing folder for automatic backups.")
            for name in ("recordings", "meetings", "remote_records", ".openwhisper-restore"):
                if folder.is_relative_to(self.data_dir / name):
                    raise ValueError("Choose a backup folder outside OpenWhisper's recording folders.")
            destination = str(folder)
        previous = dict(self._preferences)
        self._preferences.update(frequency=frequency, retention_count=retention_count,
                                 destination=destination)
        try:
            self._save_preferences()
        except Exception:
            self._preferences = previous
            raise
        self._last_error = ""
        self._retry_after = 0
        self.schedule_changed.emit()

    def create_backup(self, path, include_recordings=True):
        self._start("backup", path, include_recordings=include_recordings)

    def inspect_backup(self, path):
        self._start("inspect", path)

    def prepare_restore(self, path):
        self._start("restore", path)

    def cancel(self):
        # A staged restore must either finish publication or report failure;
        # its final journal is the restart contract, so it is not cancellable.
        if self._operation == "backup":
            self._cancel.set()

    def _emit_result(self, operation, result, error):
        if operation == "backup":
            self.backup_finished.emit(result, error)
        elif operation == "inspect":
            self.inspection_finished.emit(result, error)
        else:
            self.restore_staged.emit(str(result.path) if result else "", error)

    def _start(self, operation, path, *, include_recordings=True, automatic=False):
        if self._busy or self._closed:
            if not automatic:
                self._emit_result(operation, None, "Another backup operation is already running.")
            return False
        if operation != "inspect":
            try:
                reason = self._acquire()
            except Exception as exc:
                reason = str(exc)
            if reason:
                if not automatic:
                    self._emit_result(operation, None, reason)
                return False
            self._reserved = True
        self._busy, self._operation = True, operation
        self._last_error = ""
        self._cancel.clear()
        self.busy_changed.emit(True)

        def work():
            if operation == "inspect":
                return self._service.inspect_backup(path), ""
            with self._worker_context():
                if operation == "backup":
                    result = self._service.create_backup(
                        path, data_dir=self.data_dir, include_recordings=include_recordings,
                        progress=self._report_progress, cancel=self._cancel)
                else:
                    return self._service.prepare_restore(path, data_dir=self.data_dir), ""
            return result, self._remember_backup(result, automatic)

        def finished(future):
            try:
                result, error = future.result()
            except Exception as exc:
                result, error = None, str(exc) or "Backup operation failed."
            self._worker_finished.emit(operation, result, error, automatic)

        try:
            self._executor.submit(work).add_done_callback(finished)
        except Exception as exc:
            self._finish(operation, None, str(exc), automatic)
        return True

    def _report_progress(self, phase, completed, total):
        now = time.monotonic()
        if (phase == self._progress_phase and completed != total
                and now - self._progress_at < 0.1):
            return
        self._progress_at, self._progress_phase = now, phase
        # Qt's int signal is 32 bit; archives can easily exceed that size.
        percentage = min(1000, int(completed * 1000 / total)) if total else 0
        self.progress.emit(str(phase), percentage, 1000 if total else 0)

    def _remember_backup(self, result, automatic):
        # Retention can touch a disconnected or slow drive. Keep it on the
        # same worker as archive creation, while schedule edits remain locked.
        info = asdict(result)
        info["path"] = str(result.path)
        self._preferences["last_backup"] = info
        if automatic:
            self._preferences["last_auto_at"] = self._clock()
            self._preferences["scheduled_archives"].append(str(result.path))
        try:
            self._save_preferences()
            if automatic:
                self._prune_automatic_backups()
        except Exception as exc:
            error = "Backup saved, but its history or retention could not be updated: " + str(exc)
            logger.warning(error)
            return error
        return ""

    def _finish(self, operation, result, error, automatic):
        if self._reserved:
            self._release()
            self._reserved = False
        self._busy = False
        self._operation = ""
        self._last_error = error
        if automatic and error:
            self._retry_after = self._clock() + 600
        self.busy_changed.emit(False)
        self.schedule_changed.emit()
        if not self._closed:
            self._emit_result(operation, result, error)

    def _scheduled_tick(self):
        if self._closed or self._busy or self._clock() < self._retry_after:
            return
        frequency = self._preferences["frequency"]
        interval = _INTERVALS.get(frequency)
        if interval is None or self._clock() - self._preferences["last_auto_at"] < interval:
            return
        folder = Path(self._preferences["destination"])
        if (not self._preferences["destination"] or not folder.is_absolute()
                or not folder.is_dir()):
            self._last_error = "Automatic backup is waiting for its destination folder to be available."
            self.schedule_changed.emit()
            return
        folder = folder.resolve()
        if any(folder.is_relative_to(self.data_dir / name) for name in
               ("recordings", "meetings", "remote_records", ".openwhisper-restore")):
            self._last_error = "Choose a backup folder outside OpenWhisper's recording folders."
            self.schedule_changed.emit()
            return
        stamp = datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = folder / f"OpenWhisper-auto-{stamp}-{uuid.uuid4().hex[:12]}.owbackup"
        self._start("backup", destination, include_recordings=True, automatic=True)

    def _prune_automatic_backups(self):
        folder = Path(self._preferences["destination"]).resolve()
        owned = []
        for value in self._preferences["scheduled_archives"]:
            candidate = Path(value)
            if (candidate.parent.resolve() == folder and _AUTO_NAME.fullmatch(candidate.name)
                    and not candidate.is_symlink() and candidate.is_file()):
                owned.append(candidate)
        keep = self._preferences["retention_count"]
        remove = owned[:-keep]
        for candidate in remove:
            # Only exact files recorded after successful automatic creation are
            # eligible. Manual archives and unrelated destination files remain.
            candidate.unlink()
        removed = {str(p) for p in remove}
        self._preferences["scheduled_archives"] = [
            p for p in self._preferences["scheduled_archives"] if p not in removed]
        self._save_preferences()

    def shutdown(self):
        self._closed = True
        self._timer.stop()
        self._cancel.set()
        self._executor.shutdown(wait=True, cancel_futures=True)
        if self._reserved:
            self._release()
            self._reserved = False

