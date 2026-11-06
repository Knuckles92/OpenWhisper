"""Service event primitives, selected before importing application services.

The Qt desktop keeps Qt's own objects. The Rune service process explicitly sets
``OPENWHISPER_DESKTOP_RUNTIME=native`` before its first service import, so its
event loop and workers never need a Qt installation or GUI connection.
"""

import os

IS_NATIVE = os.environ.get("OPENWHISPER_DESKTOP_RUNTIME", "").lower() == "native"

if IS_NATIVE:
    from services._native_runtime import (
        QCoreApplication, QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot,
    )
else:
    from PyQt6.QtCore import (
        QCoreApplication, QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot,
    )

__all__ = [
    "IS_NATIVE", "QCoreApplication", "QObject", "QThread", "QTimer", "Qt",
    "pyqtSignal", "pyqtSlot",
]
