"""Structured preference libraries, using their existing domain validators."""
from __future__ import annotations

from dataclasses import asdict
from uuid import uuid4

from .common import changed, identifier, settings, string


def _field(key, label, kind="string", default="", choices=()):
    result = {"key": key, "label": label, "type": kind, "default": default,
              "description": "", "secret": False, "writable": True}
    if choices:
        result["choices"] = [{"value": v, "label": str(v).replace("_", " ")} for v in choices]
    return result


def definitions():
    from services.actions.model import ActionKind
    from services.automation.models import FILTER_KINDS, STAGES
    from services.app_styles import AppCategory, Tone
    fields = {
        "dictionary": [_field("term", "Preferred spelling"), _field("heard", "Sounds like", "json", []), _field("starred", "Favorite", "boolean", False)],
        "snippets": [_field("trigger", "Spoken phrase"), _field("text", "Expansion", "multiline"), _field("formatted", "Formatted Markdown", "boolean", False)],
        "profiles": [_field("name", "Name"), _field("instructions", "Output instructions", "multiline"), _field("hotkey", "Shortcut"), _field("use_learned_rules", "Use learned rules", "boolean", True)],
        "transforms": [_field("name", "Name"), _field("instruction", "Rewrite instructions", "multiline"), _field("hotkey", "Shortcut")],
        "actions": [_field("name", "Name"), _field("description", "Description", "multiline"), _field("kind", "Action", default="clipboard", choices=ActionKind.ALL), _field("params", "Action parameters", "json", {}), _field("hotkey", "Shortcut"), _field("spoken_names", "Spoken names", "json", []), _field("shaping", "Text shaping", default="cleanup"), _field("enabled", "Enabled", "boolean", True)],
        "hotkeys": [_field("value", "Shortcut")],
        "text_models": [_field("name", "Name"), _field("base_url", "API base URL"), _field("api_key_env", "Credential name"), _field("protocol", "Protocol", default="chat", choices=("chat", "responses"))],
        "text_servers": [_field("base_url", "API base URL"), _field("api_key_env", "Credential name")],
        "speech_servers": [_field("base_url", "API base URL"), _field("api_key_env", "Credential name"), _field("model", "Transcription model")],
        "speech_models": [_field("source", "Local folder or Hugging Face owner/model[/subfolder]")],
        "app_overrides": [_field("match", "App or website"), _field("category", "Writing style", default="other", choices=AppCategory.ALL)],
        "app_tones": [_field("tone", "Writing tone", default="formal", choices=Tone.ALL)],
        "agent_models": [_field("model", "Model (empty uses the agent default)")],
        "engine_devices": [_field("device", "Run on", default="auto", choices=("auto", "cpu", "cuda"))],
        "write_up_styles": [_field("name", "Name"), _field("instructions", "Write-up instructions", "multiline")],
        "webhooks": [_field("name", "Name"), _field("url", "URL"), _field("events", "Events", "json", []), _field("enabled", "Enabled", "boolean", True)],
        "programs": [_field("name", "Name"), _field("program", "Program path"), _field("args", "Arguments", "json", []), _field("events", "Events", "json", []), _field("timeout_s", "Timeout (seconds)", "integer", 30), _field("enabled", "Enabled", "boolean", True)],
        "filters": [_field("name", "Name"), _field("kind", "Kind", default="program", choices=FILTER_KINDS), _field("stage", "Stage", default="after_cleanup", choices=STAGES), _field("url", "URL"), _field("program", "Program path"), _field("args", "Arguments", "json", []), _field("timeout_ms", "Timeout (milliseconds)", "integer", 1500), _field("enabled", "Enabled", "boolean", True)],
    }
    labels = {"dictionary": "Dictionary", "snippets": "Snippets", "profiles": "Cleanup profiles",
              "transforms": "Text transforms", "actions": "Actions", "hotkeys": "Keyboard shortcuts",
              "text_models": "Custom text endpoints", "webhooks": "Event webhooks",
              "programs": "Event programs", "filters": "Text filters", "text_servers": "Local text servers",
              "speech_servers": "Transcription servers", "speech_models": "Custom Whisper models",
              "app_overrides": "App writing styles", "app_tones": "Writing tone by app category",
              "agent_models": "Installed agent models", "engine_devices": "Speech engine devices",
              "write_up_styles": "Walkthrough write-up styles"}
    required = {"dictionary": {"term"}, "snippets": {"trigger", "text"},
                "profiles": {"name", "instructions"}, "transforms": {"name", "instruction"},
                "actions": {"name"}, "text_models": {"name", "base_url"},
                "text_servers": {"base_url"}, "speech_servers": {"base_url", "model"},
                "speech_models": {"source"}, "app_overrides": {"match"}, "write_up_styles": {"name", "instructions"},
                "webhooks": {"name", "url"}, "programs": {"name", "program"},
                "filters": {"name"}}
    descriptions = {"heard": 'One alternative phrase per line, for example open whisper.',
                    "params": 'JSON object. A link uses {"template":"https://example.com?q={text}"}; a program uses {"program":"/path/to/program","args":[]}.',
                    "args": 'One program argument per line; each line is passed as one argument.',
                    "events": 'One event name per line, for example transcription.completed.',
                    "spoken_names": 'One short spoken name per line, for example copy this.',
                    "hotkey": "For example ctrl+shift+space. Leave empty to disable the shortcut.",
                    "api_key_env": "Name of the credential slot; save its value in API credentials.",
                    "shaping": "raw, cleanup, profile:ID or transform:ID.",
                    "base_url": "OpenAI-compatible API base URL, including /v1 when required."}
    for collection, schema_fields in fields.items():
        for field in schema_fields:
            field["required"] = field["key"] in required.get(collection, set())
            field["description"] = descriptions.get(field["key"], "")
            if field["key"] in ("heard", "spoken_names", "args", "events"):
                field["editor"] = "lines"
                field["placeholder"] = "One phrase per line"
    return [{"id": key, "label": labels[key], "description": "", "fields": value,
             "can_add": key not in ("hotkeys", "text_servers", "speech_servers", "app_tones", "agent_models", "engine_devices"),
             "can_delete": key not in ("text_servers", "speech_servers", "app_tones", "agent_models", "engine_devices")}
            for key, value in fields.items()]


