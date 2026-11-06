"""Explicit controller presentation adapter for the Rune desktop.

This adapter owns serializable view state, never windows. Prompts travel over
the private desktop pipe and their answers continue the existing service flow.
"""
from __future__ import annotations

import itertools
import threading
from collections.abc import Callable

from services.desktop_runtime import QCoreApplication, QObject, QThread, pyqtSignal
from services.native_desktop.serialization import serializable


class NativeMainView:
    """The small set of main-view operations shared application services use."""
    def __init__(self, ui):
        self.ui = ui
        self.is_recording = False
        self.enabled = True

    def _update_recording_state(self):
        self.ui.state["recording"] = bool(self.is_recording)
        self.ui.emit("recording.state", {"recording": bool(self.is_recording)})

    def set_partial_transcription(self, text, is_final=False):
        self.ui.state["preview"] = text
        self.ui.emit("transcription.preview", {"text": text, "final": is_final})

    def clear_partial_transcription(self):
        self.set_partial_transcription("")

    def refresh_history(self):
        self.ui.refresh_history()

    def refresh_past_meetings(self):
        self.ui.emit("meetings.changed", {})

    def toggle_tray_visibility(self):
        self.ui.emit("window.toggle", {})

    def restore_from_tray(self):
        self.ui.emit("window.show", {})

    def centralWidget(self):
        return self

    def isEnabled(self):
        return self.enabled

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)
        self.ui.state["enabled"] = self.enabled
        self.ui.emit("window.enabled", {"enabled": self.enabled})

    def isActiveWindow(self):
        return bool(self.ui.state.get("window_active", False))


class NativeOverlay:
    def __init__(self, ui):
        self.ui = ui

    def set_recording_destination(self, destination):
        self.ui.state["destination"] = destination
        self.ui.emit("recording.destination", {"destination": destination})


class NativeWalkthroughPanel(QObject):
    snap_requested = pyqtSignal()
    snap_mode_chosen = pyqtSignal(str)
    finish_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    undo_requested = pyqtSignal()
    move_requested = pyqtSignal(int, int)
    remove_requested = pyqtSignal(int)
    note_held = pyqtSignal(bool)
    pause_requested = pyqtSignal(bool)

    def __init__(self, ui):
        super().__init__()
        self.ui = ui

    def update(self, **values):
        self.ui.state["walkthrough_live"].update(serializable(values))
        self.ui.emit("walkthrough.state", self.ui.state["walkthrough_live"])

    def set_snap_mode(self, mode, shortcut=""):
        self.update(snap_mode=mode, shortcut=shortcut)

    def set_note_enabled(self, enabled):
        self.update(note_enabled=enabled)

    def set_notes_on(self, enabled):
        self.update(note_enabled=enabled)

    def set_note_held(self, held):
        self.update(note_held=held)

    def set_countdown(self, seconds):
        self.update(countdown=seconds)

    def set_paused(self, paused):
        self.update(paused=paused)

    def add_shot(self, number, image, kept_at_s):
        shots = self.ui.state["walkthrough_live"]["shots"]
        shots.append({"index": number, "number": number, "kept_at_s": kept_at_s,
                      "image_path": str(getattr(image, "path", ""))})
        self.update(shots=shots)

    def remove_shot(self, number):
        self.update(shots=[s for s in self.ui.state["walkthrough_live"]["shots"]
                           if s["number"] != number])

    def move_shot(self, number, step):
        shots = self.ui.state["walkthrough_live"]["shots"]
        index = next((i for i, s in enumerate(shots) if s["number"] == number), None)
        if index is not None and 0 <= index + step < len(shots):
            shots.insert(index + step, shots.pop(index))
            self.update(shots=shots)

    def set_writing_up(self, writing_up):
        self.update(writing_up=writing_up)


