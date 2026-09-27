"""LAN dashboard links must advertise a reachable non-loopback address.

The choice itself (a LAN address over a VPN's, never a Docker bridge) is
covered in test_lan_address.py; the dashboard uses the same answer.
"""
from unittest.mock import patch

from meeting.web.server import MeetingWebServer, discover_lan_ipv4
from services import lan_address


def _machine(route, hostname=()):
    return (
        patch.object(lan_address, "_interfaces", lambda: []),
        patch.object(lan_address, "_route_address", lambda: route),
        patch.object(lan_address, "_hostname_addresses", lambda: list(hostname)),
    )


def test_route_selected_lan_address_wins():
    interfaces, route, hostname = _machine("192.168.1.44")
    with interfaces, route, hostname:
        assert discover_lan_ipv4() == "192.168.1.44"


def test_loopback_hostname_mapping_is_skipped_for_real_interface():
    interfaces, route, hostname = _machine("127.0.0.1", ["127.0.1.1", "10.0.0.8"])
    with interfaces, route, hostname:
        assert discover_lan_ipv4() == "10.0.0.8"


def test_lan_display_host_falls_back_only_when_no_interface_is_usable():
    server = MeetingWebServer.__new__(MeetingWebServer)
    server._bind = "lan"
    with patch("meeting.web.server.discover_lan_ipv4", return_value=None):
        assert server._display_host() == "127.0.0.1"