def _definition(collection):
    result = next((d for d in definitions() if d["id"] == collection), None)
    if result is None:
        raise ValueError("Unknown preference library.")
    return result


def _hotkeys(manager):
    from config import config
    saved = manager.load_hotkey_settings()
    return {key: saved.get(key, default) if isinstance(saved.get(key, default), str) else default
            for key, default in config.DEFAULT_HOTKEYS.items()}


def list_items(context, params):
    collection = params.get("collection")
    definition = _definition(collection)
    saved = settings(context).load_all_settings()
    if collection == "dictionary":
        from services.dictionary import load_dictionary
        items = [asdict(item) for item in load_dictionary(saved)]
    elif collection == "hotkeys":
        items = [{"id": key, "name": key.replace("_", " ").capitalize(), "value": value}
                 for key, value in _hotkeys(settings(context)).items()]
    elif collection == "text_models":
        from services.text_llm import list_custom_profiles
        items = [asdict(item) for item in list_custom_profiles(saved)]
    elif collection == "text_servers":
        from services.text_llm import list_profiles, LOCAL_SERVER_DEFAULT_URLS
        items = [asdict(item) for item in list_profiles(saved) if item.id in LOCAL_SERVER_DEFAULT_URLS]
    elif collection == "speech_servers":
        from services.speech_endpoints import ENDPOINTS, configured_endpoint, endpoint_model
        items = [{"id": endpoint.id, "name": endpoint.name, "base_url": endpoint.base_url or "",
                  "api_key_env": endpoint.connection.api_key_env, "model": endpoint_model(endpoint, saved)}
                 for preset in ENDPOINTS if preset.configurable
                 for endpoint in [configured_endpoint(preset.id, saved)]]
    elif collection == "speech_models":
        from services.whisper_sources import custom_models, custom_model_label
        items = [{"id": name, "name": custom_model_label(name), "source": name} for name in custom_models(saved)]
    elif collection == "app_overrides":
        from services.app_styles import resolve_overrides
        items = [{"id": match, "name": match, "match": match, "category": category}
                 for match, category in resolve_overrides(saved)]
    elif collection == "app_tones":
        from services.app_styles import resolve_tones
        items = [{"id": category, "name": category.capitalize(), "tone": tone}
                 for category, tone in resolve_tones(saved).items()]
    elif collection == "agent_models":
        from services.settings import MeetingAgentCore, resolve_meeting_agent_models
        names = {"claude_code": "Claude Code", "codex": "Codex", "opencode_cli": "OpenCode"}
        choices = resolve_meeting_agent_models(saved)
        items = [{"id": agent, "name": names[agent], "model": choices.get(agent, "")}
                 for agent in MeetingAgentCore.INSTALLED]
    elif collection == "engine_devices":
        from services.local_asr.catalog import BACKENDS, selected_device
        items = [{"id": backend, "name": name, "device": selected_device(backend, saved)} for backend, name in BACKENDS.items()]
    elif collection == "write_up_styles":
        from services.walkthrough.styles import saved_styles
        items = [item.to_dict() for item in saved_styles(saved)]
    else:
        module, _model, loader, _saver, _deleter = _handlers(collection)
        items = [item.to_dict() for item in getattr(module, loader)(saved)]
    for item in items:
        if item.get("base_url"):
            from urllib.parse import urlsplit, urlunsplit
            parts = urlsplit(item["base_url"])
            host = parts.netloc.rsplit("@", 1)[-1]
            item["base_url"] = urlunsplit((parts.scheme, host, parts.path, "", ""))
    return {"collection": collection, "items": items, "total": len(items), "fields": definition["fields"],
            "can_add": definition["can_add"], "can_delete": definition["can_delete"]}


