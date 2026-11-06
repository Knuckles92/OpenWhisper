"""The serialized event runtime for the Rune Python service process.

Signal receivers have thread affinity, including ordinary callbacks captured at
connection time. Timers and queued calls run on that thread's event loop; audio
and inference workers cannot accidentally run controller or UI callbacks inline.
"""

from __future__ import annotations

from collections import deque
from enum import IntEnum
import heapq
import inspect
import itertools
import sys
import threading
import time
import weakref


class Qt:
    class ConnectionType(IntEnum):
        AutoConnection = 0
        DirectConnection = 1
        QueuedConnection = 2
        BlockingQueuedConnection = 3

    class TimerType(IntEnum):
        PreciseTimer = 0
        CoarseTimer = 1
        VeryCoarseTimer = 2


_local = threading.local()


class _Scheduled:
    def __init__(self, callback):
        self.callback = callback

    def cancel(self):
        self.callback = None


class _Dispatcher:
    def __init__(self):
        self._condition = threading.Condition()
        self._queue = deque()
        self._timers = []
        self._sequence = itertools.count()
        self._prefer_queue = True
        self.stopped = False
        self.exit_code = 0

    def post(self, callback):
        with self._condition:
            self._queue.append(callback)
            self._condition.notify()

    def schedule(self, delay_ms, callback):
        task = _Scheduled(callback)
        with self._condition:
            heapq.heappush(self._timers, (
                time.monotonic() + max(0, delay_ms) / 1000,
                next(self._sequence), task,
            ))
            self._condition.notify()
        return task

    def stop(self, code=0):
        with self._condition:
            self.exit_code = code
            self.stopped = True
            self._condition.notify_all()

    def _next(self, block):
        with self._condition:
            while not self.stopped:
                while self._timers and self._timers[0][2].callback is None:
                    heapq.heappop(self._timers)
                now = time.monotonic()
                due = self._timers and self._timers[0][0] <= now
                if due and (not self._queue or not self._prefer_queue):
                    self._prefer_queue = True
                    return heapq.heappop(self._timers)[2].callback
                if self._queue:
                    self._prefer_queue = False
                    return self._queue.popleft()
                if not block:
                    return None
                delay = max(0, self._timers[0][0] - now) if self._timers else None
                self._condition.wait(delay)
        return None

    def process(self):
        # A zero-interval repeating timer must yield back to its caller.
        with self._condition:
            budget = len(self._queue) + len(self._timers) + 1
        for _ in range(budget):
            callback = self._next(False)
            if callback is None:
                break
            _invoke(callback)

    def run(self):
        while not self.stopped:
            callback = self._next(True)
            if callback is not None:
                _invoke(callback)
        return self.exit_code


def _invoke(callback, args=(), sender=None):
    previous = getattr(_local, "sender", None)
    _local.sender = sender
    try:
        callback(*args)
    except Exception:
        # Match queued Qt slots: report errors without abandoning the event loop.
        sys.excepthook(*sys.exc_info())
    finally:
        _local.sender = previous


class _Connection:
    def __init__(self, callback, kind):
        receiver = getattr(callback, "__self__", None)
        if isinstance(callback, _BoundSignal):
            receiver = callback._owner()
        self.receiver = weakref.ref(receiver) if isinstance(receiver, QObject) else None
        self.callback = weakref.WeakMethod(callback) if self.receiver and inspect.ismethod(callback) else callback
        self.weak = isinstance(self.callback, weakref.WeakMethod)
        self.affinity = receiver.thread() if self.receiver else QThread.currentThread()
        self.kind = kind
        self.arg_count = None
        try:
            parameters = list(inspect.signature(callback).parameters.values())
            if not any(p.kind == p.VAR_POSITIONAL for p in parameters):
                self.arg_count = sum(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in parameters)
        except (TypeError, ValueError):
            pass

    def target(self):
        receiver = self.receiver() if self.receiver else None
        if self.receiver and (receiver is None or receiver._deleted):
            return None
        return self.callback() if self.weak else self.callback

    def deliver(self, args, sender):
        callback = self.target()
        if callback is not None:
            _invoke(callback, args if self.arg_count is None else args[:self.arg_count], sender)


