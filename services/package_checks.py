"""Small runtime checks used by the frozen application's --self-test."""

import multiprocessing
import sys


def _multiprocessing_probe(connection, lock):
    """A spawned worker must never run the application bootstrap."""
    try:
        with lock:
            connection.send([
                name for name in ("config", "ui_qt.bootstrap", "services.application_controller")
                if name in sys.modules
            ])
    finally:
        connection.close()


def check_multiprocessing() -> None:
    """Exercise spawn and the resource tracker without opening an app window."""
    context = multiprocessing.get_context("spawn")
    # A spawn-context lock starts the POSIX resource tracker, including the
    # same path used by tqdm during Whisper's first transcription.
    lock = context.RLock()
    reader, writer = context.Pipe(duplex=False)
    worker = context.Process(target=_multiprocessing_probe, args=(writer, lock))
    try:
        worker.start()
        writer.close()
        if not reader.poll(20):
            raise RuntimeError("Multiprocessing worker did not respond")
        imported = reader.recv()
        worker.join(timeout=5)
        if worker.exitcode != 0:
            raise RuntimeError(f"Multiprocessing worker did not exit cleanly: {worker.exitcode}")
        if imported:
            raise RuntimeError(f"Multiprocessing worker initialized the app: {imported}")
    finally:
        reader.close()
        writer.close()
        if worker.pid is not None:
            if worker.is_alive():
                worker.kill()
            worker.join(timeout=5)
            worker.close()
