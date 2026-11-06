"""The established desktop D-Bus control interface on an independent loop."""
import asyncio
import logging
import threading

log = logging.getLogger(__name__)


class NativeSessionBus:
    def __init__(self, controls):
        self.controls = controls
        self.loop = None
        self.bus = None
        self.thread = None
        self._ready = threading.Event()
        self._ok = False

    def start(self):
        self.thread = threading.Thread(target=self._run, name="desktop-session-bus", daemon=True)
        self.thread.start()
        self._ready.wait(5)
        return self._ok

    def _run(self):
        from dbus_next.aio import MessageBus
        from dbus_next.constants import RequestNameReply
        from dbus_next.service import ServiceInterface, method
        from services.automation.app_control import call_on_qt
        from services.omarchy_controls import INTERFACE, PATH, SERVICE

        controls = self.controls

        class ControlInterface(ServiceInterface):
            def __init__(self):
                super().__init__(INTERFACE)

            @method()
            def Trigger(self, action: "s", released: "b") -> "b":  # noqa: F821 — D-Bus signatures
                return bool(call_on_qt(lambda: controls.Trigger(action, released)))

            @method()
            def Button(self, action: "s") -> "b":  # noqa: F821 — D-Bus signatures
                return bool(call_on_qt(lambda: controls.Button(action)))

            @method()
            def Status(self) -> "s":  # noqa: F821 — D-Bus signature
                return call_on_qt(controls.Status)

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def connect():
            self.bus = await MessageBus().connect()
            reply = await self.bus.request_name(SERVICE)
            if reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
                self.bus.disconnect()
                raise RuntimeError("Another desktop instance owns the control service")
            self.bus.export(PATH, ControlInterface())
            self._ok = True

        try:
            self.loop.run_until_complete(connect())
            self._ready.set()
            self.loop.run_forever()
        except Exception:
            log.warning("Desktop session bus unavailable", exc_info=True)
        finally:
            self._ready.set()
            if self.bus:
                self.bus.disconnect()
            self.loop.close()

    def close(self):
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread and threading.current_thread() is not self.thread:
            self.thread.join(timeout=2)
