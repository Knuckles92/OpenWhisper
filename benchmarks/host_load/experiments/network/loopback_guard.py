"""External test-process guard: permit only direct loopback network I/O.

Not application code. Installed explicitly before benchmark application imports.
This is a Python-process guard, not an OS sandbox for native subprocesses.
Local forward-proxy ports are explicitly denied so loopback allowance cannot
send test traffic through this environment's outbound proxy.
"""
import ipaddress
import os
import sys
from urllib.parse import urlsplit

_PROXY_ENDPOINTS = set()
for _key, _value in os.environ.items():
    if _key.lower().endswith('_proxy') and _key.lower() != 'no_proxy':
        try:
            _url = urlsplit(_value)
            if _url.hostname and _url.port:
                _PROXY_ENDPOINTS.add((_url.hostname, _url.port))
        except ValueError:
            pass


def _is_loopback(host):
    if isinstance(host, bytes):
        host = host.decode('ascii', 'replace')
    if host == 'localhost':
        return True
    try:
        address = ipaddress.ip_address(str(host).split('%', 1)[0])
        return address.is_loopback or bool(
            getattr(address, 'ipv4_mapped', None)
            and address.ipv4_mapped.is_loopback
        )
    except ValueError:
        return False


def _check_address(address, event):
    if not isinstance(address, tuple):
        return  # The platform itself denies AF_UNIX; no remote network here.
    host, port = address[:2]
    if not _is_loopback(host) or (host, port) in _PROXY_ENDPOINTS:
        raise PermissionError('Host scaling test isolation blocked non-loopback/proxy ' + event)


def _audit(event, args):
    if event in ('socket.connect', 'socket.bind'):
        _check_address(args[1], event)
    elif event == 'socket.sendto':
        _check_address(args[-1], event)
    elif event == 'socket.getaddrinfo':
        if not _is_loopback(args[0]):
            raise PermissionError('Host scaling test isolation blocked external DNS')
    elif event == 'socket.gethostbyname':
        if not _is_loopback(args[0]):
            raise PermissionError('Host scaling test isolation blocked external DNS')
    elif event == 'socket.gethostbyaddr':
        if not _is_loopback(args[0]):
            raise PermissionError('Host scaling test isolation blocked external reverse DNS')


sys.addaudithook(_audit)
