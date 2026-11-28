"""Qt entry point and pre-import native-library bootstrap."""

import multiprocessing

if __name__ == "__main__":
    # Frozen workers reuse this executable. Divert them before imports or CLI
    # handling can initialize the app. Even tqdm's progress lock can start a
    # resource tracker; opening the UI there recursively launches more copies.
    multiprocessing.freeze_support()

import logging
import os
import subprocess
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message="pkg_resources is deprecated")


def _is_client_only_edition() -> bool:
    """Return True when running the Client-Only OpenWhisper edition."""
    try:
        from services.edition import is_client_only  # type: ignore[import-not-found]

        return bool(is_client_only())
    except ImportError:
        return os.environ.get("OPENWHISPER_EDITION", "").strip().lower() == "client"


def _run_package_self_test() -> None:
    """Import release-critical modules and initialize Qt without starting UI."""
    import importlib

    from services.package_checks import check_multiprocessing

    if getattr(sys, "frozen", False):
        check_multiprocessing()
    else:
        # Source spawn reimports this entrypoint as __mp_main__, retaining
        # --self-test in argv. Use a clean interpreter so the probe cannot
        # recursively launch self-tests during its own bootstrap.
        probe = "from services.package_checks import check_multiprocessing; check_multiprocessing()"
        subprocess.run(
            [sys.executable, "-c", probe],
            cwd=Path(__file__).resolve().parent,
            check=True, timeout=35,
        )

    modules = [
        "lxml",
        "openpyxl",
        "pypdf",
        "sounddevice",
        "sqlalchemy.dialects.sqlite",
        "uvicorn",
        "services.agent_mcp.app",
        "services.isolated_worker",
    ]
    if not _is_client_only_edition():
        modules = [
            "av",
            "ctranslate2",
            "faster_whisper",
            "onnxruntime",
            *modules,
        ]
    if sys.platform == "win32":
        modules.extend(("keyring.backends.Windows", "keyboard", "soundcard"))
    elif sys.platform == "darwin":
        modules.extend(
            (
                "keyring.backends.macOS",
                "pynput.keyboard",
                # Meeting Mode / Accessibility stack is imported lazily at
                # runtime; freeze self-test must prove the bindings survived.
                "objc",
                "Foundation",
                "HIServices",
                "ScreenCaptureKit",
                "CoreMedia",
                "Quartz",
                # Snap: window levels, capture exclusion, handing focus back.
                "AppKit",
            )
        )
    else:
        modules.extend(
            (
                "keyring.backends.SecretService",
                "pynput.keyboard",
                "secretstorage",
            )
        )
    for module_name in modules:
        importlib.import_module(module_name)

    # The UI's lazy public exports and Settings pages are invisible to static
    # freeze analysis. Import each one so a missing screen also fails the build.
    from services.package_checks import import_app_modules

    import_app_modules()

    from PyQt6.QtWidgets import QApplication

    qt_app = QApplication.instance() or QApplication([])

    from config import bundle_root

    root = Path(bundle_root())
    from services.http_tls import verified_context

    if not verified_context().get_ca_certs():
        raise RuntimeError("The application has no trusted HTTPS certificates.")
    for relative in (
        "ui_qt/styles/theme.qss",
        "ui_qt/assets/openwhisper.ico",
        "ui_qt/assets/openwhisper.png",
        "webui/dist/index.html",
    ):
        if not (root / relative).is_file():
            raise RuntimeError(f"Missing bundled asset: {relative}")

    from PyQt6.QtGui import QIcon

    svg_icon = QIcon(str(root / "ui_qt/assets/check.svg"))
    if svg_icon.isNull():
        raise RuntimeError("Qt could not load the bundled SVG image plugin.")
    from services.release_checks import run_workflow_smoke

    run_workflow_smoke()
    del qt_app
    print("OpenWhisper package self-test passed")


