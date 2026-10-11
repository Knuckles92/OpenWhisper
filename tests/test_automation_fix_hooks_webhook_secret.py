"""A delivery or test send for a webhook being deleted never brings its signing
secret back after Settings forgot it (review: lazy secret recreation)."""

from __future__ import annotations

from services import credentials
from services.automation import webhooks
from services.automation.models import load_webhooks, webhook_secret_name
from services.settings import SettingsKey
from tests import test_automation_hooks_webhooks as base
from tests.test_automation_hooks_webhooks import hook, save_hooks

receiver = base.receiver
store = base.store
isolated = base.isolated


def _event():
    return webhooks._synthetic_event("recording.started", {"job_id": "1"})


def _deliver(hook_obj):
    clients = {}

    def client_for(private):
        clients.setdefault(private, webhooks._new_client(private))
        return clients[private]

    try:
        return webhooks._deliver(hook_obj, _event(), 1, client_for)[0]
    finally:
        for client in clients.values():
            client.close()


def _delete_like_settings(store, hook_id):
    # Settings removes the webhook first, then forgets its secret.
    store.values[SettingsKey.AUTOMATION_WEBHOOKS] = [
        row for row in store.values.get(SettingsKey.AUTOMATION_WEBHOOKS, [])
        if row["id"] != hook_id
    ]
    webhooks.delete_secret(hook_id)


def test_a_delivery_after_the_webhook_was_deleted_does_not_recreate_its_secret(receiver, store):
    save_hooks(store, hook("a", receiver.url("/a")))
    [saved] = load_webhooks(store.values)
    webhooks.ensure_secret("a")
    _delete_like_settings(store, "a")

    # A delivery that was already on its way when the webhook was deleted.
    record = _deliver(saved)

    assert credentials.store().get(webhook_secret_name("a")) is None
    assert not record.ok and record.error == "The signing secret is missing."
    assert receiver.requests == []


def test_a_saved_webhooks_first_delivery_still_makes_its_secret(receiver, store):
    save_hooks(store, hook("a", receiver.url("/a")))
    [saved] = load_webhooks(store.values)
    assert credentials.store().get(webhook_secret_name("a")) is None

    record = _deliver(saved)

    assert record.ok
    secret = credentials.store().get(webhook_secret_name("a"))
    assert secret and secret.startswith("whsec_")
    assert webhooks.ensure_secret("a") == secret
