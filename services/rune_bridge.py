"""Private stdio transport for the Rune desktop, with all control on Qt's thread.

Only a parent-launched child uses this bridge. It opens no listener, changes no
API permissions, and reserves stdout for JSON before application logging starts.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import deque
from pathlib import Path

MAX_REQUEST_BYTES = 16_384
BOOLEAN_SETTINGS = {"auto_paste", "transcript_cleanup_enabled", "copy_clipboard"}
LANGUAGES = {"", "en", "es", "fr", "de", "it", "pt", "ja", "zh"}
_OUTPUT = None


def reserve_output():
    global _OUTPUT
    if _OUTPUT is None:
        _OUTPUT = sys.stdout
        sys.stdout = sys.stderr
    return _OUTPUT


class RuneBridge:
    def __init__(self, controller, output):
        from PyQt6.QtCore import QObject, pyqtSlot

        self.controller = controller
        self.output = output
        self.status = "Preparing your workspace…"
        self.transcript = ""
        self.raw = ""
        self.preview = ""
        self.levels = deque([0.0] * 49, maxlen=49)
        self._history = []
        self._history_refreshed = 0.0
        self._history_dirty = True
        self.closed = threading.Event()

        # Decorated slots keep signal delivery on Qt's thread, even when the
        # original status/transcription signal is emitted by a worker.
        bridge = self

        class Events(QObject):
            @pyqtSlot(str)
            def status_changed(self, value):
                bridge.status = value

            @pyqtSlot(str, object)
            def transcript_received(self, value, raw):
                bridge.transcript = value
                bridge.raw = raw or value
                bridge.preview = ""

            @pyqtSlot(list)
            def audio_levels(self, values):
                if values:
                    bridge.levels.append(max(0.0, min(1.0, float(values[-1]))))

            @pyqtSlot(str, bool)
            def partial_transcription(self, value, is_final):
                bridge.preview = value

            @pyqtSlot(object)
            def history_persisted(self, result):
                bridge._history_dirty = True

        self.events = Events()
        ui = controller.ui_controller
        ui.status_changed.connect(self.events.status_changed)
        ui.transcription_received.connect(self.events.transcript_received)
        if hasattr(ui, "audio_levels_updated"):
            ui.audio_levels_updated.connect(self.events.audio_levels)
        if hasattr(controller, "partial_transcription"):
            controller.partial_transcription.connect(self.events.partial_transcription)
        if hasattr(controller, "history_persisted"):
            controller.history_persisted.connect(self.events.history_persisted)

    def start(self):
        threading.Thread(target=self._serve, name="rune-stdio", daemon=True).start()

    def _serve(self):
        from services.automation.app_control import call_on_qt
        from PyQt6.QtWidgets import QApplication

        try:
            while not self.closed.is_set():
                line = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
                if not line:
                    break
                request_id = None
                try:
                    if len(line) > MAX_REQUEST_BYTES:
                        raise ValueError("Request is too large.")
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("Expected a request object.")
                    request_id = request.get("id")
                    result = call_on_qt(lambda: self.dispatch(request))
                    response = {"id": request_id, "result": result}
                except Exception as exc:
                    response = {"id": request_id, "error": str(exc)}
                self.output.write(json.dumps(response, ensure_ascii=False) + "\n")
                self.output.flush()
                if len(line) > MAX_REQUEST_BYTES:
                    break
        except (BrokenPipeError, OSError):
            pass
        finally:
            self.closed.set()
            # EOF means the owning desktop has closed. Use the normal cleanup
            # path, including persistence drains and recording cancellation.
            try:
                call_on_qt(QApplication.instance().quit)
            except Exception:
                pass

    def dispatch(self, request):
        from services.automation.app_control import QtAppControl
        from services.settings import settings_manager

        c = self.controller
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("Expected parameter object.")
        control = QtAppControl(c)
        if method == "snapshot":
            return self.snapshot()
        if method == "record.start":
            self.transcript = self.raw = self.preview = ""
            self.levels = deque([0.0] * 49, maxlen=49)
            return control.start_recording(source="ui")
        if method == "record.stop":
            control.stop_recording()
        elif method == "cancel":
            control.cancel()
            self.preview = ""
        elif method == "file.transcribe":
            path = Path(str(params.get("path", ""))).expanduser()
            if not path.is_file():
                raise ValueError("Choose an existing audio or video file.")
            self._idle(control)
            self.transcript = self.raw = ""
            c.upload_audio_file(str(path.resolve()))
        elif method == "settings.update":
            self._idle(control)
            key, value = params.get("key"), params.get("value")
            if key in BOOLEAN_SETTINGS and isinstance(value, bool):
                settings_manager.save_setting(key, value)
            elif key == "local_asr_language" and isinstance(value, str) and value in LANGUAGES:
                settings_manager.save_setting(key, value)
            else:
                raise ValueError("This setting cannot be changed here.")
            c.ui_controller._apply_agent_changes("settings", {key: value})
        elif method == "appearance.update":
            theme = params.get("theme")
            if theme not in ("light", "dark"):
                raise ValueError("Choose a light or dark appearance.")
            settings_manager.save_setting("ui_theme", theme)
            c.ui_controller._apply_ui_theme(theme)
        elif method == "history.detail":
            from services.database import db

            entry = db.get_history_entry_by_id(str(params.get("id", "")))
            if entry is None:
                raise ValueError("This transcript is no longer in History.")
            return {"id": entry.id, "text": entry.text, "raw": entry.raw_text or entry.text,
                    "title": entry.title or entry.source_name or "Dictation"}
        elif method == "meeting.start":
            self._idle(control)
            c.meeting_runtime.start_meeting()
        elif method == "meeting.stop":
            c.meeting_runtime.end_meeting()
        elif method == "meeting.dashboard":
            c.meeting_runtime.open_dashboard()
        elif method == "walkthrough.toggle":
            c.walkthrough_runtime.toggle(source="ui")
        elif method == "walkthrough.snap":
            c.walkthrough_runtime.snap(source="ui")
        elif method == "window.settings":
            c.ui_controller.main_window.open_settings()
        elif method == "window.downloads":
            c.ui_controller.main_window.open_downloads()
        elif method == "window.classic":
            from ui_qt.widgets.tabbed_content import TabbedContentWidget

            page = params.get("page")
            if page not in (None, "meeting", "walkthrough"):
                raise ValueError("Unknown desktop page.")
            c.ui_controller.show_main_window()
            window = c.ui_controller.main_window
            if page is not None:
                index = (TabbedContentWidget.TAB_MEETING_MODE if page == "meeting"
                         else TabbedContentWidget.TAB_WALKTHROUGH)
                window.tabbed_content.set_current_index(index)
                if not window.history_sidebar.is_expanded():
                    window.toggle_history()
        else:
            raise ValueError("Unknown desktop action.")
        return {"ok": True}

    @staticmethod
    def _idle(control):
        state = control.state()
        if state.recording or state.busy or state.meeting:
            raise ValueError("Finish the current recording or meeting first.")

    def snapshot(self):
        from _version import __version__
        from services.automation.app_control import QtAppControl
        from services.database import db
        from services.settings import settings_manager

        c = self.controller
        saved = settings_manager.load_all_settings()
        state = QtAppControl(c).state().to_dict()
        if c._startup_database_ready and (
            self._history_dirty or time.monotonic() - self._history_refreshed >= 5.0
        ):
            history = []
            for entry in db.get_history_entries(limit=60):
                history.append({
                    "id": entry.id, "title": entry.title or entry.source_name or "Dictation",
                    "preview": entry.text[:180], "timestamp": entry.timestamp,
                    "duration": entry.audio_duration or 0,
                    "kind": entry.entry_kind or ("file" if entry.source_name else "dictation"),
                    "words": len(entry.text.split()), "model": entry.model,
                })
            self._history = history
            self._history_refreshed = time.monotonic()
            self._history_dirty = False
        return {
            **state, "version": __version__,
            "status": ("Ready when you are" if c._startup_database_ready
                       and self.status == "Preparing your workspace…" else self.status),
            "transcript": self.transcript, "raw": self.raw, "history": self._history,
            "preview": self.preview, "levels": list(self.levels),
            "engine": settings_manager.load_model_selection(),
            "ready": c._startup_database_ready,
            "settings": {key: saved.get(key, default) for key, default in (
                ("auto_paste", True), ("transcript_cleanup_enabled", False),
                ("copy_clipboard", True), ("local_asr_language", "en"), ("ui_theme", "light"),
            )},
        }
