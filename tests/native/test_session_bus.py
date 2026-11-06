"""Real desktop bus method dispatch on the native service event thread."""
import json
import os
import subprocess
import threading
import unittest


@unittest.skipUnless(os.environ.get("OPENWHISPER_TEST_SESSION_BUS") == "1", "requires isolated session bus")
class SessionBusTests(unittest.TestCase):
    def test_gdbus_press_release_and_status_use_service_thread(self):
        os.environ["OPENWHISPER_DESKTOP_RUNTIME"] = "native"
        from services.desktop_runtime import QCoreApplication, QTimer
        from services.native_desktop.session_bus import NativeSessionBus

        app = QCoreApplication.instance() or QCoreApplication([])
        owner = threading.get_ident()
        calls, failures = [], []

        class Controls:
            def Trigger(self, action, released):
                calls.append(("trigger", action, released, threading.get_ident()))
                return True

            def Button(self, action):
                calls.append(("button", action, threading.get_ident()))
                return True

            def Status(self):
                calls.append(("status", threading.get_ident()))
                return json.dumps({"runtime": "native"})

        bus = NativeSessionBus(Controls())
        self.assertTrue(bus.start())

        def client():
            try:
                prefix = ["gdbus", "call", "--session", "--dest", "org.openwhisper.OpenWhisper", "--object-path", "/org/openwhisper/OpenWhisper", "--method"]
                for method, arguments in (("Trigger", ["record", "false"]), ("Trigger", ["record", "true"]), ("Button", ["show"]), ("Status", [])):
                    response = subprocess.run([*prefix, "org.openwhisper.Control." + method, *arguments], capture_output=True, text=True, timeout=4)
                    if response.returncode:
                        raise AssertionError(response.stderr)
                    if method == "Status" and "native" not in response.stdout:
                        raise AssertionError(response.stdout)
            except Exception as exc:
                failures.append(exc)
            finally:
                app.quit()

        worker = threading.Thread(target=client, daemon=True)
        worker.start()
        QTimer.singleShot(10_000, app.quit)
        app.exec()
        worker.join(timeout=1)
        bus.close()
        if failures:
            raise failures[0]
        self.assertFalse(worker.is_alive())
        self.assertEqual([call[:-1] for call in calls], [("trigger", "record", False), ("trigger", "record", True), ("button", "show"), ("status",)])
        self.assertTrue(all(call[-1] == owner for call in calls))


if __name__ == "__main__":
    unittest.main()
