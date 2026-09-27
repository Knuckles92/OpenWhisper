"""The address other computers here reach this one at, not a VPN's.

Fixtures are the interfaces of two real machines (2026-09-26): an Arch
laptop with Wi-Fi, Tailscale, Docker and Proton VPN, where Proton held the
default route and Settings showed 10.2.0.2 instead of 192.168.0.118; and a
Windows desktop with Ethernet, Tailscale and a WSL adapter.
"""
import pytest

from services import lan_address as module
from services.lan_address import LAN, TAILSCALE, VIRTUAL, VPN, _Interface

ARCH_LAPTOP = [
    _Interface("wlo1", "192.168.0.118", 24),
    _Interface("tailscale0", "100.82.22.3", 32, point_to_point=True),
    _Interface("br-7ba443d1c480", "172.18.0.1", 16),
    _Interface("docker0", "172.17.0.1", 16),
    _Interface("proton0", "10.2.0.2", 32, point_to_point=True),
]
WINDOWS_DESKTOP = [
    _Interface("Tailscale", "100.65.148.53", 32, description="Tailscale Tunnel", if_type=53),
    _Interface("Ethernet", "192.168.0.206", 24,
               description="Realtek(R) PCI(e) Ethernet Controller", if_type=6),
    _Interface("vEthernet (WSL (Hyper-V firewall))", "172.21.32.1", 20,
               description="Hyper-V Virtual Ethernet Adapter", if_type=6),
    _Interface("ProtonVPN", "10.2.0.2", 32, description="ProtonVPN Tunnel", if_type=53),
]


@pytest.fixture
def machine(monkeypatch):
    def install(interfaces, route, hostname=()):
        monkeypatch.setattr(module, "_interfaces", lambda: list(interfaces))
        monkeypatch.setattr(module, "_route_address", lambda: route)
        monkeypatch.setattr(module, "_hostname_addresses", lambda: list(hostname))
        # sysfs belongs to the machine running the tests, not the fixture's.
        monkeypatch.setattr(module, "_linux_kind", lambda interface: None)
        monkeypatch.setattr(module, "_linux_physical", lambda name: name == "wlo1")
    return install


def test_the_lan_wins_over_the_vpn_holding_the_default_route(machine):
    machine(ARCH_LAPTOP, route="10.2.0.2")

    best = module.best_lan_address()

    assert (best.address, best.interface, best.kind) == ("192.168.0.118", "wlo1", LAN)


def test_every_arch_laptop_interface_is_classified(machine):
    machine(ARCH_LAPTOP, route="10.2.0.2")

    kinds = {entry.interface: entry.kind for entry in module.local_ipv4_addresses()}

    assert kinds == {"wlo1": LAN, "proton0": VPN, "tailscale0": TAILSCALE,
                     "br-7ba443d1c480": VIRTUAL, "docker0": VIRTUAL}


def test_windows_adapters_are_classified(machine):
    machine(WINDOWS_DESKTOP, route="10.2.0.2")

    kinds = {entry.address: entry.kind for entry in module.local_ipv4_addresses()}

    assert kinds == {"192.168.0.206": LAN, "100.65.148.53": TAILSCALE,
                     "172.21.32.1": VIRTUAL, "10.2.0.2": VPN}
    assert module.discover_lan_ipv4() == "192.168.0.206"


def test_only_a_vpn_is_offered_as_a_vpn(machine):
    machine([ARCH_LAPTOP[4], ARCH_LAPTOP[3]], route="10.2.0.2")

    best = module.best_lan_address()

    assert (best.address, best.kind) == ("10.2.0.2", VPN)


def test_the_default_route_breaks_a_tie_between_lans(machine):
    machine([_Interface("eth0", "192.168.1.5", 24), _Interface("eth1", "10.0.0.8", 24)],
            route="10.0.0.8")

    assert module.discover_lan_ipv4() == "10.0.0.8"


def test_linux_sysfs_marks_tunnels(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module, "_linux_kind", lambda interface: VPN if interface.name == "wg0" else None)
    monkeypatch.setattr(module, "_linux_physical", lambda name: False)

    # A tunnel with an ordinary-looking name and prefix still counts.
    assert module.classify(_Interface("wg0", "10.8.0.2", 24)).kind == VPN
    assert module.classify(_Interface("br0", "192.168.1.9", 24)).kind == LAN


def test_without_an_interface_list_the_route_probe_still_answers(machine):
    machine([], route="192.168.1.44", hostname=["127.0.1.1"])

    assert module.discover_lan_ipv4() == "192.168.1.44"


def test_loopback_host_names_are_skipped(machine):
    machine([], route="127.0.0.1", hostname=["127.0.1.1", "10.0.0.8"])

    assert module.discover_lan_ipv4() == "10.0.0.8"


def test_nothing_usable_is_none(machine):
    machine([ARCH_LAPTOP[3]], route=None)

    assert module.best_lan_address() is None


def test_this_computers_listing_does_not_fail():
    """Runs the real platform listing (getifaddrs or GetAdaptersAddresses)."""
    entries = module.local_ipv4_addresses()
    assert all(entry.kind in (LAN, VPN, VIRTUAL, TAILSCALE) for entry in entries)
