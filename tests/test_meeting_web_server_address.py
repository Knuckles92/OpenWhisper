"""LAN dashboard links must advertise a reachable non-loopback address."""
from unittest.mock import MagicMock, patch

from meeting.web.server import MeetingWebServer, discover_lan_ipv4


def _probe(address):
    probe = MagicMock()
    probe.__enter__.return_value = probe
    probe.getsockname.return_value = (address, 45678)
    return probe


def test_route_selected_lan_address_wins():
    with patch("meeting.web.server.socket.socket", return_value=_probe("192.168.1.44")), patch(
        "meeting.web.server.socket.getaddrinfo", return_value=[]
    ):
        assert discover_lan_ipv4() == "192.168.1.44"


def test_loopback_hostname_mapping_is_skipped_for_real_interface():
    with patch("meeting.web.server.socket.socket", return_value=_probe("127.0.0.1")), patch(
        "meeting.web.server.socket.getaddrinfo",
        return_value=[(2, 2, 17, "", ("127.0.1.1", 0)), (2, 2, 17, "", ("10.0.0.8", 0))],
    ):
        assert discover_lan_ipv4() == "10.0.0.8"


def test_lan_display_host_falls_back_only_when_no_interface_is_usable():
    server = MeetingWebServer.__new__(MeetingWebServer)
    server._bind = "lan"
    with patch("meeting.web.server.discover_lan_ipv4", return_value=None):
        assert server._display_host() == "127.0.0.1"