_USAGE = """\
Usage: openwhisper [OPTION | openwhisper://pair?... | FILE.openwhisper-invite]

With no option, opens OpenWhisper. Passing an openwhisper://pair URI or a
.openwhisper-invite file pairs with that host (forwarding to an already-running
OpenWhisper window when open).

Options:
  -h, --help              Show this help and exit.
  --version               Print the version and exit.
  --diagnostics OUTPUT.json
                          Write a sanitized support report to a new JSON file
                          and exit. Nothing is uploaded.
  --api [--database PATH] [--port N]
                          Serve the read-only History API on 127.0.0.1 (port
                          8766 by default) without opening the app. Set
                          OPENWHISPER_API_TOKEN to a random token of at least
                          32 characters first.
  --channel               Run as a Claude Code channel (an MCP server over
                          stdio) that forwards OpenWhisper actions into a
                          running Claude Code session. OpenWhisper must be
                          running with API & MCP on.
  --self-test             Check that the app's modules and bundled files load,
                          and exit.
  --workflow-smoke [REPORT.json]
                          Run a headless check of the dictation pipeline,
                          optionally saving its report, and exit.

Environment:
  OPENWHISPER_UI=omarchy|classic
                          Use the Omarchy or the classic look instead of
                          detecting the desktop.
  OPENWHISPER_DATA_DIR=PATH
                          Keep settings, history and recordings in PATH.
  OPENWHISPER_LOG_LEVEL=LEVEL
                          Log at DEBUG, INFO (the default), WARNING or ERROR.
"""


def _handle_early_cli() -> None:
    """Handle worker and metadata modes before native-library bootstrap."""
    if sys.argv[1:2] in (["-h"], ["--help"]):
        # Before anything loads Qt or CUDA: help must answer in a terminal
        # with no display, and at once.
        print(_USAGE, end="")
        raise SystemExit(0)
    if sys.argv[1:2] == ["--diagnostics"]:
        from services.diagnostics import main as diagnostics_main

        raise SystemExit(diagnostics_main(sys.argv[2:]))
    if sys.argv[1:2] == ["--api"]:
        from services.agent_api.cli import main

        raise SystemExit(main(sys.argv[2:]))
    if sys.argv[1:2] == ["--channel"]:
        # Claude Code starts this with stdio pipes; it must answer before
        # anything loads Qt or CUDA.
        from services.automation.channel_bridge import main

        raise SystemExit(main(sys.argv[2:]))
    if sys.argv[1:] == ["--local-asr-worker"]:
        from services.local_asr.worker import main

        main()
        raise SystemExit(0)
    if sys.argv[1:2] == ["--macos-update-helper"] and sys.platform == "darwin":
        from services.app_update_macos import run_helper

        raise SystemExit(run_helper(sys.argv[2:]))
    if sys.argv[1:] == ["--version"]:
        from _version import __version__

        print(f"OpenWhisper {__version__}")
        raise SystemExit(0)


def _handle_package_self_test() -> None:
    """Run the import test after platform DLL/library bootstrap is complete."""
    if sys.argv[1:] == ["--isolated-worker"]:
        from services.isolated_worker import main as isolated_worker_main

        isolated_worker_main()
        raise SystemExit(0)
    if sys.argv[1:2] == ["--workflow-smoke"]:
        from services.release_checks import run_workflow_smoke
        import json

        if len(sys.argv) > 3:
            raise SystemExit("Usage: --workflow-smoke [report.json]")
        report = run_workflow_smoke()
        if len(sys.argv) == 3:
            Path(sys.argv[2]).write_text(json.dumps(report, indent=2), encoding="utf-8")
        raise SystemExit(0)
    if sys.argv[1:] == ["--self-test"]:
        _run_package_self_test()
        raise SystemExit(0)


_handle_early_cli()

# Turn off huggingface_hub telemetry before anything can import it; set_for_app
# keeps the override off external programs and respects a user-set value.
from services.process_env import set_for_app  # noqa: E402

set_for_app("HF_HUB_DISABLE_TELEMETRY", "1", keep_existing=True)

from services import native_libraries  # noqa: E402


