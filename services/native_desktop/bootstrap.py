"""Qt-free service process for the native Rune desktop's private JSON pipe."""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

# A valid 5M-character Scratchpad can occupy 20 MB as UTF-8. The pipe remains
# bounded while accepting that documented text limit, including JSON framing.
MAX_REQUEST_BYTES = 32 * 1024 * 1024
_OUTPUT = None


def _request_lines(stream):
    """Bound line buffering without holding stdin's Python lock during exit.

    The owner can quit while its parent still has the pipe open. A daemon
    blocked in BufferedReader.readline would then abort Python finalization.
    Raw descriptor reads release the GIL and hold no interpreter I/O lock.
    """
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError):
        while line := stream.readline(MAX_REQUEST_BYTES + 1):
            yield line
        return
    pending = bytearray()
    while True:
        newline = pending.find(b"\n")
        if newline >= 0:
            yield bytes(pending[:newline + 1])
            del pending[:newline + 1]
        elif len(pending) > MAX_REQUEST_BYTES:
            yield bytes(pending)
            return
        else:
            chunk = os.read(descriptor, min(65536, MAX_REQUEST_BYTES + 1 - len(pending)))
            if not chunk:
                if pending:
                    yield bytes(pending)
                return
            pending.extend(chunk)


def reserve_output():
    """Keep library prints and diagnostics away from the structured transport."""
    global _OUTPUT
    # CREATE_NO_WINDOW children can inherit a stdout pipe without a stderr
    # console handle. Reserve that pipe before redirecting library prints to
    # an always-writable diagnostic stream.
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="backslashreplace")
    if _OUTPUT is None:
        if sys.stdout is None:
            raise RuntimeError("The native service requires its parent's stdout pipe.")
        _OUTPUT = sys.stdout
        sys.stdout = sys.stderr
    return _OUTPUT


def _hotkey_state(controller):
    from services._hotkey_common import DYNAMIC_NAMESPACES, PRESS_ACTIONS
    from services.hotkey_manager import format_hotkey, parse_hotkey

    manager = getattr(controller, "hotkey_manager", None)
    suspended = bool(getattr(manager, "capture_suspended", False))
    if manager is None or suspended:
        return {"hotkey_chords": [], "hotkeys_suspended": suspended}
    standard = {"enable_disable", "record_toggle", "command_mode", *(name for name, _ in PRESS_ACTIONS)}
    bindings = manager._all_hotkeys()
    if not manager.program_enabled:
        bindings = {"enable_disable": manager.hotkeys.get("enable_disable", "")}
    chords = set()
    for action, chord in bindings.items():
        namespace, separator, _item_id = action.partition(":")
        if action not in standard and not (separator and namespace in DYNAMIC_NAMESPACES):
            continue
        if chord:
            modifiers, key = parse_hotkey(chord)
            if key:
                chords.add(format_hotkey(modifiers, key))
    return {"hotkey_chords": sorted(chords), "hotkeys_suspended": suspended}


class NativeContext:
    def __init__(self, controller, ui, emit, shutdown):
        self.controller = controller
        self.ui = ui
        self.emit = emit
        self.shutdown = shutdown
        self._history = []
        self._history_at = 0.0
        self._history_generation = -1

    def snapshot(self):
        from _version import __version__
        from services.automation.app_control import QtAppControl
        from services.database import db
        from services.native_desktop.ui import serializable
        from services.settings import settings_manager

        c, ui = self.controller, self.ui
        ready = bool(c._startup_database_ready)
        if ready and (ui._history_generation != self._history_generation
                      or time.monotonic() - self._history_at >= 5):
            self._history = [{
                "id": entry.id, "title": entry.title or entry.source_name or "Dictation",
                "preview": entry.text[:180], "timestamp": entry.timestamp,
                "duration": entry.audio_duration or 0,
                "kind": entry.entry_kind or ("file" if entry.source_name else "dictation"),
                "words": len(entry.text.split()), "model": entry.model,
            } for entry in db.get_history_entries(limit=60)]
            self._history_at = time.monotonic()
            self._history_generation = ui._history_generation
        saved = settings_manager.load_all_settings()
        state = QtAppControl(c).state().to_dict()
        result = {**serializable(ui.state), **state, **_hotkey_state(c), "version": __version__,
            "history": self._history, "engine": settings_manager.load_model_selection(),
            "ready": ready, "runtime": "native", "settings": {
                key: saved.get(key, default) for key, default in (
                    ("auto_paste", True), ("transcript_cleanup_enabled", False),
                    ("copy_clipboard", True), ("local_asr_language", "en"),
                    ("ui_theme", "light"), ("ui_font_scale", 1.0), ("compact_mode", False))}}
        if ready and result["status"] == "Preparing your workspace…":
            result["status"] = "Ready when you are"
        return result


