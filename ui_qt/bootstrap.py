"""Qt application bootstrap and startup flow."""

from __future__ import annotations

import faulthandler
import logging
import signal
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import config
from ui_qt.startup_profiler import StartupProfiler

_CRASH_LOG_FILE = None
_QT_MESSAGE_HANDLER_INSTALLED = False


def setup_logging() -> None:
    """Setup application logging."""
    level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    file_handler = RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=level,
        format=config.LOG_FORMAT,
        handlers=[file_handler, logging.StreamHandler()],
        force=True,
    )
    _enable_crash_logging()
    _install_qt_message_handler()


def _enable_crash_logging() -> None:
    """Enable faulthandler crash logging for hard crashes."""
    global _CRASH_LOG_FILE

    try:
        crash_log_path = Path(config.LOG_FILE).with_suffix(".crash.log")
        _CRASH_LOG_FILE = open(crash_log_path, "a", buffering=1)
        faulthandler.enable(file=_CRASH_LOG_FILE, all_threads=True)

        for sig in (signal.SIGSEGV, signal.SIGABRT, signal.SIGFPE, signal.SIGILL):
            try:
                faulthandler.register(sig, file=_CRASH_LOG_FILE, all_threads=True)
            except (AttributeError, RuntimeError, ValueError):
                pass

        logging.info(f"Faulthandler enabled for crash diagnostics: {crash_log_path}")
    except Exception as exc:
        logging.warning(f"Failed to enable faulthandler: {exc}")


def _install_qt_message_handler() -> None:
    """Route Qt warnings/errors to the Python logger."""
    global _QT_MESSAGE_HANDLER_INSTALLED

    if _QT_MESSAGE_HANDLER_INSTALLED:
        return

    try:
        from PyQt6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception as exc:
        logging.warning(f"Failed to install Qt message handler: {exc}")
        return

    def _qt_message_handler(msg_type, context, message) -> None:
        logger = logging.getLogger("qt")
        context_info = ""
        try:
            if context and (context.file or context.function or context.line):
                context_info = f" ({context.file}:{context.line} {context.function})"
        except Exception:
            context_info = ""

        text = f"{message}{context_info}"

        if msg_type == QtMsgType.QtDebugMsg:
            logger.debug(text)
        elif msg_type == QtMsgType.QtInfoMsg:
            logger.info(text)
        elif msg_type == QtMsgType.QtWarningMsg:
            logger.warning(text)
        elif msg_type == QtMsgType.QtCriticalMsg:
            logger.error(text)
        elif msg_type == QtMsgType.QtFatalMsg:
            logger.critical(text)
        else:
            logger.info(text)

    qInstallMessageHandler(_qt_message_handler)
    _QT_MESSAGE_HANDLER_INSTALLED = True
    logging.info("Qt message handler installed")


def get_early_runtime_components():
    """Load only the runtime classes needed for the first visual."""
    from ui_qt.app import QtApplication
    from ui_qt.loading_screen import LoadingScreen

    return QtApplication, LoadingScreen


def get_late_runtime_components():
    """Load heavier runtime classes after the loading screen is visible."""
    from services.application_controller import ApplicationController
    from ui_qt.ui_controller import UIController

    return UIController, ApplicationController


def process_qt_events() -> None:
    """Flush pending Qt events during startup."""
    from PyQt6.QtCore import QCoreApplication

    QCoreApplication.processEvents()


def load_local_whisper_backend():
    """Load the local Whisper backend (safe to call off the UI thread)."""
    from transcriber import LocalWhisperBackend

    return LocalWhisperBackend()


def run_with_ui_pulse(fn):
    """Run ``fn`` on a worker thread while keeping the splash animation alive.

    Startup previously blocked the UI thread on model load, so QTimer-driven
    painting never ran. This spins a nested event loop on the main thread until
    the worker finishes, which lets the loading-screen glow timer fire.

    Args:
        fn: Zero-arg callable. Must not touch Qt widgets/objects.

    Returns:
        The return value of ``fn``.

    Raises:
        Exception: Re-raises whatever ``fn`` raised on the worker thread.
    """
    import threading

    from PyQt6.QtCore import QEventLoop, QTimer
    from PyQt6.QtWidgets import QApplication

    if QApplication.instance() is None:
        return fn()

    box = {"done": False, "result": None, "error": None}

    def worker() -> None:
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001 - re-raised on main thread
            box["error"] = exc
        finally:
            box["done"] = True

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    while not box["done"]:
        loop = QEventLoop()
        QTimer.singleShot(33, loop.quit)
        loop.exec()

    thread.join(timeout=1.0)

    if box["error"] is not None:
        raise box["error"]
    return box["result"]


