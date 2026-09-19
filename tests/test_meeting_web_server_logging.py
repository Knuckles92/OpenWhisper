"""The dashboard must start in a windowed frozen build, which has no stdout.

PyInstaller's ``console=False`` build leaves ``sys.stdout`` as None. uvicorn's
default ``LOGGING_CONFIG`` asks a formatter to probe ``sys.stdout.isatty()``,
and ``logging.config`` reports the resulting AttributeError as the opaque
"Unable to configure formatter 'default'" -- which reached users as a "Meeting
dashboard could not start" dialog.
"""
import sys
from unittest.mock import MagicMock, patch

import uvicorn

from meeting.web.server import MeetingWebServer


def _uvicorn_config_kwargs():
    """Capture the uvicorn.Config keyword arguments ``start()`` builds."""
    server = MeetingWebServer.__new__(MeetingWebServer)
    server._thread = None
    server._app = MagicMock()
    server._bind = "localhost"
    server._port = 0
    captured = {}

    def _record(_app, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    with patch("meeting.web.server.uvicorn.Config", side_effect=_record), patch(
        "meeting.web.server._ThreadedUvicornServer", return_value=MagicMock()
    ), patch("meeting.web.server.threading.Thread", return_value=MagicMock()):
        server.start()
    return captured


def test_dashboard_config_does_not_need_a_stdout():
    kwargs = _uvicorn_config_kwargs()
    saved = (sys.stdout, sys.stderr)
    sys.stdout = sys.stderr = None
    try:
        # uvicorn.Config.__init__ calls configure_logging(), so construction
        # alone is what used to raise.
        uvicorn.Config(MagicMock(), **kwargs)
    finally:
        sys.stdout, sys.stderr = saved


def test_dashboard_leaves_host_logging_alone():
    """log_config=None keeps uvicorn from running dictConfig over our handlers."""
    assert _uvicorn_config_kwargs()["log_config"] is None
