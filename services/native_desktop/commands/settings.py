"""Discoverable, validated settings for the first-party native interface."""
from __future__ import annotations

import math

from .common import boolean, changed, integer, settings, string

# These are stored state, acknowledgements, or credentials, not preferences.
# In particular no schema is built by walking the user's saved settings file.
_HIDDEN = {"custom_whisper_models", "meeting_agent_models", "audio_input_priority",
           "app_context_excluded_apps", "app_style_overrides", "dictation_languages",
           "dictation_dictionary", "dictation_snippets", "actions", "automation_webhooks",
           "automation_programs", "automation_filters", "walkthrough_closed"}
_INTERNAL_SUFFIXES = ("_seen", "_consent_given", "_last_enabled", "_ack", "_declined")
_CHOICES = {
    "selected_model": ("local_whisper", "api", "parakeet", "qwen_asr", "nemotron", "moonshine", "parakeet_mlx", "parakeet_unified", "remote"),
    "api_transcription_endpoint": ("openai", "groq"),
    "recording_trigger_mode": ("toggle", "hold"),
    "ui_theme": ("dark", "light", "system"),
    "ui_font_scale": (90, 100, 110, 125, 150),
    "streaming_overlay_layout": ("stacked", "single"),
    "recording_overlay_look": ("pill", "bar"),
    "whisper_device": ("auto", "cpu", "cuda"),
    "whisper_compute_type": ("default", "auto", "int8", "int8_float16", "float16", "float32"),
    "transcript_cleanup_level": ("light", "medium", "high"),
    "transcript_cleanup_reasoning": ("off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
    "recording_retention_mode": ("custom", "keep_all", "size_limit"),
    "meeting_agent_core": ("pi", "opencode", "claude_code", "codex", "opencode_cli"),
    "meeting_speaker_id_backend": ("off", "local", "openai"),
    "meeting_server_bind": ("localhost", "lan"),
    "meeting_insight_review_sensitivity": ("normal", "thorough"),
    "typesafe_provider": ("typesafe", "openrouter", "text_model"),
    "typesafe_text_model": ("meeting", "cleanup"),
    "hf_access_policy": ("ask", "always", "never"),
    "remote_records_location": ("local", "host", "both"),
}
_RANGES = {"streaming_overlay_font_size": (10, 48), "streaming_chunk_duration": (0.1, 60),
           "max_saved_recordings": (0, 1_000_000), "max_saved_recordings_mb": (0, 10_000_000),
           "meeting_server_port": (1024, 65535), "remote_host_port": (1024, 65535),
           "mcp_port": (1024, 65535)}
_EXTRA = {"hf_access_policy": "ask", "mcp_enabled": False, "mcp_port": 8767,
          "mcp_tailscale_enabled": False, "mcp_settings_access": False,
          "mcp_retitle_transcriptions": False, "mcp_retitle_meetings": False,
          "remote_host_enabled": False, "remote_host_model_management": False,
          "remote_host_port": 47821, "remote_host_tailscale_trust": False,
          "remote_host_keep_records": False, "remote_host_manage_mcp": False,
          "remote_records_location": "local", "remote_client_history": False,
          "transcript_cleanup_rules": [], "transcript_batch_custom_instructions": "",
          "meeting_asr_source": "local", "dictation_languages": [],
          "app_context_excluded_apps": [], "typesafe_voice_command_names": ["openwhisper"],
          "mcp_writable_settings": []}


def _category(key):
    if key.startswith("walkthrough_"):
        return "walkthroughs", "Walkthroughs"
    if key.startswith("meeting_"):
        return "meetings", "Meetings"
    if key.startswith(("transcript_cleanup_", "transcript_batch_")):
        return "cleanup", "AI cleanup"
    if key.startswith(("typesafe_",)):
        return "intelligence", "Fast judgments"
    if key.startswith(("mcp_", "api_live_", "api_run_", "api_control_", "api_text_", "api_agent_", "voice_routing_")):
        return "automation", "Automation & MCP"
    if key.startswith("remote_") or key == "host_mode":
        return "remote", "Remote computers"
    if key.startswith(("ui_", "streaming_overlay_", "recording_overlay_", "scratchpad_")) or key in ("compact_mode", "minimize_tray"):
        return "appearance", "Appearance"
    if key.startswith(("update_",)):
        return "updates", "Updates"
    if key.startswith(("max_saved_", "confirm_", "recording_retention_")):
        return "history", "History & storage"
    if key.startswith(("app_context_", "app_style", "dictionary_", "snippets_")):
        return "personalization", "Personalization"
    if key.startswith(("whisper_", "parakeet_", "qwen_", "moonshine_", "nemotron_", "api_transcription_")) or key in ("selected_model", "hf_access_policy"):
        return "engines", "Speech engines"
    return "dictation", "Dictation"


def _defaults():
    from services import settings as module
    result = {key: value for key, value in module.SETTING_DEFAULTS.items()
              if key not in _HIDDEN and not key.endswith(_INTERNAL_SUFFIXES)
              and type(value) in (bool, int, float, str)}
    result.update(_EXTRA)
    result["typesafe_voice_command_names"] = list(module.DEFAULT_VOICE_COMMAND_NAMES)
    return result


def _choices(saved=None):
    # Enum definitions are authoritative; keep UI and service validation equal.
    from services import settings as module
    choices = dict(_CHOICES)
    names = {"recording_trigger_mode": "RecordingTriggerMode", "ui_font_scale": "UiFontScale",
             "ui_theme": "UiTheme", "recording_overlay_look": "RecordingOverlayLook",
             "streaming_overlay_layout": "StreamingOverlayLayout", "recording_retention_mode": "RecordingRetentionMode",
             "walkthrough_copy_format": "WalkthroughCopyFormat", "walkthrough_snap_mode": "WalkthroughSnapMode",
             "walkthrough_page_size": "WalkthroughPageSize", "transcript_cleanup_model_sort": "TranscriptCleanupModelSort",
             "transcript_cleanup_reasoning": "TranscriptCleanupReasoning", "meeting_language": "MeetingLanguage",
             "local_asr_language": "MeetingLanguage", "meeting_live_thinking": "TranscriptCleanupReasoning",
             "meeting_final_thinking": "TranscriptCleanupReasoning", "walkthrough_write_up_agent": "MeetingAgentCore"}
    for key, name in names.items():
        enum = getattr(module, name)
        if hasattr(enum, "ALL"):
            choices[key] = enum.ALL
    from services.text_llm import list_profiles
    profile_ids = tuple(p.id for p in list_profiles(saved))
    choices["transcript_cleanup_provider"] = (*profile_ids, *module.TranscriptCleanupProvider.INSTALLED)
    choices["meeting_llm_provider"] = profile_ids
    from services.speech_endpoints import ENDPOINT_IDS
    choices["api_transcription_endpoint"] = ENDPOINT_IDS
    choices["meeting_asr_source"] = ("local", "remote")
    from config import config
    from services.whisper_sources import custom_models
    whisper_models = tuple([*config.WHISPER_MODEL_CHOICES, *custom_models(saved or {})])
    choices["whisper_model"] = whisper_models
    choices["meeting_whisper_model"] = whisper_models
    from services.local_asr.catalog import MODELS
    choices["meeting_asr_model"] = tuple([*whisper_models, *MODELS])
    choices["transcript_batch_relation"] = ("separate", "sequential", "custom")
    choices["walkthrough_write_up_agent"] = module.MeetingAgentCore.INSTALLED
    from services.cleanup_profiles import load_cleanup_profiles
    choices["quick_record_profile"] = ("", *(profile.id for profile in load_cleanup_profiles(saved or {})))
    from services.dictation_language import language_choices
    choices["dictation_active_language"] = ("", *language_choices(saved or {}))
    return choices


def schema(context, _params):
    from services.agent_mcp.controls import CONTROLS_BY_KEY
    from .collections import definitions
    saved = settings(context).load_all_settings()
    groups = {}
    choices = _choices(saved)
    for key, default in _defaults().items():
        group_id, label = _category(key)
        group = groups.setdefault(group_id, {"id": group_id, "label": label,
                                              "description": "", "fields": []})
        control = CONTROLS_BY_KEY.get(key)
        kind = "boolean" if type(default) is bool else "integer" if type(default) is int else "number" if type(default) is float else "json" if isinstance(default, (dict, list)) else "string"
        if key.endswith(("_prompt", "_instructions", "_rules")):
            kind = "json" if isinstance(default, list) else "multiline"
        value = saved.get(key, default)
        try:
            value = _validate(key, value, default, choices)
        except ValueError:
            value = default
        field = {"key": key, "label": control.label if control else key.replace("_", " ").capitalize(),
                 "description": control.effect if control else "Applies to the next operation.",
                 "type": kind, "value": value, "default": default, "writable": True, "secret": False}
        if key in choices:
            field["choices"] = [{"value": v, "label": str(v).replace("_", " ").capitalize()} for v in choices[key]]
        if key in _RANGES:
            field["min"], field["max"] = _RANGES[key]
        if isinstance(default, list):
            field["editor"] = "lines"
            field["placeholder"] = "One item per line"
        group["fields"].append(field)
    return {"categories": list(groups.values()), "collections": definitions(),
            "credentials": _credential_status(saved)}


def values(context, _params):
    saved = settings(context).load_all_settings()
    choices, result = _choices(saved), {}
    for key, default in _defaults().items():
        try:
            result[key] = _validate(key, saved.get(key, default), default, choices)
        except ValueError:
            result[key] = default
    return {"values": result}


def coverage(_context, _params):
    """The complete persisted-key inventory, without reading user data."""
    from services.settings import SettingsKey
    surfaces = {key: "settings.schema" for key in _defaults()}
    structured = {"hotkeys": "hotkeys", "dictation_dictionary": "dictionary",
                  "dictation_snippets": "snippets", "transcript_cleanup_profiles": "profiles",
                  "text_transforms": "transforms", "actions": "actions", "text_llm_profiles": "text_models",
                  "automation_webhooks": "webhooks", "automation_programs": "programs",
                  "automation_filters": "filters", "custom_whisper_models": "speech_models",
                  "api_transcription_servers": "speech_servers", "app_style_overrides": "app_overrides",
                  "walkthrough_write_up_styles": "write_up_styles", "app_style_tones": "app_tones",
                  "meeting_agent_models": "agent_models", "local_asr_devices": "engine_devices"}
    surfaces.update({key: "collections." + collection for key, collection in structured.items()})
    surfaces.update({"audio_input_priority": "devices.priority", "local_asr_models": "models.select",
                     "remote_engine_client": "remote.pair", "remote_host_devices": "remote.state",
                     "guided_setup_state": "setup.state"})
    internal = {
        "audio_input_device": "Legacy microphone index, migrated to devices.priority.",
        "hf_hub_offline": "Legacy preference, migrated to hf_access_policy.",
        "streaming_overlay_enabled": "Legacy preference, migrated to streaming_enabled.",
        "streaming_paste_enabled": "Legacy preference, migrated to streaming_enabled.",
        "window_geometry": "Window position is owned by the native frontend.",
        "compact_window_geometry": "Window position is owned by the native frontend.",
        "host_window_geometry": "Window position is owned by the native frontend.",
        "scratchpad_geometry": "Window position is owned by the native frontend.",
        "last_tab_index": "Navigation state is owned by the native frontend.",
        "walkthrough_closed": "Saved library selection is owned by the native frontend.",
        "update_last_check_at": "Updater scheduling state.",
        "update_last_failed_check_at": "Updater scheduling state.",
        "update_api_retry_at": "Updater rate-limit scheduling state.",
        "meeting_insight_review_consent": "Explicit consent prompt in meeting setup.",
        "meeting_linux_preview_ack_version": "Platform readiness prompt state.",
        "whisper_gpu_offer_declined": "Explicit GPU choice prompt state.",
    }
    for key in vars(SettingsKey).values():
        if isinstance(key, str) and key.endswith(_INTERNAL_SUFFIXES):
            internal.setdefault(key, "Onboarding or per-feature consent prompt state.")
    keys = sorted({value for name, value in vars(SettingsKey).items() if name.isupper() and isinstance(value, str)})
    return {"items": [{"key": key, "surface": surfaces.get(key), "internal": key in internal,
                        "reason": internal.get(key, ""), "covered": key in surfaces or key in internal}
                       for key in keys]}


def _validate(key, value, default, choices):
    if type(default) is bool:
        boolean(value, key)
    elif type(default) is int:
        low, high = _RANGES.get(key, (-1, 10_000_000))
        integer(value, key, low, high)
    elif type(default) is float:
        low, high = _RANGES.get(key, (0, 1_000_000))
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"Invalid {key}.")
    elif isinstance(default, list):
        if not isinstance(value, list) or len(value) > 500 or any(not isinstance(v, str) or len(v) > 10_000 for v in value):
            raise ValueError(f"Invalid {key}.")
    elif isinstance(default, dict):
        if not isinstance(value, dict) or len(value) > 50 or any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 1000 for k, v in value.items()):
            raise ValueError(f"Invalid {key}.")
        if key == "meeting_agent_models":
            from services.settings import MeetingAgentCore
            if set(value) - set(MeetingAgentCore.INSTALLED):
                raise ValueError("Unknown installed meeting agent.")
        elif key == "local_asr_devices":
            from services.local_asr.catalog import BACKENDS
            if set(value) - set(BACKENDS) or any(device not in ("auto", "cpu", "cuda") for device in value.values()):
                raise ValueError("Unknown speech engine device.")
        elif key == "app_style_tones":
            from services.app_styles import AppCategory, Tone
            if set(value) - set(AppCategory.ALL) or any(tone not in Tone.ALL for tone in value.values()):
                raise ValueError("Unknown writing style or tone.")
    else:
        string(value, key, maximum=30_000)
    if key in choices and value not in choices[key]:
        raise ValueError(f"Choose a supported value for {key}.")
    if key == "mcp_writable_settings":
        from services.agent_mcp.controls import CONTROLS_BY_KEY
        if any(name not in CONTROLS_BY_KEY for name in value):
            raise ValueError("Unknown setting in the MCP writable list.")
    if key == "dictation_languages":
        from services.dictation_language import chosen_languages
        if chosen_languages({key: value}) != value:
            raise ValueError("Use unique supported dictation language codes.")
    return value