def _handlers(collection):
    from services import snippets, cleanup_profiles, text_transforms
    from services.actions import model as actions
    from services.automation import models as automation
    return {
        "snippets": (snippets, "Snippet", "load_snippets", "save_snippet", "delete_snippet"),
        "profiles": (cleanup_profiles, "CleanupProfile", "load_cleanup_profiles", "save_cleanup_profile", "delete_cleanup_profile"),
        "transforms": (text_transforms, "Transform", "load_transforms", "save_transform", "delete_transform"),
        "actions": (actions, "Action", "load_actions", "save_action", "delete_action"),
        "webhooks": (automation, "Webhook", "load_webhooks", "save_webhook", "delete_webhook"),
        "programs": (automation, "Program", "load_programs", "save_program", "delete_program"),
        "filters": (automation, "TextFilter", "load_filters", "save_filter", "delete_filter"),
    }[collection]


def save(context, params):
    collection = params.get("collection")
    definition = _definition(collection)
    item = params.get("item")
    if not isinstance(item, dict):
        raise ValueError("Supply an item to save.")
    allowed = {field["key"] for field in definition["fields"]}
    if set(item) - allowed - {"id", "name", "learned", "new", "protected", "builtin", "kind", "model_metadata"}:
        raise ValueError("Unsupported item field.")
    values = {field["key"]: item.get(field["key"], field["default"]) for field in definition["fields"]}
    for field in definition["fields"]:
        value = values[field["key"]]
        if field["type"] in ("string", "multiline"):
            string(value, field["label"], maximum=100_000)
        elif field["type"] == "boolean" and type(value) is not bool:
            raise ValueError(f"{field['label']} must be true or false.")
        elif field["type"] == "integer" and type(value) is not int:
            raise ValueError(f"{field['label']} must be an integer.")
        elif field["type"] == "json":
            if isinstance(field["default"], dict) and not isinstance(value, dict):
                raise ValueError(f"{field['label']} must be an object.")
            if isinstance(field["default"], list) and (not isinstance(value, list) or len(value) > 500 or any(not isinstance(entry, str) for entry in value)):
                raise ValueError(f"{field['label']} must be a list of phrases.")
        if field.get("choices") and value not in [choice["value"] for choice in field["choices"]]:
            raise ValueError(f"Invalid {field['label']}.")
    item_id = item.get("id") or uuid4().hex
    string(item_id, "item ID", required=True, maximum=4096)
    manager = settings(context)
    if collection == "hotkeys":
        from services.cleanup_profiles import normalize_hotkey
        current = _hotkeys(manager)
        if item_id not in current:
            raise ValueError("Unknown keyboard shortcut.")
        current[item_id] = normalize_hotkey(values["value"]) if values["value"].strip() else ""
        from services.hotkey_conflicts import hotkey_conflict
        # Check each shortcut against the complete proposed configuration.
        for key, value in current.items():
            if value:
                others = {k: v for k, v in current.items() if k != key}
                conflict = hotkey_conflict(value, manager.load_all_settings(), standard_hotkeys=others)
                if conflict:
                    raise ValueError(f"Shortcut already used by {conflict}.")
        manager.save_hotkey_settings(current)
        context.controller.update_hotkeys(current)
    elif collection == "dictionary":
        from services import dictionary
        if isinstance(values["heard"], str):
            values["heard"] = dictionary.parse_heard(values["heard"])
        if not isinstance(values["heard"], (list, tuple)):
            raise ValueError("Sounds like must be a list of phrases.")
        def mutate(saved):
            if item.get("id"):
                term = dictionary.update_term(saved, item_id, term=values["term"], heard=values["heard"])
                dictionary.set_starred(saved, term.id, values["starred"])
            else:
                term = dictionary.add_term(saved, values["term"], heard=values["heard"], starred=values["starred"])
            return term.id
        item_id = manager.mutate_settings(mutate)
    elif collection == "text_models":
        from services.text_llm import upsert_custom_profile, normalize_local_server_url
        values["base_url"] = normalize_local_server_url(values["base_url"])
        profile = manager.mutate_settings(lambda saved: upsert_custom_profile(saved, profile_id=item.get("id") or None, **values))
        item_id = profile.id
    elif collection == "text_servers":
        from services.text_llm import LOCAL_SERVER_DEFAULT_URLS, normalize_local_server_url, validate_api_key_env
        if item_id not in LOCAL_SERVER_DEFAULT_URLS:
            raise ValueError("Choose an existing local text server.")
        manager.update_settings({f"{item_id}_base_url": normalize_local_server_url(values["base_url"]),
                                 f"{item_id}_api_key_env": validate_api_key_env(values["api_key_env"])})
    elif collection == "speech_servers":
        from services.speech_endpoints import ENDPOINTS, valid_server_url
        from services.text_llm import validate_api_key_env
        if item_id not in {endpoint.id for endpoint in ENDPOINTS if endpoint.configurable}:
            raise ValueError("Choose an existing transcription server.")
        values["base_url"] = valid_server_url(values["base_url"])
        values["api_key_env"] = validate_api_key_env(values["api_key_env"])
        string(values["model"], "transcription model", required=True, maximum=500)
        def save_server(saved):
            servers = dict(saved.get("api_transcription_servers") or {})
            servers[item_id] = values
            saved["api_transcription_servers"] = servers
        manager.mutate_settings(save_server)
    elif collection == "speech_models":
        from services.whisper_sources import parse_source, validate_model_folder, custom_models
        source = parse_source(values["source"])
        item_id = validate_model_folder(source.local_path) if source.local_path else source.name
        def save_source(saved):
            sources = custom_models(saved)
            old_id = item.get("id")
            if old_id:
                sources = [name for name in sources if name != old_id]
            if item_id not in sources:
                sources.append(item_id)
            if len(sources) > 100:
                raise ValueError("Keep at most 100 custom Whisper models.")
            saved["custom_whisper_models"] = sources
        manager.mutate_settings(save_source)
    elif collection == "app_overrides":
        from services.app_styles import set_override, remove_override
        def save_override(saved):
            if item.get("id") and item["id"] != values["match"]:
                remove_override(saved, item["id"])
            set_override(saved, **values)
        manager.mutate_settings(save_override)
        item_id = values["match"]
    elif collection == "app_tones":
        from services.app_styles import set_tone
        manager.mutate_settings(lambda saved: set_tone(saved, item_id, values["tone"]))
    elif collection == "agent_models":
        from services.settings import MeetingAgentCore, resolve_meeting_agent_models
        if item_id not in MeetingAgentCore.INSTALLED:
            raise ValueError("Choose an installed agent.")
        def save_agent_model(saved):
            choices = resolve_meeting_agent_models(saved)
            choices[item_id] = values["model"].strip()
            saved["meeting_agent_models"] = choices
        manager.mutate_settings(save_agent_model)
    elif collection == "engine_devices":
        from services.local_asr.catalog import BACKENDS
        if item_id not in BACKENDS:
            raise ValueError("Choose a speech engine.")
        if item_id == "moonshine" and values["device"] != "cpu":
            raise ValueError("Moonshine runs on the CPU. Choose CPU.")
        if item_id == "parakeet_mlx" and values["device"] == "cuda":
            raise ValueError("Parakeet MLX uses Apple Silicon. Choose Automatic or CPU.")
        if context.controller.recorder.is_recording or context.controller.transcription_runtime.has_active_job or context.controller.is_meeting_active():
            raise ValueError("Finish the recording before changing speech engine devices.")
        def save_device(saved):
            devices = dict(saved.get("local_asr_devices") or {})
            devices[item_id] = values["device"]
            saved["local_asr_devices"] = devices
        manager.mutate_settings(save_device)
        context.controller.reload_whisper_model()
    elif collection == "write_up_styles":
        from services.walkthrough.styles import save_style, delete_style, saved_styles
        def save_write_up_style(saved):
            previous = next((style for style in saved_styles(saved) if style.id == item.get("id")), None)
            if previous and previous.name != values["name"]:
                delete_style(saved, previous.id)
            return save_style(saved, **values)
        item_id = manager.mutate_settings(save_write_up_style).id
    else:
        module, model_name, _loader, saver, _deleter = _handlers(collection)
        model = getattr(module, model_name)(id=item_id, **values)
        manager.mutate_settings(lambda saved: getattr(module, saver)(saved, model))
    context.controller.on_settings_changed(collection)
    if collection == "profiles":
        context.controller.hotkey_runtime.refresh_profile_hotkeys()
    return changed("collections.list", "settings.schema", id=item_id, collection=collection)


