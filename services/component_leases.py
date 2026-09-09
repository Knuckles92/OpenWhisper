"""Prevent component replacement while a local engine is using its payload."""
from __future__ import annotations

import threading
from contextlib import contextmanager

_lock = threading.RLock()
_users: dict[str, int] = {}
_mutating: set[str] = set()


def acquire_component(component_id: str):
    """Acquire a shared runtime lease; return an idempotent release callback."""
    with _lock:
        if component_id in _mutating:
            from services.components import ComponentError
            raise ComponentError("This component is being installed or removed. Try again when Downloads finishes.")
        _users[component_id] = _users.get(component_id, 0) + 1
    released = False

    def release():
        nonlocal released
        with _lock:
            if not released:
                released = True
                _users[component_id] -= 1
                if not _users[component_id]:
                    del _users[component_id]
    return release


@contextmanager
def component_mutation(component_id: str):
    with _lock:
        if _users.get(component_id) or component_id in _mutating:
            from services.components import ComponentError
            raise ComponentError("This component is in use. Finish the meeting or report job before changing it.")
        _mutating.add(component_id)
    try:
        yield
    finally:
        with _lock:
            _mutating.discard(component_id)