class NativeUIController(QObject):
    status_changed = pyqtSignal(str)
    transcription_received = pyqtSignal(str, object)
    audio_levels_updated = pyqtSignal(list)
    agent_data_changed = pyqtSignal()
    _audio_level_requested = pyqtSignal()
    _scratchpad_state_changed = pyqtSignal(object)

    def __init__(self, emit: Callable[[str, dict], None], clipboard=None):
        super().__init__()
        self.emit = emit
        self.state = {
            "status": "Preparing your workspace…", "transcript": "", "raw": "",
            "preview": "", "levels": [0.0] * 49, "recording": False,
            "enabled": True, "engine_busy": False, "model_downloads": {},
            "components": {}, "prompts": [], "stats": {}, "scratchpad": "",
            "file_queue": {"items": [], "active": False},
            "meeting_live": {"active": False, "paused": False, "title": "",
                "id": "", "meeting_id": "",
                "transcript": "", "segments": [], "summary": "", "finalization": {},
                "status": "", "dashboard_url": ""},
            "walkthrough_live": {"active": False, "paused": False, "shots": []},
        }
        self.main_window = NativeMainView(self)
        self.overlay = NativeOverlay(self)
        self.walkthrough_panel = NativeWalkthroughPanel(self)
        self.is_recording = False
        self.streaming_flow_active = False
        self._closed = threading.Event()
        self._prompt_ids = itertools.count(1)
        self._prompts: dict[str, dict] = {}
        self._prompt_lock = threading.Lock()
        self._clipboard = clipboard
        self._last_original = ""
        self._history_generation = 0
        self._audio_level_lock = threading.Lock()
        self._audio_level_pending = None
        self._audio_level_queued = False
        self._audio_level_requested.connect(self._flush_audio_level)
        self._scratchpad_state_changed.connect(self._on_scratchpad_state)
        from services.native_desktop.scratchpad import ScratchpadStore
        self._scratchpad = ScratchpadStore(on_state=self._scratchpad_state_changed.emit)
        self.state["scratchpad"] = self._scratchpad.text
        self.state["scratchpad_save"] = self._scratchpad.snapshot()
        if self.state["scratchpad_save"]["error"]:
            self.show_notice(self.state["scratchpad_save"]["error"])

    @property
    def clipboard(self):
        if self._clipboard is None:
            from services.native_desktop.clipboard import NativeClipboard
            self._clipboard = NativeClipboard()
        return self._clipboard

    def request_prompt(self, kind, title, message, options, *, default=None, **extra):
        prompt_id = str(next(self._prompt_ids))
        prompt = {"id": prompt_id, "kind": kind, "title": title,
                  "message": message, "options": serializable(options), "default":default, **serializable(extra)}
        pending = {"event": threading.Event(), "value": default, "prompt": prompt}
        with self._prompt_lock:
            self._prompts[prompt_id] = pending
            self.state["prompts"] = [v["prompt"] for v in self._prompts.values()]
        self.emit("prompt", prompt)
        # The UI remains responsive while an existing synchronous controller
        # continuation waits for the user's answer, just as a modal dialog did.
        while not pending["event"].wait(0.02) and not self._closed.is_set():
            QCoreApplication.processEvents()
        with self._prompt_lock:
            self._prompts.pop(prompt_id, None)
            self.state["prompts"] = [v["prompt"] for v in self._prompts.values()]
        self.emit("prompt.closed", {"id": prompt_id})
        return pending["value"]

    def answer_prompt(self, prompt_id, value):
        with self._prompt_lock:
            pending = self._prompts.get(str(prompt_id))
            if pending is None:
                raise ValueError("This prompt is no longer open.")
            allowed = [v.get("value") if isinstance(v, dict) else v
                       for v in pending["prompt"]["options"]]
            if allowed and value not in allowed:
                raise ValueError("Choose one of the offered responses.")
            pending["value"] = value
            pending["event"].set()
        return {"ok": True}

    def cancel_prompts(self):
        self._closed.set()
        cancel_pairing = getattr(self, "_remote_pair_cancel", None)
        if cancel_pairing is not None:
            cancel_pairing.set()
        with self._prompt_lock:
            for pending in self._prompts.values():
                pending["event"].set()

    def set_status(self, status):
        self.state["status"] = str(status)
        self.status_changed.emit(str(status))
        self.emit("status", {"text": str(status)})

    def set_ambient_status(self, status):
        self.emit("notice", {"text": status, "ambient": True})

    def set_transcript(self, text, raw=None):
        self.state.update(transcript=text, raw=raw or text, preview="")
        self._last_original = raw or text
        self.transcription_received.emit(text, raw)
        self.emit("transcription.result", {"text": text, "raw": raw or text})
        queue = self.state["file_queue"]
        if queue["active"] and len(queue["items"]) == 1:
            queue["items"][0].update(status="done", error="")
            queue["active"] = False
            self.emit("file.queue", queue)

    def update_audio_levels(self, values):
        if not values or self._closed.is_set():
            return
        if QThread.currentThread() is self.thread():
            self._apply_audio_level(values)
            return
        # This is called under the recorder's capture lock. Transport pressure
        # must never delay audio; one queued callback keeps only the latest meter.
        with self._audio_level_lock:
            self._audio_level_pending = list(values)
            if self._audio_level_queued:
                return
            self._audio_level_queued = True
        self._audio_level_requested.emit()

    def _flush_audio_level(self):
        with self._audio_level_lock:
            values, self._audio_level_pending = self._audio_level_pending, None
            self._audio_level_queued = False
        if values is not None and not self._closed.is_set():
            self._apply_audio_level(values)

    def _apply_audio_level(self, values):
        level = max(0.0, min(1.0, float(values[-1])))
        self.state["levels"] = [*self.state["levels"][1:], level]
        self.audio_levels_updated.emit(list(values))
        self.emit("audio.level", {"level": level})

    def set_device_info(self, device_info, ready=None):
        self.state.update(device_info=device_info, engine_ready=ready)
        self.emit("engine.state", {"device_info": device_info, "ready": ready})

    def set_engine_busy(self, busy):
        self.state["engine_busy"] = bool(busy)
        self.emit("engine.busy", {"busy": bool(busy)})

    def set_transcription_stats(self, transcription_time, audio_duration, file_size,
                                cleanup_time=None, remote=None):
        self.state["stats"] = serializable(dict(transcription_time=transcription_time,
            audio_duration=audio_duration, file_size=file_size, cleanup_time=cleanup_time,
            remote=remote))
        self.emit("transcription.stats", self.state["stats"])

    def clear_transcription_stats(self):
        self.state["stats"] = {}

    def set_overlay_state(self, state):
        self.state["overlay_state"] = serializable(state)
        self.emit("overlay.state", {"state": serializable(state)})

    def show_overlay_caption(self, text):
        self.state["walkthrough_live"]["caption"] = text
        self.emit("overlay.caption", {"text": text})

    def show_notice(self, text, *, done=False):
        self.emit("notice", {"text": text, "done": done})

    def show_transcription_error(self, message):
        self.set_status(message)
        queue = self.state["file_queue"]
        if queue["active"]:
            for item in queue["items"]:
                if item.get("status") in ("queued", "running"):
                    item.update(status="failed", error=message)
            queue["active"] = False
            self.emit("file.queue", queue)
        self.emit("error", {"title": "Transcription", "message": message})

    def set_hands_free(self, on):
        self.state["hands_free"] = bool(on)
        self.emit("recording.hands_free", {"on": bool(on)})

    def on_dictation_started(self, profile=None):
        self.state.update(transcript="", raw="", preview="", levels=[0.0] * 49)
        self.emit("recording.started", {"profile": serializable(profile)})

    def on_recording_device_switched(self, old, new):
        self.show_notice(f"Microphone changed from {old} to {new}")

    def on_dictionary_term_learned(self, term):
        self.show_notice(f"Learned ‘{term}’", done=True)

    def show_streaming_overlay(self):
        self.state["streaming_visible"] = True
        self.emit("overlay.streaming", {"visible": True})

    def hide_streaming_overlay(self):
        self.state["streaming_visible"] = False
        self.emit("overlay.streaming", {"visible": False})

    def update_streaming_text(self, text, is_final=False):
        self.main_window.set_partial_transcription(text, is_final)

    def update_hotkey_display(self, hotkeys):
        self.state["hotkeys"] = dict(hotkeys)
        self.emit("hotkeys.changed", dict(hotkeys))

    def refresh_history(self):
        self._history_generation += 1
        self.emit("history.changed", {"generation": self._history_generation})

    def refresh_model_views(self):
        self.emit("models.changed", {})

    def refresh_local_engine_controls(self):
        self.emit("engine.changed", {})

    def refresh_remote_models(self):
        self.emit("remote.models.changed", {})

    def set_remote_link(self, link):
        self.state["remote_link"] = serializable(link)
        self.emit("remote.link", {"link": serializable(link)})

    def set_remote_clients(self, clients):
        self.state["remote_clients"] = serializable(clients)
        self.emit("remote.clients", {"clients": serializable(clients)})

    def on_model_download_started(self, model_name):
        self.state["model_downloads"][model_name] = {"status": "downloading", "done": 0, "total": 0}
        self.emit("models.download", {"model": model_name, **self.state["model_downloads"][model_name]})

    def on_model_download_progress(self, model_name, done, total):
        self.state["model_downloads"][model_name] = {"status": "downloading", "done": done, "total": total}
        self.emit("models.download", {"model": model_name, **self.state["model_downloads"][model_name]})

    def on_model_download_finished(self, model_name, success):
        self.state["model_downloads"][model_name] = {"status": "ready" if success else "failed"}
        self.emit("models.download", {"model": model_name, **self.state["model_downloads"][model_name]})
        self.refresh_model_views()

    def on_model_batch_planned(self, model_names):
        self.state["download_batch"] = {"models": list(model_names), "completed": 0}
        self.emit("models.batch", self.state["download_batch"])

    def on_model_batch_finished(self, completed, planned):
        self.state["download_batch"] = {"completed": completed, "planned": planned}
        self.emit("models.batch", self.state["download_batch"])

    def on_model_deleted(self, model_name, success, error):
        self.emit("models.deleted", {"model": model_name, "success": success, "error": error})
        self.refresh_model_views()

    def on_component_progress(self, component_id, phase, done, total):
        self.state["components"][component_id] = {"phase": phase, "done": done, "total": total}
        self.emit("components.progress", {"id": component_id, **self.state["components"][component_id]})

    def on_component_state_changed(self):
        self.emit("components.changed", {})

    def on_component_install_finished(self, component_id, success, message):
        self.state["components"][component_id] = {"success": success, "message": message}
        self.emit("components.finished", {"id": component_id, "success": success, "message": message})

    def set_batch_progress(self, position, total, source_name):
        self.state["batch"] = {"position": position, "total": total, "source_name": source_name}
        queue = self.state["file_queue"]
        queue["active"] = True
        if 0 < position <= len(queue["items"]):
            queue["items"][position - 1]["status"] = "running"
        self.emit("files.progress", self.state["batch"])
        self.emit("file.queue", queue)

    def set_batch_item_finished(self, position, success, transcript=""):
        queue = self.state["file_queue"]
        if 0 < position <= len(queue["items"]):
            queue["items"][position - 1].update(status="done" if success else "failed",
                error="" if success else transcript)
        queue["active"] = any(item.get("status") in ("queued", "running") for item in queue["items"])
        self.emit("files.item", {"position": position, "success": success, "transcript": transcript})
        self.emit("file.queue", queue)

    def set_batch_result(self, result):
        self.state["file_queue"]["active"] = False
        queue = self.state["file_queue"]
        for item in queue["items"]:
            if item.get("status") in ("queued", "running"):
                item.update(status="canceled", error="The batch ended before this file finished.")
        self.state["batch_result"] = serializable(result)
        self.emit("files.result", self.state["batch_result"])
        self.emit("file.queue", queue)

    def show_large_file_state(self, file_size_mb):
        self.show_notice(f"Processing large file ({file_size_mb:.1f} MB)…")

    def show_hf_consent_dialog(self, model_name, policy, env_blocked=False):
        from services.hf_access import ConsentAction
        options = [{"label": "Later", "value": ConsentAction.CANCEL}]
        if not env_blocked:
            options = [{"label": "Download once", "value": ConsentAction.DOWNLOAD_ONCE},
                       {"label": "Allow downloads", "value": ConsentAction.ALWAYS_ALLOW}, *options]
        return self.request_prompt("model.download", "Download speech model", model_name,
            options, default=ConsentAction.CANCEL, policy=policy, env_blocked=env_blocked)

    def show_required_runtime_dialog(self, model_name, component_id):
        return self.request_prompt("component.install", "Install speech engine", model_name,
            [{"label": "Install", "value": True}, {"label": "Later", "value": False}],
            default=False, component_id=component_id) is True

    def show_gpu_runtime_dialog(self, model_name, gpu_component, cpu_component):
        return self.request_prompt("component.gpu", "Choose speech runtime", model_name,
            [{"label": "Use GPU", "value": gpu_component}, {"label": "Use CPU", "value": cpu_component},
             {"label": "Later", "value": None}], gpu_component=gpu_component, cpu_component=cpu_component)

    def show_use_gpu_dialog(self, plan):
        return self.request_prompt("engine.gpu", "Use this GPU", "Use GPU acceleration for speech recognition?",
            [{"label": "Use GPU", "value": True}, {"label": "Keep current", "value": False}],
            default=False, plan=plan) is True

    def on_meeting_state_changed(self, payload):
        if isinstance(payload, dict):
            self.state["meeting_live"].update(serializable(payload))
            self.emit("meeting.state", self.state["meeting_live"])

    def on_meeting_live_event(self, kind, payload):
        live = self.state["meeting_live"]
        if kind == "segments":
            items = payload.get("items", [])
            segments = {str(item.get("id", index)): item
                        for index, item in enumerate(live.get("segments", []))}
            for removed_id in payload.get("removed_ids", []):
                segments.pop(str(removed_id), None)
            for index, item in enumerate(items):
                segments[str(item.get("id", index))] = serializable(item)
            live["segments"] = list(segments.values())
            live["transcript"] = "\n".join(str(item.get("text", "")) for item in live["segments"])
        elif kind == "intelligence":
            live["intelligence"] = serializable(payload)
        elif kind == "voice_feedback":
            live["voice_feedback"] = serializable(payload)
        else:
            return
        self.emit("meeting.live", live)

    def set_meeting_status(self, status):
        self.state["meeting_live"]["status"] = status
        self.set_status(status)

    def on_meeting_error(self, message):
        self.set_meeting_status(message)
        self.emit("error", {"title": "Meeting", "message": message})

    def on_meeting_server_started(self, result):
        result = serializable(result)
        meeting_id = str(result.get("meeting_id") or result.get("id") or "")
        if meeting_id and meeting_id != self.state["meeting_live"].get("meeting_id"):
            self.state["meeting_live"].update(segments=[], transcript="", summary="", finalization={})
        self.state["meeting_live"].update(result)
        if meeting_id:
            self.state["meeting_live"].update(id=meeting_id, meeting_id=meeting_id)
        self.state["meeting_live"]["dashboard_url"] = result.get("host_url") or result.get("url", "")
        self.emit("meeting.server", result)

    def ensure_meeting_start_readiness(self):
        import sys

        from meeting.platform import linux_meeting_implementation_ready
        if sys.platform.startswith("linux") and linux_meeting_implementation_ready():
            return self.request_prompt("meeting.audio", "Meeting audio", "Choose what this meeting should capture.",
                [{"label": "Microphone and system audio", "value": "required"},
                 {"label": "Microphone only", "value": "disabled"}, {"label": "Cancel", "value": None}])
        return "auto"

    def show_meeting_consent_dialog(self):
        return self.request_prompt("meeting.cloud", "Enable AI meeting insights",
            "Meeting text will be sent to your configured AI provider to produce insights.",
            [{"label": "Enable insights", "value": True}, {"label": "Keep local", "value": False}], default=False) is True

    def show_meeting_recovery_dialog(self, meetings, on_finalize=None, on_discard=None):
        for meeting in meetings:
            choice = self.request_prompt("meeting.recovery", "Interrupted meeting", "Recover saved meeting audio?",
                [{"label": "Recover", "value": "recover"}, {"label": "Discard", "value": "discard"},
                 {"label": "Later", "value": "later"}], default="later", meeting=meeting)
            meeting_id = meeting.get("id") or meeting.get("meeting_id")
            if choice == "recover" and on_finalize:
                on_finalize(meeting_id)
            elif choice == "discard" and on_discard:
                on_discard(meeting_id)

    def show_meeting_recovery_scan_error(self, message, on_retry):
        if self.request_prompt("meeting.recovery_error", "Meeting recovery", message,
            [{"label": "Retry scan", "value": True}, {"label": "Later", "value": False}], default=False):
            on_retry()

    def copy_meeting_guest_link(self, url):
        copied = self.copy_to_clipboard(url)
        self.show_notice("Guest link copied" if copied else "Couldn't copy guest link", done=copied)

    def open_settings_dialog(self):
        self.emit("navigate", {"page": "settings"})

    def open_downloads(self, component_id=None):
        self.emit("navigate", {"page": "models", "component_id": component_id})

    def select_transcription_backend(self, model_name):
        from config import config
        backend = config.MODEL_VALUE_MAP.get(model_name, model_name)
        callback = getattr(self, "on_model_changed", None)
        if not callable(callback):
            raise RuntimeError("The speech controller is not ready.")
        callback(backend)
        self.state["engine"] = backend
        self.emit("engine.selected", {"engine": backend})

    def toggle_scratchpad(self):
        self.emit("scratchpad.toggle", {"text": self.state["scratchpad"]})

    def show_in_scratchpad(self, text):
        self.update_scratchpad(text)
        self.emit("scratchpad.show", {"text": text})

    def update_scratchpad(self, text):
        self._scratchpad.set_text(text)
        self.state["scratchpad"] = text
        self.emit("scratchpad", {"text": text})

    def _on_scratchpad_state(self, state):
        previous = self.state.get("scratchpad_save", {})
        if state.get("revision", 0) < previous.get("revision", 0):
            return
        self.state["scratchpad_save"] = state
        self.emit("scratchpad.save", state)
        if state["error"]:
            self.show_notice(state["error"])

    def flush_scratchpad(self):
        saved = self._scratchpad.flush()
        state = self._scratchpad.snapshot()
        self._on_scratchpad_state(state)
        # An unread existing file has no new edits to flush. Keep it intact
        # while still allowing Quit or restoring a known-good backup.
        return saved or not state["dirty"]

    def insert_into_scratchpad(self, text):
        if not self.state.get("scratchpad_focused", False) or not self.state.get("window_active", False):
            return False
        previous = self.state["scratchpad"]
        self.update_scratchpad(previous + ("\n" if previous else "") + text)
        self.emit("scratchpad.insert", {"text": text, "content": self.state["scratchpad"]})
        return True

    def cycle_dictation_language(self):
        from services import dictation_language
        from services.settings import settings_manager
        settings_manager.mutate_settings(dictation_language.cycle)
        language = dictation_language.current_language(settings_manager.load_all_settings())
        self.state["dictation_language"] = language
        self.show_notice(f"Dictation language: {dictation_language.label(language)}")

    def paste_last_original(self):
        if self._last_original and getattr(self, "on_paste_text_now", None):
            self.on_paste_text_now(self._last_original)

    def show_accessibility_setup(self):
        from services.hotkey_manager import (
            accessibility_permission_instructions,
            request_accessibility_trust,
        )
        from services.settings import SettingsKey, settings_manager
        choice = self.request_prompt("platform.accessibility", "Set up auto-paste",
            accessibility_permission_instructions() or "Allow OpenWhisper in Accessibility settings to paste into other apps.",
            [{"label": "Open Accessibility settings", "value": "open"},
             {"label": "Continue with clipboard", "value": "later"}], default="later")
        settings_manager.save_setting(SettingsKey.MACOS_ACCESSIBILITY_INTRO_SEEN, True)
        if choice == "open":
            import subprocess
            import sys
            request_accessibility_trust()
            if sys.platform == "darwin":
                subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"], close_fds=True)

    def set_walkthrough_active(self, active):
        if active:
            self.state["walkthrough_live"] = {"active": True, "paused": False, "shots": []}
        else:
            self.state["walkthrough_live"]["active"] = False
        self.emit("walkthrough.state", self.state["walkthrough_live"])

    def dismiss_walkthrough_panel(self):
        self.set_walkthrough_active(False)

    def show_walkthrough_ready(self, result, *, copied):
        self.state["walkthrough_live"].update(active=False, result=serializable(result), copied=copied)
        self.emit("walkthrough.ready", self.state["walkthrough_live"])

    def show_walkthrough_library(self, folder=None):
        self.emit("navigate", {"page": "walkthroughs", "folder": serializable(folder)})

    def snap_support(self):
        from services.native_desktop.snap import snap_support
        return snap_support()

    def request_snap_access(self):
        import sys
        if sys.platform == "darwin":
            import Quartz
            Quartz.CGRequestScreenCaptureAccess()
            self.show_notice("Allow OpenWhisper in System Settings → Privacy & Security → Screen Recording, then try again.")

    def start_snap(self, *, hint="", take="region", front=False):
        from services.native_desktop.snap import NativeSnapSession
        return NativeSnapSession(self, hint=hint, take=take, front=front)

    def keep_overlay_out_of_screenshots(self, enabled):
        self.emit("overlay.capture_exclusion", {"enabled": bool(enabled)})
        return False

    def on_update_check_finished(self, result, error="", manual=False):
        if result is not None:
            self._last_update_result = result
        self.state["update"] = {"result": serializable(result), "error": error, "manual": manual}
        self.emit("update.checked", self.state["update"])

    def on_update_download_progress(self, phase, done, total):
        self.state["update_progress"] = {"phase": phase, "done": done, "total": total}
        self.emit("update.progress", {"phase": phase, "done": done, "total": total})

    def on_update_download_finished(self, handoff, error):
        self.state["update_download"] = {"handoff": serializable(handoff), "error": error}
        self.emit("update.finished", self.state["update_download"])

    def prefetch_clipboard_snapshot(self):
        self.clipboard.prefetch_clipboard_snapshot()

    def discard_clipboard_prefetch(self):
        self.clipboard.discard_clipboard_prefetch()

    def stage_transcript_for_paste(self, text, html=None, *, restore=True):
        return self.clipboard.stage_transcript_for_paste(text, html=html, restore=restore)

    def capture_selection(self, callback, *, timeout_ms=None):
        self.clipboard.capture_selection(callback, timeout_ms=timeout_ms)

    def schedule_clipboard_restore(self, stage):
        return self.clipboard.schedule_clipboard_restore(stage)

    def commit_transcript_clipboard(self, stage, text):
        return self.clipboard.commit_transcript_clipboard(stage, text)

    def copy_to_clipboard(self, text, html=None):
        return self.clipboard.copy_to_clipboard(text, html=html)

    def copy_files_to_clipboard(self, paths, text="", *, image=None):
        return self.clipboard.copy_files_to_clipboard(paths, text=text, image=image)

    def cleanup(self):
        self.cancel_prompts()
        self._scratchpad.close()
        self._on_scratchpad_state(self._scratchpad.snapshot())
        for session in list(getattr(self, "_snap_sessions", {}).values()):
            session.cancel()
        if self._clipboard is not None:
            self._clipboard.close()
        self.emit("lifecycle", {"state": "stopped"})