def update(context, params):
    changes = params.get("changes", {params.get("key"): params.get("value")})
    if not isinstance(changes, dict) or not changes or len(changes) > 100:
        raise ValueError("Supply settings to change.")
    defaults = _defaults()
    prospective = {**settings(context).load_all_settings(), **changes}
    choices = _choices(prospective)
    for key, value in changes.items():
        if key not in defaults:
            raise ValueError("Unsupported setting.")
        _validate(key, value, defaults[key], choices)
    controller = context.controller
    if any(key == "selected_model" or key.startswith(("whisper_", "local_asr_")) for key in changes):
        if controller.recorder.is_recording or controller.transcription_runtime.has_active_job or controller.is_meeting_active():
            raise ValueError("Finish the recording before changing speech engines.")
    if changes.get("meeting_insight_review") is True and prospective.get("meeting_insight_review_consent") != "typesafe-text-v1":
        consent = context.ui.request_prompt("meeting.insight_review", "Check meeting insights?",
            "Insight review sends excerpts from the meeting transcript and its insights to your selected Fast judgments provider. Enable Fast judgments to use this review.",
            [{"label": "Keep review off", "value": False}, {"label": "Allow insight review", "value": True}], default=False)
        if consent is not True:
            return {"ok": False, "canceled": True, "refresh": ["settings.schema"]}
        changes = {**changes, "meeting_insight_review_consent": "typesafe-text-v1"}
    retention = None
    if set(changes) & {"recording_retention_mode", "max_saved_recordings", "max_saved_recordings_mb"}:
        from services.settings import resolve_max_saved_recordings, resolve_max_saved_recordings_bytes
        from .common import history
        retention = (resolve_max_saved_recordings(prospective), resolve_max_saved_recordings_bytes(prospective))
        expired = history(context).recordings_over_limit(*retention)
        if expired and not context.ui.request_prompt("recording.retention", "Remove older recordings?",
                f"This storage limit removes {len(expired)} saved audio recording(s). Their transcript text stays in History.",
                [{"label": "Keep current limit", "value": False}, {"label": "Apply storage limit", "value": True}], default=False):
            return {"ok": False, "canceled": True, "refresh": ["settings.schema"]}
    def mutate(saved):
        saved.update(changes)
        if "streaming_enabled" in changes:
            from services.settings import LEGACY_STREAMING_KEYS
            for key in LEGACY_STREAMING_KEYS:
                saved.pop(key, None)
        if "dictation_languages" in changes:
            from services.dictation_language import set_chosen
            set_chosen(saved, changes["dictation_languages"])
    settings(context).mutate_settings(mutate)
    if retention is not None:
        from .common import history
        history(context).set_retention(*retention)
    if "selected_model" in changes:
        controller.on_model_changed(changes["selected_model"])
    if "recording_trigger_mode" in changes:
        controller.update_recording_trigger_mode(changes["recording_trigger_mode"])
    if any(key.startswith(("whisper_", "local_asr_", "parakeet_", "qwen_", "moonshine_", "nemotron_")) for key in changes):
        controller.reload_whisper_model()
    if "streaming_enabled" in changes:
        controller.reconfigure_streaming()
    for group in {_category(key)[0] for key in changes}:
        controller.on_settings_changed(group)
    if any(key.startswith("remote_host_") for key in changes):
        controller.remote_engine.apply_host_settings()
    if any(key.startswith("mcp_") for key in changes):
        from services.agent_mcp.runtime import runtime
        runtime.restore(settings(context))
    refresh = ["settings.schema", "settings.values"]
    if any(key == "selected_model" or key.startswith(("whisper_", "local_asr_")) for key in changes):
        refresh.append("models.list")
    context.emit("settings.changed", {"keys": list(changes)})
    return changed(*refresh)


