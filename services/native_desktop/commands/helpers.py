"""Optional connection checks and action helpers used by native settings."""
from __future__ import annotations

from dataclasses import asdict
from uuid import uuid4

from .common import boolean, identifier, settings, string


def secret_copy(context, params):
    collection = params.get("collection")
    item_id = identifier(params)
    renew = boolean(params.get("renew", False), "renew")
    saved = settings(context).load_all_settings()
    if collection == "actions":
        from services.actions.model import find_action, ActionKind, action_secret_name
        item = find_action(saved, item_id)
        if item is None or item.kind != ActionKind.WEBHOOK:
            raise ValueError("Choose a saved webhook action.")
        from services.actions.runtime import ensure_action_secret
        name = action_secret_name(item_id)
        ensure = lambda: ensure_action_secret(item_id)
    elif collection == "webhooks":
        from services.automation.models import find_webhook, webhook_secret_name
        if find_webhook(saved, item_id) is None:
            raise ValueError("Choose a saved webhook.")
        from services.automation.webhooks import ensure_secret
        name = webhook_secret_name(item_id)
        ensure = lambda: ensure_secret(item_id)
    else:
        raise ValueError("This library has no signing secrets.")
    if renew:
        approved = context.ui.request_prompt("automation.secret", "Replace the signing secret?",
            "Update your receiving service with the new secret before it accepts requests again.",
            [{"label": "Keep current secret", "value": False}, {"label": "Replace and copy", "value": True}], default=False)
        if approved is not True:
            return {"ok": False, "canceled": True}
        from services.automation.signing import new_secret
        from services.credentials import store
        store().set(name, new_secret())
    secret = ensure()
    if not context.ui.clipboard.copy_to_clipboard(secret):
        raise ValueError("The signing secret could not be copied. Try Copy again.")
    return {"ok": True, "copied": True, "collection": collection, "id": item_id}


def test_item(context, params):
    collection = params.get("collection")
    item_id = identifier(params)
    token = uuid4().hex
    key = f"{collection}:{item_id}"
    results = context.ui.state.setdefault("collection_tests", {})
    if results.get(key, {}).get("status") == "running":
        raise ValueError("This test is already running.")
    def finished(result):
        payload = {"token": token, "collection": collection, "id": item_id,
                   "status": "done", "result": asdict(result)}
        context.ui.state["collection_tests"][key] = payload
        context.emit("collections.test.finished", payload)
    if collection == "actions":
        from services.actions.model import find_action
        action = find_action(settings(context).load_all_settings(), item_id)
        if action is None:
            raise ValueError("Action was not found.")
        text = string(params.get("text", "Test from OpenWhisper"), "test text", required=True, maximum=10_000)
        results[key] = {"token": token, "status": "running"}
        context.controller.action_runtime.test(action, text, finished)
    elif collection == "webhooks":
        from services.automation.models import find_webhook
        if find_webhook(settings(context).load_all_settings(), item_id) is None:
            raise ValueError("Webhook was not found.")
        from services.automation.webhooks import send_test
        results[key] = {"token": token, "status": "running"}
        send_test(item_id).add_done_callback(lambda future: finished(future.result()))
    else:
        raise ValueError("Try it is available for actions and webhooks.")
    return {"ok": True, "token": token, "collection": collection, "id": item_id}


def _background(context, kind, work):
    from .system import _background as run
    def request():
        try:
            return work()
        except Exception as exc:
            # Provider response bodies and request URLs can echo credentials.
            raise ValueError(f"The provider request failed ({type(exc).__name__}).") from None
    return run(context, kind, request)


def text_catalog(context, params):
    from services.text_llm import get_profile, list_chat_models
    profile = get_profile(identifier(params), settings(context).load_all_settings())
    if profile is None:
        raise ValueError("Text endpoint was not found.")
    return _background(context, "text_models.catalog", lambda: {"profile_id": profile.id,
        "items": [{"id": name, "name": name} for name in list_chat_models(profile)]})


def speech_catalog(context, params):
    from services.speech_endpoints import ENDPOINT_IDS, configured_endpoint, list_models
    endpoint_id = identifier(params)
    if endpoint_id not in ENDPOINT_IDS:
        raise ValueError("Transcription endpoint was not found.")
    endpoint = configured_endpoint(endpoint_id, settings(context).load_all_settings())
    return _background(context, "speech_servers.catalog", lambda: {"endpoint_id": endpoint_id,
        "items": [{"id": name, "name": name} for name in list_models(endpoint)]})


def verify_credential(context, params):
    from services.text_llm import list_profiles, verify_api_key, resolve_api_key
    name = params.get("name")
    profile_id = params.get("profile_id")
    profiles = list_profiles(settings(context).load_all_settings())
    profile = next((p for p in profiles if p.id == profile_id or (not profile_id and p.api_key_env == name)), None)
    if profile is None:
        raise ValueError("Choose a text provider to verify its saved API key.")
    def verify():
        key = resolve_api_key(profile)
        if not key:
            raise ValueError("Save an API key for this provider first.")
        ok, message = verify_api_key(profile, key)
        return {"ok": ok, "message": message, "name": profile.api_key_env, "profile_id": profile.id}
    return _background(context, "credentials.verify", verify)


COMMANDS = {
    "collections.secret.copy": secret_copy,
    "collections.test": test_item,
    "collections.tests": lambda c, p: {"items": list(c.ui.state.get("collection_tests", {}).values())},
    "text_models.catalog": text_catalog,
    "speech_servers.catalog": speech_catalog,
    "credentials.verify": verify_credential,
    "operations.list": lambda c, p: {"items": list(c.ui.state.get("system_operations", {}).values())},
}