def _patch_subprocess_for_windows() -> None:
    # Not platform.system(): on Windows it queries WMI, which can take the
    # process down when the WMI service is unhealthy.
    if sys.platform != "win32":
        return

    original_popen = subprocess.Popen

    class _NoConsolePopen(original_popen):
        def __init__(self, *args, **kwargs):
            if "creationflags" not in kwargs:
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            elif not (kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW):
                kwargs["creationflags"] |= subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoConsolePopen


def _activate_downloadable_components() -> None:
    # Never let a broken component stop the app from starting. Settling an
    # interrupted install comes first, so activation sees the restored tree;
    # only the app does it, since a mid-download staging dir is another
    # process's work in progress.
    try:
        from services.components import prune_orphans
        prune_orphans()
    except Exception:
        # Logging is not configured yet; the last-resort handler prints this
        # to stderr so a broken component tree is not silently ignored.
        logging.getLogger(__name__).warning(
            "Could not settle interrupted component installs", exc_info=True
        )
    try:
        from services.component_runtime import activate_components
        activate_components()
    except Exception:
        logging.getLogger(__name__).warning(
            "Could not activate downloadable components", exc_info=True
        )


def _handle_invite_cli_argument(argv=None, *, data_dir=None):
    """Validate and forward or stage an ``openwhisper://pair`` or ``.openwhisper-invite`` arg.

    Returns an exit code (0 when forwarded to a running instance, 2 on invalid
    invite input), or ``None`` when startup should continue normally.
    """
    args = sys.argv[1:] if argv is None else list(argv)
    # Fast check without importing services on ordinary launches.
    has_candidate = any(
        isinstance(a, str)
        and not a.strip().startswith("-")
        and (
            a.strip().lower().startswith("openwhisper://")
            or a.strip().lower().endswith(".openwhisper-invite")
        )
        for a in args
    )
    if not has_candidate:
        return None

    from config import data_root as _data_root
    from services.remote_asr.invite import (
        InviteValidationError,
        extract_invite_arg,
        forward_invite_to_running_instance,
        resolve_invite_arg,
        set_pending_startup_invite,
    )

    raw_arg = extract_invite_arg(args)
    if not raw_arg:
        return None
    try:
        invite = resolve_invite_arg(raw_arg)
    except InviteValidationError as exc:
        print(f"OpenWhisper: invalid invite link: {exc}", file=sys.stderr)
        return 2

    target_dir = data_dir if data_dir is not None else _data_root()
    if forward_invite_to_running_instance(target_dir, invite):
        return 0

    set_pending_startup_invite(invite)
    return None


def _early_update_gate() -> None:
    """Wait out a running setup, and refuse a launch the updater owns."""
    if sys.argv[1:2] in (["--isolated-worker"], ["--self-test"], ["--workflow-smoke"]):
        return
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    from services.app_update_apply import (
        acquire_application_mutex_or_exit,
        maybe_exit_if_update_in_progress,
        recover_before_start,
    )

    maybe_exit_if_update_in_progress()
    if recover_before_start():
        raise SystemExit(0)
    acquire_application_mutex_or_exit()


_invite_exit = _handle_invite_cli_argument()
if _invite_exit is not None:
    raise SystemExit(_invite_exit)

_early_update_gate()
native_libraries.register_cuda_dll_directories()
native_libraries.register_qt_icu_directories()
native_libraries.preload_cuda_libraries()
_activate_downloadable_components()
_patch_subprocess_for_windows()

if sys.platform.startswith("linux"):
    from services.linux_deps import check_linux_dependencies

    if check_linux_dependencies() != 0:
        sys.exit(1)

# PyQt imports in the self-test must happen only after Windows ICU directories
# and Linux shared-library preflight have been configured.
_handle_package_self_test()

# A restore must finish before the UI can import settings or open SQLite. Keep
# the shared data lease for the process lifetime after any pending restore.
from config import data_root
from services.backup_startup import (
    RESTART_EXIT_CODE,
    acquire_startup_data_lease,
    release_startup_data_lease,
    spawn_restart,
)