_CREDENTIALS = ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY", "TYPESAFE_API_KEY", "HF_TOKEN")


def _credential_names(saved=None):
    from services.text_llm import list_profiles
    from services.speech_endpoints import credential_owners
    return list(dict.fromkeys([*_CREDENTIALS,
        *(p.api_key_env for p in list_profiles(saved) if p.api_key_env),
        *(name for name, _label in credential_owners(saved))]))


def _credential_status(saved=None):
    from services.credentials import credential_source
    from services.text_llm import list_profiles
    names = _credential_names(saved)
    verifiable = {profile.api_key_env for profile in list_profiles(saved) if profile.api_key_env}
    return [{"name": name, "label": name.replace("_", " "), "secret": True,
             "configured": credential_source(name) != "none", "value": None,
             "can_verify": name in verifiable} for name in names]


def credential_write(context, params):
    from services.credentials import store, validate_api_key
    name = params.get("name")
    # Configured text and speech endpoints name their own key slots; never
    # accept arbitrary environment variable names from the frontend.
    if name not in _credential_names(settings(context).load_all_settings()):
        raise ValueError("Unsupported credential.")
    value = string(params.get("value"), "credential", maximum=512)
    if value:
        store().set(name, validate_api_key(value))
    else:
        store().delete(name)
    context.controller.on_settings_changed("credentials")
    return changed("settings.schema", configured=bool(value), name=name)
