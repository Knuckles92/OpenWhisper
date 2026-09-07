"""Verified HTTPS for both source Python and frozen application bundles."""
import ssl

import certifi


def verified_context() -> ssl.SSLContext:
    # Frozen Python's compiled CA path can point at the build machine. Keep
    # system/custom trust roots, and add the CA bundle shipped with the app.
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context