class _BoundSignal:
    def __init__(self, owner):
        self._owner = weakref.ref(owner)
        self._connections = []
        self._lock = threading.RLock()

    def __getitem__(self, _signature):
        return self

    def connect(self, callback, type=Qt.ConnectionType.AutoConnection, **_options):
        if not callable(callback):
            raise TypeError("signal receiver must be callable")
        with self._lock:
            self._connections.append(_Connection(callback, type))

    def disconnect(self, callback=None):
        with self._lock:
            if callback is None:
                self._connections.clear()
                return
            retained = [c for c in self._connections if c.target() != callback]
            if len(retained) == len(self._connections):
                raise TypeError("receiver is not connected")
            self._connections = retained

    def emit(self, *args):
        owner = self._owner()
        if owner is None or owner._deleted or owner._signals_blocked:
            return
        with self._lock:
            connections = tuple(self._connections)
        for connection in connections:
            if connection.target() is None:
                continue
            receiver = connection.receiver() if connection.receiver else None
            affinity = receiver.thread() if receiver else connection.affinity
            kind = connection.kind
            same_thread = affinity is QThread.currentThread()
            if kind == Qt.ConnectionType.DirectConnection or (kind == Qt.ConnectionType.AutoConnection and same_thread):
                connection.deliver(args, owner)
            elif kind == Qt.ConnectionType.BlockingQueuedConnection:
                if same_thread:
                    raise RuntimeError("blocking queued connection would deadlock its receiver")
                done = threading.Event()

                def deliver(connection=connection, done=done):
                    try:
                        connection.deliver(args, owner)
                    finally:
                        done.set()

                affinity._dispatcher.post(deliver)
                done.wait()
            else:
                affinity._dispatcher.post(lambda c=connection: c.deliver(args, owner))

    __call__ = emit


class pyqtSignal:
    def __init__(self, *_types, **_options):
        self._key = object()

    def __get__(self, instance, _owner):
        if instance is None:
            return self
        return instance._signals.setdefault(self._key, _BoundSignal(instance))


def pyqtSlot(*_types, **_options):
    def decorate(function):
        return function
    return decorate


class QObject:
    destroyed = pyqtSignal(object)

    def __init__(self, parent=None):
        self._signals = {}
        self._signals_blocked = False
        self._deleted = False
        self._affinity = parent.thread() if parent else QThread.currentThread()
        self._parent = None
        self._children = []
        self._properties = {}
        self._object_name = ""
        self.setParent(parent)

    def thread(self):
        return self._affinity

    def moveToThread(self, thread):
        if self._parent is not None:
            raise RuntimeError("an object with a parent cannot move to another thread")
        self._move(thread)

    def _move(self, thread):
        self._affinity = thread
        for child in self._children:
            child._move(thread)

    def setParent(self, parent):
        if parent is not None and parent.thread() is not self.thread():
            raise RuntimeError("parent and child must belong to the same event thread")
        if self._parent is not None:
            self._parent._children.remove(self)
        self._parent = parent
        if parent is not None:
            parent._children.append(self)

    def parent(self):
        return self._parent

    def children(self):
        return self._children[:]

    def blockSignals(self, blocked):
        previous = self._signals_blocked
        self._signals_blocked = bool(blocked)
        return previous

    def signalsBlocked(self):
        return self._signals_blocked

    def sender(self):
        return getattr(_local, "sender", None)

    def setProperty(self, name, value):
        self._properties[name] = value
        return True

    def property(self, name):
        return self._properties.get(name)

    def setObjectName(self, name):
        self._object_name = name

    def objectName(self):
        return self._object_name

    def deleteLater(self):
        self.thread()._dispatcher.post(self._delete)

    def _delete(self):
        if self._deleted:
            return
        for child in self._children[:]:
            child._delete()
        self.destroyed.emit(self)
        self._deleted = True
        self._signals.clear()
        self.setParent(None)