class _NullTextStream:
    """A write-nowhere text stream standing in for a missing console."""

    encoding = "utf-8"
    name = "<null>"
    mode = "w"

    def write(self, text):
        return len(text)

    def flush(self):
        pass

    def close(self):
        pass

    def isatty(self):
        return False

    def writable(self):
        return True


def _ensure_writable_stdio() -> None:
    """Give the GUI writable stdout/stderr when the windowed build has none.

    A windowed (PyInstaller) build launches without a console, so Python
    exposes sys.stdout/sys.stderr as None and the first library print or
    progress bar dies with "'NoneType' object has no attribute 'write'".
    Every worker and CLI mode above has already dispatched — the channel
    bridge and the speech workers treat a missing stream as their signal —
    so only the desktop app is left to hand null streams to.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, _NullTextStream())


_ensure_writable_stdio()


def _show_startup_error(message: str) -> None:
    print(f"OpenWhisper: {message}", file=sys.stderr)
    if not getattr(sys, "frozen", False):
        return
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication([])
        QMessageBox.critical(None, "OpenWhisper", message)
        del app
    except Exception:
        # The stderr message remains available when Qt itself cannot start.
        pass


try:
    _APP_DATA_LEASE = acquire_startup_data_lease(data_root())
except Exception as exc:
    _show_startup_error(
        f"Could not safely open application data: {exc} "
        "The backup restore remains pending. Close other OpenWhisper processes "
        "and start the app again."
    )
    raise SystemExit(1) from exc

from ui_qt.bootstrap import main as _bootstrap_main


def dispatch_incoming_invite(invite) -> bool:
    """Route a validated ``InviteLink`` to Guided Setup or Remote Settings on the Qt thread.

    Never silently pairs over a different host: always shows host name, address,
    and TLS fingerprint confirmation in Guided Setup or Settings -> Remote engine.
    """
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return False

    from ui_qt.dialogs.guided_setup import GuidedSetupDialog
    from ui_qt.dialogs.settings_dialog import SettingsDialog
    from ui_qt.dialogs.settings_destinations import REMOTE_ENGINE

    # Prefer an already-open Guided Setup dialog.
    for widget in app.topLevelWidgets():
        if isinstance(widget, GuidedSetupDialog) and widget.isVisible():
            widget.begin_with_invite(invite)
            widget.raise_()
            widget.activateWindow()
            return True

    # Next, an already-open Settings dialog.
    for widget in app.topLevelWidgets():
        if isinstance(widget, SettingsDialog) and widget.isVisible():
            widget.select_destination(REMOTE_ENGINE)
            widget.handle_invite_link(invite)
            widget.raise_()
            widget.activateWindow()
            return True

    # Otherwise bring the main window forward and open Remote engine settings (or Guided Setup).
    from services.remote_asr.invite import set_pending_startup_invite

    set_pending_startup_invite(invite)
    for widget in app.topLevelWidgets():
        if widget.objectName() == "mainWindow" or hasattr(widget, "restore_from_tray"):
            if hasattr(widget, "restore_from_tray"):
                widget.restore_from_tray()
            if hasattr(widget, "settings_destination_requested"):
                widget.settings_destination_requested.emit(REMOTE_ENGINE)
                return True
            if hasattr(widget, "guided_setup_requested"):
                widget.guided_setup_requested.emit()
                return True
    return False


_QT_INVOKE_EVENT_TYPE = None


def _run_on_qt_thread(callback) -> bool:
    """Invoke ``callback`` on the Qt GUI thread if a QApplication is active."""
    global _QT_INVOKE_EVENT_TYPE
    try:
        from PyQt6.QtCore import QCoreApplication, QEvent, QObject
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        return False

    app = QApplication.instance()
    if app is None:
        return False

    if _QT_INVOKE_EVENT_TYPE is None:
        _QT_INVOKE_EVENT_TYPE = QEvent.Type(QEvent.registerEventType())
    event_type = _QT_INVOKE_EVENT_TYPE

    class _InvokeEvent(QEvent):
        def __init__(self, fn):
            super().__init__(event_type)
            self.fn = fn

    class _Receiver(QObject):
        def customEvent(self, event):  # noqa: N802
            try:
                fn = getattr(event, "fn", None)
                if callable(fn):
                    fn()
            finally:
                self._keepalive = None  # type: ignore[attr-defined]
                self.deleteLater()

    receiver = _Receiver()
    receiver.moveToThread(app.thread())
    receiver._keepalive = receiver  # type: ignore[attr-defined]
    QCoreApplication.postEvent(receiver, _InvokeEvent(callback))
    return True


def main() -> int:
    """Start the Invite IPC bridge around the Qt bootstrap lifecycle."""
    from services.remote_asr.invite import (
        InviteIpcServer,
        consume_pending_startup_invite,
        peek_pending_startup_invite,
    )

    ipc_server = None
    try:
        def _on_ipc_invite(invite) -> None:
            if not _run_on_qt_thread(lambda inv=invite: dispatch_incoming_invite(inv)):
                logging.getLogger(__name__).debug("Queued invite until Qt UI is ready")

        ipc_server = InviteIpcServer(data_root(), _on_ipc_invite)
        ipc_server.start()
    except Exception:
        logging.getLogger(__name__).debug("Could not start invite IPC listener", exc_info=True)
        ipc_server = None

    # Attach macOS FileOpen URL filter (and deliver any pending startup invite)
    # once QApplication and MainWindow are ready.
    def _attach_qt_invite_hooks() -> None:
        import time as _time

        from PyQt6.QtWidgets import QApplication

        from services.remote_asr.invite import install_qt_file_open_filter

        file_open_installed = False
        deadline = _time.monotonic() + 25.0
        while _time.monotonic() < deadline:
            app = QApplication.instance()
            if app is not None and not file_open_installed:
                file_open_installed = _run_on_qt_thread(
                    lambda: install_qt_file_open_filter(dispatch_incoming_invite)
                )
            if app is not None and any(
                hasattr(w, "restore_from_tray") and w.isVisible()
                for w in app.topLevelWidgets()
            ):
                pending = consume_pending_startup_invite()
                if pending is not None:
                    _run_on_qt_thread(lambda inv=pending: dispatch_incoming_invite(inv))
                return
            if file_open_installed and peek_pending_startup_invite() is None:
                return
            _time.sleep(0.15)

    import threading as _threading

    _threading.Thread(
        target=_attach_qt_invite_hooks,
        name="openwhisper-qt-invite-hooks",
        daemon=True,
    ).start()

    try:
        return _bootstrap_main()
    finally:
        if ipc_server is not None:
            ipc_server.stop()


__all__ = ["main"]


if __name__ == "__main__":
    from services.code_identity import remember_launch

    # What a later change on disk is measured against: see code_identity.
    remember_launch()
    try:
        exit_code = main()
    finally:
        # main() has drained persistence and closed SQLite before returning.
        # A restart must not inherit our lease or race its own restore.
        release_startup_data_lease()
    # ``main`` has already run every cleanup path and logged the shutdown.
    # Finalizing on top of Qt's teardown and the keyboard library's listener
    # thread — which cannot be stopped — instead produced access violations in
    # openwhisper.crash.log on the way out. Nothing is left to lose here.
    logging.shutdown()
    if exit_code == RESTART_EXIT_CODE:
        try:
            if sys.platform == "win32" and getattr(sys, "frozen", False):
                # The fresh process acquires these mutexes without waiting.
                # Release them after cleanup or the child can exit before this
                # process reaches os._exit().
                from services.app_update_apply import release_application_mutex_for_setup

                release_application_mutex_for_setup()
            spawn_restart(__file__)
        except Exception as exc:
            _show_startup_error(
                f"Could not restart OpenWhisper: {exc} "
                "Please start OpenWhisper again manually."
            )
            exit_code = 1
        else:
            exit_code = 0
    os._exit(exit_code)
