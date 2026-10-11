"""Brings agent scans, "Try it" results and signing secrets back to the Actions page.

One relay lives for the session, created on first use on the application
thread, so a worker or the action runtime never holds or emits on a page
widget: holding one let a thread drop the last reference and delete it off
the Qt thread, and emitting on one raced Settings closing. Each request
carries a token from :func:`next_token`; a widget answers only its own.
Qt drops a deleted widget's connection.
"""

from __future__ import annotations

import itertools
import logging
import threading
from typing import Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)


class _ActionsRelay(QObject):
    #: A scan for installed agents finished; read ``agents.available()`` now.
    scan_done = pyqtSignal(int)
    #: ``(token, ActionResult)`` for one "Try it".
    try_done = pyqtSignal(int, object)
    #: ``(token, secret, error)``: a webhook action's signing secret, or why not.
    secret_done = pyqtSignal(int, str, str)


_relay: Optional[_ActionsRelay] = None
_tokens = itertools.count(1)


def relay() -> _ActionsRelay:
    global _relay
    if _relay is None:
        _relay = _ActionsRelay()
    return _relay


def next_token() -> int:
    return next(_tokens)


def start_agent_scan(token: int, *, refresh: bool = False) -> None:
    """Refresh the installed-agent scan that ``agents.available()`` reads, off the Qt thread.

    It is the same scan Meeting Mode's agent picker runs, through the same
    seam, and ``scan_done(token)`` follows when it is finished.
    """
    from ui_qt.widgets import agent_picker

    # Bound here: the worker must not reach back into a widget.
    scan = agent_picker.scan_installed_agents
    done = relay().scan_done

    def worker() -> None:
        try:
            scan(refresh=refresh)
        except Exception:
            logger.warning("Scanning for installed agents failed", exc_info=True)
        done.emit(token)

    threading.Thread(target=worker, name="settings-actions-agent-scan", daemon=True).start()


def fetch_secret(token: int, action_id: str, *, renew: bool = False) -> None:
    """A webhook action's signing secret, made if missing (``renew``: a new one), off the Qt thread.

    The credential store can block (a keyring unlock prompt), so the runtime's
    calls run on a worker; ``secret_done(token, secret, error)`` follows.
    """
    from services.actions import runtime

    ensure, delete = runtime.ensure_action_secret, runtime.delete_action_secret
    done = relay().secret_done

    def worker() -> None:
        try:
            if renew:
                delete(action_id)
            secret, error = ensure(action_id), ""
        except Exception as exc:
            logger.warning("Reading an action's signing secret failed: %s", type(exc).__name__)
            secret, error = "", str(exc) or type(exc).__name__
        done.emit(token, secret, error)

    threading.Thread(target=worker, name="settings-actions-secret", daemon=True).start()


def forget_secret(action_id: str) -> None:
    """Remove a webhook action's signing secret off the Qt thread; nothing waits for it."""
    from services.actions import runtime

    delete = runtime.delete_action_secret

    def worker() -> None:
        try:
            delete(action_id)
        except Exception as exc:
            # An orphaned secret signs nothing; the next delete or restore drops it.
            logger.warning("Removing an action's signing secret failed: %s", type(exc).__name__)

    threading.Thread(target=worker, name="settings-actions-secret-delete", daemon=True).start()


def try_callback(token: int) -> Callable[[object], None]:
    """The ``done`` to hand ``dialog.on_try_action``; safe from any thread."""
    done = relay().try_done

    def finish(result) -> None:
        done.emit(token, result)

    return finish
