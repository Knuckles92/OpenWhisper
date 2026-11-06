"""Native controls for backup, paired computers, updates and first-run setup."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from services.native_desktop.serialization import serializable

from .common import boolean, changed, identifier, integer, settings, string


def _background(context, name, work):
    """Keep slow network calls off the owner loop, with observable outcomes."""
    operations = context.ui.state.setdefault("system_operations", {})
    if operations.get(name, {}).get("status") == "running":
        raise ValueError("This operation is already running.")
    operation_id = uuid4().hex

    def publish(status, **fields):
        operation = {"id": operation_id, "kind": name, "status": status,
                     "updated_at": time.monotonic(), **serializable(fields)}
        context.ui.state["system_operations"] = {
            **context.ui.state.get("system_operations", {}), name: operation}
        context.emit("system.operation", operation)

    publish("running")

    def finished(future):
        try:
            result = future.result()
        except Exception as exc:
            publish("failed", error=str(exc))
        else:
            publish("done", result=result)

    context.controller.executor.submit(work).add_done_callback(finished)
    return changed("remote.state", operation=operation_id)


def _remote(context):
    return context.controller.remote_engine


def remote_state(context, _params):
    engine = _remote(context)
    pairing = engine.client_pairing()
    operations = context.ui.state.get("system_operations", {})
    return {"host": serializable(engine.host_state()),
        "client": {"paired": pairing is not None, "pairing": serializable(pairing),
            "name": pairing.host_name if pairing else "", "address": pairing.address if pairing else "",
            "pending_name": engine.pending_pairing_name(), "code": context.ui.state.get("pairing_code", "")},
        "sync": serializable(context.controller.record_sync.status()),
        "operations": operations,
        "operation": max(operations.values(), key=lambda value: value.get("updated_at", 0), default={})}


def remote_configure(context, params):
    methods = {"enabled": "set_host_enabled", "model_management": "set_model_management",
        "keep_records": "set_keep_records", "manage_mcp": "set_manage_mcp",
        "tailscale_trust": "set_tailscale_trust", "share_history": "set_share_history"}
    if not params or set(params) - {*methods, "port", "records_location"}:
        raise ValueError("Unknown remote setting.")
    values = {key: boolean(value, key) for key, value in params.items() if key in methods}
    if "port" in params:
        values["port"] = integer(params["port"], "port", 1, 65535)
    if "records_location" in params:
        from services.remote_asr.settings import RECORD_LOCATIONS
        if params["records_location"] not in RECORD_LOCATIONS:
            raise ValueError("Unknown record storage location.")
        values["records_location"] = params["records_location"]

    def apply():
        engine = _remote(context)
        for key, value in values.items():
            if key == "port":
                engine.set_host_port(value)
            elif key == "records_location":
                context.controller.record_sync.set_location(value)
            else:
                getattr(engine, methods[key])(value)
        return remote_state(context, {})

    return _background(context, "remote.configure", apply)


def remote_scan(context, params):
    scope = params.get("scope", "nearby")
    if scope not in ("nearby", "lan", "tailnet"):
        raise ValueError("Choose nearby, LAN, or Tailscale computers.")
    sweep = boolean(params.get("sweep", False), "sweep")

    def scan():
        engine = _remote(context)
        result = (engine.scan_nearby(sweep=sweep) if scope == "nearby" else
                  engine.scan_lan(sweep=sweep) if scope == "lan" else engine.scan_tailnet())
        hosts = result if isinstance(result, list) else result.hosts
        items = [{**serializable(host), "address": host.address,
                  "name": getattr(host, "name", "") or getattr(host, "host_name", ""),
                  "can_pair_without_code": getattr(host, "can_pair_without_code", False)} for host in hosts]
        return {"items": items, "hosts": items, "scope": scope,
                "status": serializable(getattr(result, "status", None))}

    return _background(context, "remote.scan", scan)


def remote_pair(context, params):
    address = string(params.get("address"), "host address", required=True, maximum=512)
    code = params.get("code")
    if code is not None:
        string(code, "pairing code", maximum=16)
    tailscale = boolean(params.get("tailscale", False), "tailscale")
    return _background(context, "remote.pair", lambda: _remote(context).pair(address, code, tailscale=tailscale))


def remote_pair_request(context, params):
    address = string(params.get("address"), "host address", required=True, maximum=512)
    context.ui._remote_pair_cancel = threading.Event()

    def on_code(code):
        context.ui.state["pairing_code"] = code
        context.emit("remote.pair.code", {"code": code})

    def pair():
        try:
            return _remote(context).request_pairing(address, on_code=on_code,
                canceled=context.ui._remote_pair_cancel)
        finally:
            context.ui.state.pop("pairing_code", None)

    return _background(context, "remote.pair", pair)


def remote_pair_cancel(context, _params):
    cancel = getattr(context.ui, "_remote_pair_cancel", None)
    if cancel is not None:
        cancel.set()
    return changed("remote.state")


def remote_forget(context, _params):
    remote_pair_cancel(context, {})
    _remote(context).forget_host()
    return changed("remote.state", "models.list")


def remote_disconnect(context, _params):
    from config import config
    if context.controller.recorder.is_recording or context.controller.is_transcribing() or context.controller.is_meeting_active():
        raise ValueError("Finish the current recording or meeting before changing the engine.")
    if context.controller._current_model_name == "remote":
        context.controller.on_model_changed(config.DEFAULT_BACKEND)
    return changed("remote.state", "models.list", "settings.schema")


def remote_pair_open(context, _params):
    code = _remote(context).open_pairing()
    if code is None:
        raise ValueError("Turn on engine sharing before opening pairing.")
    return changed("remote.state", code=code)


def remote_pair_answer(context, params):
    accepted = _remote(context).answer_pair_request(identifier(params), boolean(params.get("allow"), "allow"))
    if not accepted:
        raise ValueError("This pairing request is no longer open.")
    return changed("remote.state")


def remote_device_remove(context, params):
    device_id = identifier(params)
    delete_records = boolean(params.get("delete_records", False), "delete_records")
    return _background(context, "remote.device.remove",
        lambda: {"removed": _remote(context).remove_device(device_id, delete_records=delete_records)})


def remote_device_rename(context, params):
    result = _remote(context).rename_device(identifier(params), string(params.get("name", ""), "name", maximum=80))
    if result is None:
        raise ValueError("That computer is no longer paired.")
    return changed("remote.state")


def remote_rename(context, params):
    if _remote(context).rename_host(string(params.get("name", ""), "name", maximum=80)) is None:
        raise ValueError("Pair with a computer before renaming it.")
    return changed("remote.state")


def remote_sync(context, params):
    action = params.get("action", "retry")
    sync = context.controller.record_sync
    if action == "retry":
        sync.wake(now=True)
        return changed("remote.state")
    if action not in ("send_existing", "bring_back"):
        raise ValueError("Unknown record synchronization action.")
    return _background(context, "remote.sync", lambda: {
        "count": sync.send_existing() if action == "send_existing" else sync.bring_back()})


def _backup(context):
    coordinator = context.controller.backup_coordinator
    if coordinator is None:
        raise ValueError("Backup service is still starting.")
    return coordinator


def backup_state(context, _params):
    return {**serializable(_backup(context).snapshot()), **context.ui.state.get("backup", {})}


def backup_configure(context, params):
    frequency = params.get("frequency", "off")
    if frequency not in ("off", "daily", "weekly"):
        raise ValueError("Choose Off, Daily, or Weekly.")
    retention = integer(params.get("retention_count", 5), "retention count", 1, 100)
    destination = string(params.get("destination", ""), "backup folder", maximum=4096)
    _backup(context).set_schedule(frequency, retention, destination)
    return changed("backup.state")


def _path(params, *, existing=False):
    path = Path(string(params.get("path"), "archive path", required=True, maximum=4096)).expanduser()
    if not path.is_absolute() or (not path.is_file() if existing else not path.parent.is_dir()):
        raise ValueError("Choose an absolute archive path in an existing folder.")
    return str(path)


def backup_create(context, params):
    path = _path(params)
    if Path(path).exists():
        raise ValueError("Choose a new filename for the backup.")
    _backup(context).create_backup(path, boolean(params.get("include_recordings", True), "include_recordings"))
    return changed("backup.state")


def backup_validate(context, params):
    _backup(context).inspect_backup(_path(params, existing=True))
    return changed("backup.state")


def backup_restore(context, params):
    path = _path(params, existing=True)
    if params.get("confirmed") is not True:
        raise ValueError("Confirm that restoring replaces this computer's OpenWhisper data and restarts the app.")
    _backup(context).prepare_restore(path)
    return changed("backup.state")


def updates_state(context, _params):
    from services.app_update import InstallChannel, RELEASES_PAGE_URL, channel_label, detect_channel, source_update_hint
    channel = detect_channel()
    result = {**context.ui.state.get("update", {}),
        "progress": context.ui.state.get("update_progress", {}),
        "download": context.ui.state.get("update_download", {}),
        "channel": channel, "channel_label": channel_label(channel), "source_hint": source_update_hint(channel),
        "releases_url": RELEASES_PAGE_URL, "manual_update": channel == InstallChannel.NATIVE_PACKAGE}
    if channel == InstallChannel.NATIVE_PACKAGE:
        # A stale result or handoff from the old Qt installer must not turn
        # into an install action in a relocated native service package.
        if isinstance(result.get("result"), dict):
            result["result"] = {**result["result"], "can_apply": False, "apply_mode": "notify_only", "channel": channel}
        result["download"], result["progress"] = {}, {}
    return result


def _require_update_installation():
    from services.app_update import InstallChannel, NATIVE_PACKAGE_UPDATE_HINT, detect_channel
    if detect_channel() == InstallChannel.NATIVE_PACKAGE:
        raise ValueError(NATIVE_PACKAGE_UPDATE_HINT)


def updates_download(context, params):
    _require_update_installation()
    result = getattr(context.ui, "_last_update_result", None)
    if result is None or not result.can_apply:
        raise ValueError("Check for an installable update first.")
    previous = context.ui.state.pop("update_download", {})
    if previous.get("handoff"):
        context.controller.discard_update_handoff(previous["handoff"])
    context.controller.request_update_download(result, boolean(params.get("force_setup", False), "force_setup"))
    return changed("updates.state")


def updates_cancel(context, _params):
    context.controller.cancel_update_download()
    previous = context.ui.state.get("update_download", {})
    if previous.get("handoff"):
        context.controller.discard_update_handoff(previous["handoff"])
    context.ui.state["update_download"] = {"handoff": None, "error": "The update was canceled."}
    return changed("updates.state")


def updates_apply(context, params):
    _require_update_installation()
    from services.app_update_apply import (
        _launch_exe,
        discover_install_registration,
        helper_argv,
        helper_exe_for,
        load_journal,
        release_application_mutex_for_setup,
        running_app_dir,
    )
    from services.update_contract import decode_native_result
    handoff = context.ui.state.get("update_download", {}).get("handoff")
    if not handoff or context.ui.state["update_download"].get("error"):
        raise ValueError("Download and verify an update before installing it.")
    if params.get("confirmed") is not True:
        raise ValueError("Confirm the restart to install this update.")
    if context.controller.recorder.is_recording or context.controller.is_transcribing() or context.controller.is_meeting_active():
        raise ValueError("Finish recording, transcription, or the meeting before installing the update.")
    from services.components import component_coordinator
    from services.hf_access import hf_access_coordinator
    if component_coordinator.is_any_installing() or hf_access_coordinator.requests_in_flight:
        raise ValueError("Finish model and component downloads before installing the update.")
    if context.controller.backup_coordinator is not None and context.controller.backup_coordinator.busy:
        raise ValueError("Finish the backup or restore before installing the update.")
    if not context.ui.flush_scratchpad() and context.ui.state["scratchpad_save"]["dirty"]:
        raise ValueError("Save your latest Scratchpad notes before installing the update.")
    native_transaction = decode_native_result(handoff)
    if native_transaction:
        journal = load_journal(native_transaction)
        _launch_exe(helper_exe_for(journal), helper_argv(journal))
    elif sys.platform == "darwin":
        from services.app_update_macos import (
            READY_WAIT_S,
            decode_result,
            helper_ready,
            start_helper,
        )
        transaction = decode_result(handoff)
        if transaction:
            from services.desktop_runtime import QCoreApplication
            process = start_helper(transaction)
            deadline = time.monotonic() + READY_WAIT_S
            while not helper_ready(transaction):
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise ValueError("The updater could not be started.")
                QCoreApplication.processEvents()
                time.sleep(0.02)
        else:
            subprocess.Popen(["open", handoff], close_fds=True)
    elif sys.platform == "win32":
        from services.app_update import updates_dir
        registration = discover_install_registration()
        scope = "/ALLUSERS" if registration and registration.hive == "HKLM" else "/CURRENTUSER"
        release_application_mutex_for_setup()
        _launch_exe(handoff, ["/SP-", "/SILENT", "/NORESTART", "/OPENWHISPERUPDATE=1", scope,
            "/DIR=" + running_app_dir(), "/LOG=" + os.path.join(updates_dir(), "setup.log")])
    else:
        raise ValueError("Use your package manager or source checkout to update this installation.")
    context.emit("lifecycle", {"state": "updating"})
    context.shutdown()
    return {"ok": True}


def stats_get(_context, _params):
    from services.dictation_stats import load_summary
    summary = load_summary()
    result = serializable(summary)
    for period, values in zip(summary.periods, result["periods"], strict=True):
        values["minutes_saved"] = period.minutes_saved
    return result


def stats_reset(_context, params):
    if params.get("confirmed") is not True:
        raise ValueError("Confirm the statistics reset.")
    from services.dictation_stats import reset
    reset()
    return changed("stats.get")


def setup_state(context, _params):
    from services.guided_setup import should_invite, speech_model_downloaded
    from services.settings import SettingsKey
    saved = settings(context).load_all_settings()
    return {"state": saved.get(SettingsKey.GUIDED_SETUP_STATE, "existing"),
        "should_invite": should_invite(settings(context)),
        "speech_model_downloaded": speech_model_downloaded(saved),
        "engine": settings(context).load_model_selection(),
        "engine_ready": bool(context.controller.current_backend and context.controller.current_backend.is_available()),
        "hotkeys": settings(context).load_hotkey_settings(),
        "message": context.ui.state.get("device_info", "")}


def setup_save(state):
    def save(context, _params):
        from services.guided_setup import save_state
        save_state(state, settings(context))
        return changed("setup.state", "settings.schema")
    return save


def mcp_state(_context, _params):
    from services.agent_mcp.runtime import runtime
    status = runtime.status()
    return {**serializable(status), "url": status.url}


def mcp_copy(context, params):
    from services.agent_mcp import setup
    from services.agent_mcp.runtime import runtime
    format_name = params.get("format", "config")
    if format_name not in ("claude", "chatgpt", "config", "prompt", "token", "channel_config", "channel_command"):
        raise ValueError("Choose a supported MCP setup format.")
    status = runtime.status()
    if format_name == "channel_config":
        text = setup.claude_channel_config()
    elif format_name == "channel_command":
        text = setup.claude_channel_command()
    else:
        if status.state != "running":
            raise ValueError("Enable API and MCP before copying connection instructions.")
        token = runtime.token()
        use_tailscale = boolean(params.get("tailscale", False), "tailscale")
        url = status.remote_url if use_tailscale else status.url
        if not url:
            raise ValueError("Enable Tailscale access before copying its connection instructions.")
        text = (token if format_name == "token" else
            setup.claude_command(url, token) if format_name == "claude" else
            setup.chatgpt_config(url, token) if format_name == "chatgpt" else
            setup.agent_prompt(url, token=token) if format_name == "prompt" else
            setup.client_config(url, token))
    if not context.ui.copy_to_clipboard(text):
        raise ValueError("Couldn't copy the connection instructions to the clipboard.")
    return {"ok": True}


def screenshot_complete(context, params):
    session = getattr(context.ui, "_snap_sessions", {}).get(identifier(params))
    if session is None:
        raise ValueError("This screenshot is no longer open.")
    session.complete(params)
    return {"ok": True}


def scratchpad_focus(context, params):
    context.ui.state["scratchpad_focused"] = boolean(params.get("active"), "active")
    if "text" in params:
        context.ui.update_scratchpad(string(params["text"], "scratchpad text", maximum=5_000_000))
    return {"ok": True}


def scratchpad_flush(context, _params):
    saved = context.ui.flush_scratchpad()
    if not saved and context.ui.state["scratchpad_save"]["dirty"]:
        raise ValueError("Your latest Scratchpad notes could not be saved. Check free space and file permissions.")
    return {"ok": True, "save": context.ui.state["scratchpad_save"]}


def window_state(context, params):
    context.ui.state["window_active"] = boolean(params.get("active"), "active")
    return {"ok": True}


def hotkeys_capture(context, params):
    active = boolean(params.get("active"), "active")
    context.controller.hotkey_runtime.set_capture_suspended(active)
    context.ui.state["hotkey_capture_active"] = active
    return {"ok": True, "active": active}


def hotkeys_event(context, params):
    """Forward actual native key events through the existing source debouncer.

    Frontends must report real releases. This endpoint does not synthesize
    one, so global OS hooks retain push-and-hold and double-tap semantics.
    Windows' suppressing hook owns its events and needs no focused fallback.
    """
    from services.hotkey_manager import parse_hotkey

    key = string(params.get("key"), "key", required=True, maximum=80)
    pressed = boolean(params.get("pressed"), "pressed")
    repeat = boolean(params.get("repeat", False), "repeat")
    modifiers = params.get("modifiers", [])
    allowed_modifiers = {"ctrl", "control", "alt", "shift", "cmd", "super", "win", "windows", "meta"}
    if (not isinstance(modifiers, list) or len(modifiers) > 4
            or any(not isinstance(value, str) or value not in allowed_modifiers for value in modifiers)):
        raise ValueError("Supply supported key modifiers.")
    if modifiers and "+" in key:
        raise ValueError("Supply the main key separately from its modifiers.")
    normalized_modifiers, main_key = parse_hotkey("+".join([*modifiers, key]))
    manager = context.controller.hotkey_manager
    handler = getattr(manager, "handle_hotkey_press" if pressed else "handle_hotkey_release", None)
    if repeat or not main_key or not callable(handler):
        return {"ok": True, "handled": False}
    return {"ok": True, "handled": bool(handler(normalized_modifiers, main_key, source="native"))}


COMMANDS = {
    "remote.state": remote_state, "remote.configure": remote_configure, "remote.scan": remote_scan,
    "remote.pair": remote_pair, "remote.pair.request": remote_pair_request,
    "remote.pair.cancel": remote_pair_cancel, "remote.forget": remote_forget,
    "remote.disconnect": remote_disconnect,
    "remote.pair.open": remote_pair_open,
    "remote.pair.close": lambda c, p: (_remote(c).cancel_pairing() or changed("remote.state")),
    "remote.pair.answer": remote_pair_answer,
    "remote.pair.retry_save": lambda c, p: _background(c, "remote.pair", _remote(c).retry_pairing_save),
    "remote.rename": remote_rename,
    "remote.devices.remove": remote_device_remove, "remote.devices.rename": remote_device_rename,
    "remote.sync": remote_sync,
    "backup.state": backup_state, "backup.configure": backup_configure, "backup.create": backup_create,
    "backup.validate": backup_validate, "backup.restore": backup_restore,
    "backup.cancel": lambda c, p: (_backup(c).cancel() or changed("backup.state")),
    "updates.state": updates_state,
    "updates.check": lambda c, p: (c.controller.request_update_check(manual=True) or changed("updates.state")),
    "updates.download": updates_download,
    "updates.cancel": updates_cancel,
    "updates.apply": updates_apply,
    "stats.get": stats_get, "stats.reset": stats_reset,
    "setup.state": setup_state, "setup.start": setup_save("started"),
    "setup.complete": setup_save("completed"), "setup.skip": setup_save("skipped"),
    "mcp.state": mcp_state, "mcp.copy": mcp_copy,
    "scratchpad.focus": scratchpad_focus, "window.state": window_state,
    "scratchpad.flush": scratchpad_flush,
    "hotkeys.capture": hotkeys_capture, "hotkeys.event": hotkeys_event,
    "screenshot.complete": screenshot_complete,
}
