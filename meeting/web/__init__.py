"""Meeting Mode web layer: FastAPI dashboard server, WebSocket hub, and auth.

The dashboard is served over plain HTTP on localhost by default; the host
explicitly opts in to LAN binding. Access control is capability-token based
(see ``meeting.web.auth``), with a separate host and guest token per meeting.
"""
from meeting.web.server import MeetingWebServer

__all__ = ["MeetingWebServer"]