def main() -> int:
    """Main application entry point."""
    profiler = StartupProfiler()
    profiler.mark("main_entered")
    summary_logged = False

    setup_logging()
    profiler.mark("logging_ready")
    logging.info("=" * 60)
    logging.info("Starting OpenWhisper")
    logging.info("=" * 60)

    # Model loads are cache-first (local_files_only) regardless of settings;
    # an external HF_HUB_OFFLINE=1 additionally hard-disables downloads.
    from services.settings import is_hf_hub_offline_env_set

    if is_hf_hub_offline_env_set():
        logging.info(
            "HF_HUB_OFFLINE set in environment — Hugging Face downloads disabled"
        )

    profiler.mark("early_imports_started")
    QtApplication, LoadingScreen = get_early_runtime_components()
    profiler.mark("early_imports_finished")

    qt_app = QtApplication()
    profiler.mark("qt_app_created")
    loading_screen = None
    ui_controller = None
    app_controller = None

    try:
        loading_screen = LoadingScreen()
        profiler.mark("loading_screen_constructed")
        loading_screen.show()
        profiler.mark("loading_screen_shown")

        loading_screen.update_status("Initializing components...")
        loading_screen.update_progress("Preparing startup...")
        loading_screen.repaint()
        process_qt_events()
        profiler.mark("first_visual_flushed")

        loading_screen.update_status("Loading application...")
        loading_screen.update_progress("Loading runtime components...")
        process_qt_events()

        profiler.mark("late_imports_started")
        UIController, ApplicationController = run_with_ui_pulse(
            get_late_runtime_components
        )
        profiler.mark("late_imports_finished")

        loading_screen.update_status("Creating interface...")
        loading_screen.update_progress("Setting up windows...")
        process_qt_events()

        ui_controller = UIController()
        profiler.mark("ui_controller_created")

        loading_screen.update_status("Initializing audio system...")
        loading_screen.update_progress("Loading transcription models...")
        process_qt_events()

        local_backend = run_with_ui_pulse(load_local_whisper_backend)
        app_controller = ApplicationController(
            ui_controller, local_backend=local_backend
        )
        profiler.mark("application_controller_created")

        local_backend = app_controller.transcription_backends.get("local_whisper")
        if local_backend and hasattr(local_backend, "device_info"):
            device_info = local_backend.device_info
            loading_screen.update_progress(f"Using {device_info}")
            process_qt_events()
            logging.info(f"Whisper device: {device_info}")

        loading_screen.destroy()
        loading_screen = None

        ui_controller.show_main_window()
        profiler.mark("main_window_shown")

        if local_backend and hasattr(local_backend, "device_info"):
            ui_controller.set_device_info(local_backend.device_info)

        # Now that the main UI is available, a missing local model may request
        # download consent (never during startup, never for API-only users).
        app_controller.notify_main_ui_ready()

        profiler.log_summary()
        summary_logged = True
        logging.info("Application initialization complete")
        logging.info("Starting event loop")
        return qt_app.run(ui_controller.main_window)
    except Exception:
        if not summary_logged:
            profiler.log_summary()
            summary_logged = True
        logging.exception("Application startup failed")
        raise
    finally:
        try:
            if loading_screen is not None:
                loading_screen.destroy()
        except Exception:
            logging.exception("Failed to cleanup loading screen")

        try:
            if app_controller is not None:
                app_controller.cleanup()
            elif ui_controller is not None:
                ui_controller.cleanup()
        except Exception:
            logging.exception("Failed to cleanup controllers")

        logging.info("=" * 60)
        logging.info("Application shutdown complete")
        logging.info("=" * 60)