def delete(context, params):
    collection = params.get("collection")
    _definition(collection)
    item_id = identifier(params)
    if collection == "hotkeys":
        return save(context, {"collection": collection, "item": {"id": item_id, "value": ""}})
    if collection == "dictionary":
        from services.dictionary import delete_term
        settings(context).mutate_settings(lambda saved: delete_term(saved, item_id))
    elif collection == "text_models":
        from services.text_llm import remove_custom_profile
        settings(context).mutate_settings(lambda saved: remove_custom_profile(saved, item_id))
    elif collection in ("text_servers", "speech_servers", "app_tones", "agent_models", "engine_devices"):
        raise ValueError("Built-in entries cannot be deleted.")
    elif collection == "speech_models":
        from services.whisper_sources import custom_models
        settings(context).mutate_settings(lambda saved: saved.update(custom_whisper_models=[name for name in custom_models(saved) if name != item_id]))
    elif collection == "app_overrides":
        from services.app_styles import remove_override
        settings(context).mutate_settings(lambda saved: remove_override(saved, item_id))
    elif collection == "write_up_styles":
        from services.walkthrough.styles import delete_style
        settings(context).mutate_settings(lambda saved: delete_style(saved, item_id))
    else:
        module, _model, _loader, _saver, deleter = _handlers(collection)
        settings(context).mutate_settings(lambda saved: getattr(module, deleter)(saved, item_id))
    if collection == "actions":
        from services.actions.runtime import delete_action_secret
        delete_action_secret(item_id)
    elif collection == "webhooks":
        from services.automation.webhooks import delete_secret
        delete_secret(item_id)
    context.controller.on_settings_changed(collection)
    if collection == "profiles":
        context.controller.hotkey_runtime.refresh_profile_hotkeys()
    return changed("collections.list", "settings.schema", collection=collection)
