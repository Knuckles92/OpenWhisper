"""The channel sends the saved access token only to OpenWhisper on this computer.

OPENWHISPER_URL may name another computer. The token in the credential store
is this computer's; sending it there would hand it to whoever answers. Another
computer needs its own OPENWHISPER_TOKEN, over https.
"""

import pytest

from services.automation import channel_bridge as bridge
from services.automation.channel_bridge import AppLink, LinkError

STORED = "stored-token-from-the-credential-store-0123456789"


class NoConnections:
    """Stands in for http.client's connections: records any attempt."""

    attempts: list = []

    def __init__(self, host, port, timeout=None):
        NoConnections.attempts.append((host, port))
        raise AssertionError("no connection should be made")


@pytest.fixture
def no_network(monkeypatch):
    NoConnections.attempts = []
    monkeypatch.setattr(bridge.http.client, "HTTPConnection", NoConnections)
    monkeypatch.setattr(bridge.http.client, "HTTPSConnection", NoConnections)
    return NoConnections.attempts


def refuse_store_reads():
    pytest.fail("the saved token was read for another computer")


@pytest.mark.parametrize("url", ["https://openwhisper.example/", "https://100.82.22.3:8767"])
def test_another_computer_needs_its_own_token(no_network, url):
    link = AppLink(url=url, read_token=refuse_store_reads)
    with pytest.raises(LinkError) as caught:
        link.notify("Done.")
    assert caught.value.state == "no_token"
    assert "OPENWHISPER_TOKEN" in caught.value.message
    with pytest.raises(LinkError):
        link.connect(5.0)
    assert no_network == []


@pytest.mark.parametrize("url", ["http://203.0.113.5:8767", "http://openwhisper.example/"])
def test_another_computer_needs_https_even_with_a_token(no_network, url):
    link = AppLink(url=url, token="explicit-token-0123456789abcdef0123", read_token=refuse_store_reads)
    with pytest.raises(LinkError) as caught:
        link.target()
    assert caught.value.state == "bad_url"
    assert "https://" in caught.value.message
    assert no_network == []


def test_https_with_its_own_token_is_allowed():
    link = AppLink(url="https://openwhisper.example/base", token="explicit-token-0123456789abcdef0123",
                   read_token=refuse_store_reads)
    assert link.target() == bridge.Target("openwhisper.example", 443, True, "/base")
    assert link.token() == "explicit-token-0123456789abcdef0123"


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8767", "http://localhost:8767", "http://[::1]:8767", "http://127.0.0.2:9"],
)
def test_this_computer_still_uses_the_saved_token(url):
    link = AppLink(url=url, read_token=lambda: STORED)
    link.target()
    assert link.token() == STORED