class QThread(QObject):
    started = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dispatcher = _Dispatcher()
        self._worker = None
        self._running = False

    @staticmethod
    def currentThread():
        handle = getattr(_local, "thread", None)
        if handle is None:
            handle = object.__new__(QThread)
            _local.thread = handle
            QObject.__init__(handle)
            handle._dispatcher = _Dispatcher()
            handle._worker = threading.current_thread()
            handle._running = True
        return handle

    @staticmethod
    def currentThreadId():
        return threading.get_ident()

    def start(self):
        if self._running:
            return
        self._dispatcher.stopped = False
        self._running = True

        def run():
            _local.thread = self
            try:
                self.started.emit()
                self.run()
            finally:
                self._running = False
                self.finished.emit()

        self._worker = threading.Thread(target=run, daemon=True)
        self._worker.start()

    def run(self):
        self.exec()

    def exec(self):
        return self._dispatcher.run()

    def quit(self):
        self._dispatcher.stop()

    def exit(self, code=0):
        self._dispatcher.stop(code)

    def wait(self, msecs=-1):
        if self._worker is None:
            return True
        self._worker.join(None if msecs < 0 else msecs / 1000)
        return not self._worker.is_alive()

    def isRunning(self):
        return self._running

    @staticmethod
    def msleep(msecs):
        time.sleep(msecs / 1000)


class QCoreApplication(QObject):
    aboutToQuit = pyqtSignal()
    _instance = None

    def __init__(self, argv=None):
        if QCoreApplication._instance is not None:
            raise RuntimeError("an application event loop already exists")
        super().__init__()
        self.argv = list(argv or [])
        QCoreApplication._instance = self

    @staticmethod
    def instance():
        return QCoreApplication._instance

    @staticmethod
    def processEvents(*_args):
        QThread.currentThread()._dispatcher.process()

    def exec(self):
        if QThread.currentThread() is not self.thread():
            raise RuntimeError("the application event loop must run on its owner thread")
        try:
            return self.thread()._dispatcher.run()
        finally:
            self.aboutToQuit.emit()

    @staticmethod
    def quit():
        QCoreApplication.exit(0)

    @staticmethod
    def exit(code=0):
        app = QCoreApplication.instance()
        if app is not None:
            app.thread()._dispatcher.stop(code)


class QTimer(QObject):
    timeout = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._interval = 0
        self._single = False
        self._active = False
        self._task = None
        self._deadline = 0
        self._timer_type = Qt.TimerType.CoarseTimer

    def setInterval(self, msecs):
        self._interval = max(0, int(msecs))
        if self._active:
            self.start()

    def interval(self):
        return self._interval

    def setSingleShot(self, single):
        self._single = bool(single)

    def isSingleShot(self):
        return self._single

    def setTimerType(self, timer_type):
        self._timer_type = timer_type

    def timerType(self):
        return self._timer_type

    def start(self, msecs=None):
        if QThread.currentThread() is not self.thread():
            raise RuntimeError("timers must start on their owner event thread")
        self.stop()
        if msecs is not None:
            self._interval = max(0, int(msecs))
        self._active = True
        self._schedule()

    def _schedule(self):
        self._deadline = time.monotonic() + self._interval / 1000
        self._task = self.thread()._dispatcher.schedule(self._interval, self._fire)

    def _fire(self):
        if not self._active or self._deleted:
            return
        task = self._task
        if self._single:
            self._active = False
        self.timeout.emit()
        if self._active and self._task is task:
            self._schedule()

    def stop(self):
        self._active = False
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def isActive(self):
        return self._active

    def remainingTime(self):
        return max(0, int((self._deadline - time.monotonic()) * 1000)) if self._active else -1

    def _delete(self):
        self.stop()
        super()._delete()

    @staticmethod
    def singleShot(msecs, callback):
        connection = _Connection(callback, Qt.ConnectionType.AutoConnection)
        connection.affinity._dispatcher.schedule(msecs, lambda: connection.deliver((), None))
