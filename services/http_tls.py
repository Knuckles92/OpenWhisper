"""Verified HTTPS for both source Python and frozen application bundles."""
import ssl
import threading
from typing import Optional

import certifi

_shared_context: Optional[ssl.SSLContext] = None
_shared_lock = threading.Lock()


def new_verified_context() -> ssl.SSLContext:
    """Build a private verified context that the caller may configure.

    Costs about 154 ms (129 ms of it parsing the certifi bundle, measured
    September 22, 2026), so only use this when the context will be mutated:
    httpcore, for one, sets ALPN on its context at every connect.
    """
    # Frozen Python's compiled CA path can point at the build machine. Keep
    # system/custom trust roots, and add the CA bundle shipped with the app.
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


def verified_context() -> ssl.SSLContext:
    """Return the process-wide verified context, built on first use.

    Building one per request put ~154 ms in front of every TypeSafe judgment.
    An ``SSLContext`` is safe to share between threads for client sockets as
    long as nobody reconfigures it, so callers must treat it as read-only;
    ``urllib`` and ``http.client`` only read a context they are handed. Trust
    roots are read once, so a certificate installed while the app runs is
    picked up on the next launch.
    """
    global _shared_context
    context = _shared_context
    if context is None:
        with _shared_lock:
            context = _shared_context
            if context is None:
                context = _shared_context = new_verified_context()
    return context
