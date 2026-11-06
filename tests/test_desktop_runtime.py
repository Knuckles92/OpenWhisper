"""Exercise native dispatch in fresh interpreters without loading a GUI toolkit."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[1]


def run_runtime(source, *, native=True):
    env = os.environ.copy()
    env.pop("OPENWHISPER_DESKTOP_RUNTIME", None)
    if native:
        env["OPENWHISPER_DESKTOP_RUNTIME"] = "native"
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)], cwd=ROOT,
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def test_default_desktop_retains_real_qt_types():
    run_runtime("""
        from PyQt6 import QtCore
        from services import desktop_runtime as runtime
        assert not runtime.IS_NATIVE
        for name in ('QObject', 'QTimer', 'QThread', 'QCoreApplication', 'pyqtSignal', 'pyqtSlot', 'Qt'):
            assert getattr(runtime, name) is getattr(QtCore, name)
    """, native=False)


def test_native_controller_imports_and_event_loop_need_no_qt():
    run_runtime("""
        import sys
        class NoQt:
            def find_spec(self, fullname, *_args):
                if fullname.split('.')[0] in ('PyQt6', 'ui_qt'):
                    raise AssertionError('GUI import: ' + fullname)
        sys.meta_path.insert(0, NoQt())
        from services.application_controller import ApplicationController
        from services.backup_manager import BackupCoordinator
        from services.desktop_runtime import IS_NATIVE, QCoreApplication, QTimer
        assert IS_NATIVE
        assert ApplicationController and BackupCoordinator
        app = QCoreApplication([])
        QTimer.singleShot(1, lambda: app.exit(17))
        assert app.exec() == 17
        assert not any(name.startswith('PyQt6') for name in sys.modules)
    """)


def test_native_service_smoke_covers_stream_delivery_recovery_and_backup():
    run_runtime("""
        import sys
        class NoQt:
            def find_spec(self, fullname, *_args):
                if fullname.split('.')[0] == 'PyQt6':
                    raise AssertionError('Qt import: ' + fullname)
        sys.meta_path.insert(0, NoQt())
        from services.release_checks import run_workflow_smoke
        report = run_workflow_smoke(repeats=2)
        assert report['passed'] is True
        assert all(report['checks'].values())
    """)


def test_qt_service_smoke_keeps_a_widget_capable_application():
    run_runtime("""
        from PyQt6.QtCore import QCoreApplication
        from PyQt6.QtWidgets import QApplication
        from services import release_checks
        observed = []
        transcribe = release_checks._FixtureDecoder.transcribe
        def check_application(self, *args, **kwargs):
            observed.append(isinstance(QCoreApplication.instance(), QApplication))
            return transcribe(self, *args, **kwargs)
        release_checks._FixtureDecoder.transcribe = check_application
        report = release_checks.run_workflow_smoke(repeats=1)
        assert report['passed'] is True
        assert observed and all(observed), observed
    """, native=False)


def test_worker_signals_queue_to_receiver_and_plain_callback_affinity():
    run_runtime("""
        import threading
        from services.desktop_runtime import QCoreApplication, QObject, Qt, pyqtSignal, pyqtSlot
        app = QCoreApplication([])
        owner_thread = threading.get_ident()
        delivered, inline = [], []
        class Receiver(QObject):
            result = pyqtSignal(str, bool)
            @pyqtSlot(str)
            def receive(self, value):
                delivered.append((value, threading.get_ident(), self.sender() is self))
        receiver = Receiver()
        receiver.result.connect(receiver.receive)
        receiver.result.connect(lambda value: delivered.append((value, threading.get_ident(), True)))
        receiver.result.connect(lambda *_args: inline.append(threading.get_ident()), Qt.ConnectionType.DirectConnection)
        thread = threading.Thread(target=lambda: receiver.result.emit('worker-result', True))
        thread.start()
        thread.join()
        assert not delivered
        assert inline and inline[0] != owner_thread
        app.processEvents()
        assert delivered == [('worker-result', owner_thread, True)] * 2
        receiver.result.disconnect(receiver.receive)
        receiver.result.emit('disconnected', True)
        assert len(delivered) == 3
        receiver.blockSignals(True)
        receiver.result.emit('blocked', True)
        assert len(delivered) == 3
    """)


def test_automation_calls_run_on_native_application_thread():
    run_runtime("""
        import threading
        from services.desktop_runtime import QCoreApplication, QTimer
        from services.automation.app_control import call_on_qt
        app = QCoreApplication([])
        owner_thread = threading.get_ident()
        outcomes = []
        def worker():
            outcomes.append(call_on_qt(threading.get_ident))
            try:
                call_on_qt(lambda: 1 / 0)
            except ZeroDivisionError:
                outcomes.append('exception-preserved')
            finally:
                app.quit()
        thread = threading.Thread(target=worker)
        thread.start()
        QTimer.singleShot(1000, lambda: app.exit(99))
        assert app.exec() == 0
        thread.join(1)
        assert not thread.is_alive()
        assert outcomes == [owner_thread, 'exception-preserved']
    """)


def test_native_timers_repeat_restart_cancel_and_delete():
    run_runtime("""
        from services.desktop_runtime import QCoreApplication, QObject, QTimer
        app = QCoreApplication([])
        values = []
        timer = QTimer()
        timer.setInterval(1)
        def tick():
            values.append('repeat')
            if values.count('repeat') == 1:
                timer.start(2)
            else:
                timer.stop()
                QTimer.singleShot(3, app.quit)
        timer.timeout.connect(tick)
        timer.start()
        single = QTimer()
        single.setSingleShot(True)
        single.timeout.connect(lambda: values.append('single'))
        single.start(1)
        cancelled = QTimer()
        cancelled.timeout.connect(lambda: values.append('cancelled'))
        cancelled.start(1)
        cancelled.stop()
        parent = QObject()
        deleted = QTimer(parent)
        deleted.timeout.connect(lambda: values.append('deleted'))
        deleted.start(1)
        parent.deleteLater()
        assert deleted in parent.children()
        QTimer.singleShot(1000, lambda: app.exit(99))
        assert app.exec() == 0
        assert values.count('repeat') == 2
        assert values.count('single') == 1
        assert 'cancelled' not in values and 'deleted' not in values
        assert not timer.isActive() and timer.remainingTime() == -1
        assert not single.isActive() and single.isSingleShot()
    """)


def test_moved_receiver_and_timer_execute_on_worker_event_loop():
    run_runtime("""
        import threading
        from services.desktop_runtime import QCoreApplication, QObject, QThread, QTimer, pyqtSignal, pyqtSlot
        app = QCoreApplication([])
        owner_thread = threading.get_ident()
        results = []
        class Receiver(QObject):
            request = pyqtSignal()
            response = pyqtSignal(object)
            def __init__(self):
                super().__init__()
                self.request.connect(self.work)
            @pyqtSlot()
            def work(self):
                self.timer = QTimer(self)
                self.timer.setSingleShot(True)
                self.timer.timeout.connect(self.complete)
                self.timer.start(1)
            @pyqtSlot()
            def complete(self):
                self.response.emit((threading.get_ident(), QThread.currentThread() is worker))
                worker.quit()
        worker = QThread()
        receiver = Receiver()
        receiver.response.connect(lambda result: results.append((result, threading.get_ident())))
        receiver.moveToThread(worker)
        worker.finished.connect(app.quit)
        worker.start()
        receiver.request.emit()
        QTimer.singleShot(1000, lambda: app.exit(99))
        assert app.exec() == 0
        assert worker.wait(1000)
        assert not worker.isRunning()
        assert len(results) == 1
        assert results[0][0][0] != owner_thread
        assert results[0][0][1] is True
        assert results[0][1] == owner_thread
    """)


def test_zero_interval_timer_yields_to_queued_shutdown():
    run_runtime("""
        from services.desktop_runtime import QCoreApplication, QObject, QTimer, Qt, pyqtSignal
        app = QCoreApplication([])
        class Requests(QObject):
            stop = pyqtSignal()
        requests = Requests()
        requests.stop.connect(app.quit, Qt.ConnectionType.QueuedConnection)
        timer = QTimer()
        ticks = []
        def tick():
            ticks.append(1)
            if len(ticks) == 1:
                requests.stop.emit()
        timer.timeout.connect(tick)
        timer.start(0)
        QTimer.singleShot(1000, lambda: app.exit(99))
        assert app.exec() == 0
        assert len(ticks) == 1
    """)


def test_native_macos_accessibility_intro_uses_service_hook_without_gui():
    run_runtime("""
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch
        from services.desktop_runtime import QCoreApplication
        from services.runtime import hotkeys
        assert callable(hotkeys.is_accessibility_trusted)
        app = QCoreApplication([])
        shown = []
        runtime = hotkeys.HotkeyRuntime.__new__(hotkeys.HotkeyRuntime)
        runtime.controller = SimpleNamespace(ui_controller=SimpleNamespace(
            show_accessibility_setup=lambda: shown.append(True)))
        runtime._auto_paste_enabled = lambda: True
        runtime._accessibility_intro_seen = lambda: False
        with patch.object(hotkeys.sys, 'platform', 'darwin'), patch.object(hotkeys, 'is_accessibility_trusted', return_value=False):
            runtime._check_autopaste_permission()
            app.processEvents()
        assert shown == [True]
        assert not any(name.startswith(('PyQt6', 'ui_qt')) for name in sys.modules)
    """)
