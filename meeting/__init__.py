"""Meeting Mode: long-running meeting capture, transcription, and live intelligence.

This package is deliberately self-contained: no Qt imports anywhere inside it.
The only inbound dependencies are ``config``, ``services.database``,
``services.settings``, and helpers from ``services.transcript_cleanup`` — all
injectable — so the package can later be extracted into a standalone
application.
"""
