"""A refresh that could not be saved leaves the subscription's secret as it was.

The agent is told the refresh failed, so it keeps signing checks with the
secret it had. A new secret left in the credential store (with the old one
only as the rotation's "previous") would sign deliveries with a key the
agent never got, and after the rotation window with that key alone.
"""

import time

import pytest

from services import credentials
from services.agent_mcp import events_ext
from services.automation import catalog

OLD = "whsec_" + "A" * 32
NEWER = "whsec_" + "B" * 32
REFUSED = "whsec_" + "C" * 32


@pytest.fixture
def store():
    """A credential store in memory, apart from every other test's."""
    fake = credentials.CredentialStore(backend_factory=credentials.memory_backend)
    previous = credentials.set_store(fake)
    yield fake
    credentials.set_store(previous)


@pytest.fixture
def registry(tmp_path, store):
    return events_ext._Registry(str(tmp_path / "subscriptions.json"))


def subscription(*, expires_in=3600.0):
    return events_ext.EventSubscription(
        "sub_" + "1" * 32, catalog.HISTORY_SAVED, {}, "https://example.com/hook",
        int(time.time() + expires_in), "", events_ext.ORIGIN_LOCAL,
    )


def failing_writes(registry, monkeypatch):
    def refuse(payload):
        raise OSError("disk full")

    monkeypatch.setattr(registry, "_write", refuse)


def test_a_refresh_that_could_not_be_saved_keeps_the_old_secret(registry, store, monkeypatch):
    saved = subscription()
    registry.upsert(saved, OLD)

    failing_writes(registry, monkeypatch)
    with pytest.raises(OSError):
        registry.upsert(subscription(expires_in=7200.0), REFUSED)

    assert store.get(events_ext.secret_name(saved.id)) == OLD
    assert registry.signing_secrets(saved.id) == [OLD]
    assert registry.get(saved.id) == saved


def test_a_rotation_in_progress_survives_a_refresh_that_failed(registry, store, monkeypatch):
    saved = subscription()
    registry.upsert(saved, OLD)
    registry.upsert(saved, NEWER)  # a rotation: OLD still signs for a while
    assert registry.signing_secrets(saved.id) == [NEWER, OLD]

    failing_writes(registry, monkeypatch)
    with pytest.raises(OSError):
        registry.upsert(saved, REFUSED)

    assert store.get(events_ext.secret_name(saved.id)) == NEWER
    assert registry.signing_secrets(saved.id) == [NEWER, OLD]


def test_a_refresh_with_the_same_secret_that_failed_changes_nothing(registry, store, monkeypatch):
    saved = subscription()
    registry.upsert(saved, OLD)
    failing_writes(registry, monkeypatch)
    with pytest.raises(OSError):
        registry.upsert(subscription(expires_in=7200.0), OLD)
    assert store.get(events_ext.secret_name(saved.id)) == OLD
    assert registry.signing_secrets(saved.id) == [OLD]


def test_a_new_subscription_that_could_not_be_saved_leaves_no_secret(registry, store, monkeypatch):
    failing_writes(registry, monkeypatch)
    with pytest.raises(OSError):
        registry.upsert(subscription(), OLD)
    assert store.get(events_ext.secret_name(subscription().id)) is None
    assert registry.active() == []
