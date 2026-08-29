"""Single source of truth for the OpenWhisper version.

Consumed by ``config.py``, the PyInstaller ``version_info`` resource, and the
Inno Setup ``AppVersion``. Keep this module dependency-free so the build
scripts can read it without importing the application.

Note: ``services.database.SCHEMA_VERSION`` is the *database schema* version and
is unrelated to this number.
"""

__version__ = "2.4.7"

__all__ = ["__version__"]