def main(argv=None) -> int:
    """Own data locks, native event loop, controller and private stdio lifetime."""
    os.environ["OPENWHISPER_DESKTOP_RUNTIME"] = "native"
    output = reserve_output()
    from config import config, data_root
    from services.backup_startup import (
        acquire_startup_data_lease,
        release_startup_data_lease,
    )
    from services.desktop_runtime import (
        QCoreApplication,
        QObject,
        Qt,
        QTimer,
        pyqtSignal,
        pyqtSlot,
    )
    from services.native_desktop.ui import NativeUIController, serializable
    from services.ui_instance import acquire_ui_instance, release_ui_instance

    logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format=config.LOG_FORMAT, handlers=[logging.StreamHandler(sys.stderr),
            RotatingFileHandler(config.LOG_FILE, maxBytes=config.LOG_MAX_BYTES,
                                backupCount=config.LOG_BACKUP_COUNT, encoding="utf-8")], force=True)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    from services.diagnostics import install_failure_capture
    install_failure_capture()
    write_lock = threading.Lock()
    closed = threading.Event()

    def write(packet):
        if closed.is_set():
            return
        try:
            # JSON escapes preserve every Unicode value even when a Windows
            # service pipe's TextIOWrapper inherited an ASCII code page.
            line = json.dumps(serializable(packet), ensure_ascii=True, allow_nan=False) + "\n"
            with write_lock:
                output.write(line)
                output.flush()
        except (BrokenPipeError, OSError):
            closed.set()
            if ui is not None:
                ui.cancel_prompts()
            QCoreApplication.quit()

    def emit(event, data):
        write({"event": event, "data": data})

    app = QCoreApplication([])
    controller = ui = instance = None
    lease_acquired = False
    try:
        acquire_startup_data_lease(data_root())
        lease_acquired = True
        instance = acquire_ui_instance(data_root())
        if instance is None:
            emit("lifecycle", {"state": "already_running"})
            return 0
        from services.guided_setup import initialize as initialize_guided_setup
        from services.settings import keep_unchosen_engine, settings_manager
        initialize_guided_setup()
        keep_unchosen_engine()
        from services.code_identity import remember_launch
        remember_launch()
        from services.application_controller import ApplicationController
        from transcriber import LocalWhisperBackend
        ui = NativeUIController(emit)
        controller = ApplicationController(ui, local_backend=LocalWhisperBackend(load=False))
        controller.history_persisted.connect(lambda _result: ui.refresh_history())
        controller.batch_completed.connect(ui.set_batch_result)
        instance.attach_activate(ui.main_window.restore_from_tray)

        def shutdown(*, force=False):
            if not force and not closed.is_set():
                saved = ui.flush_scratchpad()
                if not saved and ui.state["scratchpad_save"]["dirty"]:
                    raise ValueError("Your latest Scratchpad notes could not be saved. Check free space and file permissions before quitting.")
            ui.cancel_prompts()
            app.quit()

        context = NativeContext(controller, ui, emit, shutdown)

        class Requests(QObject):
            received = pyqtSignal(object)

            def __init__(self):
                super().__init__()
                self.received.connect(self.dispatch, Qt.ConnectionType.QueuedConnection)

            @pyqtSlot(object)
            def dispatch(self, request):
                request_id = request.get("id")
                try:
                    if not isinstance(request.get("method"), str):
                        raise ValueError("Expected a method name.")
                    params = request.get("params", {})
                    if not isinstance(params, dict):
                        raise ValueError("Expected a parameter object.")
                    from services.native_desktop.commands import dispatch
                    result = dispatch(context, request["method"], params)
                    write({"id": request_id, "result": result})
                except Exception as exc:
                    logging.getLogger(__name__).debug("Native command failed", exc_info=True)
                    write({"id": request_id, "error": str(exc)})

        requests = Requests()

        def read_requests():
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            try:
                for line in _request_lines(stream):
                    if closed.is_set():
                        break
                    request_id = None
                    try:
                        if len(line) > MAX_REQUEST_BYTES:
                            raise ValueError("Request is too large.")
                        request = json.loads(line)
                        if not isinstance(request, dict):
                            raise ValueError("Expected a request object.")
                        request_id = request.get("id")
                        requests.received.emit(request)
                    except (ValueError, UnicodeError) as exc:
                        write({"id": request_id, "error": str(exc)})
                        if len(line) > MAX_REQUEST_BYTES:
                            break
            except (OSError, ValueError):
                logging.getLogger(__name__).debug("Native pipe closed", exc_info=True)
            finally:
                closed.set()
                shutdown(force=True)

        threading.Thread(target=read_requests, name="native-desktop-stdio", daemon=True).start()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda _signum, _frame: shutdown(force=True))
        controller.setup_backup()
        backup = controller.backup_coordinator

        def backup_update(**fields):
            ui.state["backup"] = {**ui.state.get("backup", {}), **serializable(fields)}
            emit("backup.state", {**backup.snapshot(), **ui.state["backup"]})

        backup.progress.connect(lambda phase, done, total:
            backup_update(progress={"phase": phase, "done": done, "total": total}))
        backup.busy_changed.connect(lambda busy: backup_update(busy=busy))
        backup.schedule_changed.connect(lambda: backup_update())
        backup.backup_finished.connect(lambda result, error:
            backup_update(last_result=result, error=error))
        backup.inspection_finished.connect(lambda result, error:
            backup_update(inspection={"path": str(result.path) if result else "",
                "valid": result is not None and not error, "info": result, "error": error}))

        def restore_staged(path, error):
            backup_update(restore={"path": path, "error": error})
            if not error:
                emit("lifecycle", {"state": "restart_required", "reason": "restore"})

        backup.restore_staged.connect(restore_staged)
        controller.notify_main_ui_ready()
        from services.credentials import (
            UNRESPONSIVE_KEYRING_STATUS,
            keyring_did_not_respond,
        )
        if keyring_did_not_respond():
            ui.set_ambient_status(UNRESPONSIVE_KEYRING_STATUS)
        from services.app_update_apply import (
            parse_health_token,
            write_health_acknowledgement,
        )
        health_token = parse_health_token()
        if health_token:
            ui.main_window.setEnabled(False)

            def acknowledge_update():
                if controller.update_readiness_error or not write_health_acknowledgement(health_token):
                    emit("fatal", {"message": controller.update_readiness_error or "Could not acknowledge the update transaction."})
                    app.exit(1)
                else:
                    ui.main_window.setEnabled(True)

            QTimer.singleShot(1500, acknowledge_update)
        from services.agent_mcp.runtime import runtime as mcp_runtime
        mcp_runtime.configure_controls(on_change=ui.agent_data_changed.emit,
            meeting_renamer=controller.meeting_runtime.retitle_saved_meeting,
            client_history=controller.remote_engine.history)
        mcp_runtime.restore(settings_manager)
        emit("lifecycle", {"state": "ready", "runtime": "native"})
        return app.exec()
    except Exception as exc:
        logging.exception("Native desktop startup failed")
        emit("fatal", {"message": str(exc)})
        return 1
    finally:
        if ui is not None:
            ui.cancel_prompts()
        try:
            from services.agent_mcp.runtime import runtime as mcp_runtime
            mcp_runtime.stop(wait=True)
            if controller is not None:
                controller.cleanup()
            elif ui is not None:
                ui.cleanup()
        finally:
            release_ui_instance()
            if lease_acquired:
                release_startup_data_lease()
            closed.set()


if __name__ == "__main__":
    raise SystemExit(main())
