"""Single source of truth for the OpenWhisper version.

Consumed by ``config.py``, the PyInstaller ``version_info`` resource, and the
Inno Setup ``AppVersion``. Keep this module dependency-free so the build
scripts can read it without importing the application.

Routine releases use a zero-padded two-digit patch after each ``X.Y.0``
milestone (e.g. ``2.9.03``, ``2.9.04``, ..., ``2.9.99``); minor and major bumps
are reserved for deliberate milestones.

Note: ``services.database.SCHEMA_VERSION`` is the *database schema* version and
is unrelated to this number.
"""

__version__ = "2.9.2"

__all__ = ["__version__"]
