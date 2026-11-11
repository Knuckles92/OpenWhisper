"""Typed settings: declarations, the typed read path, and equivalence.

The equivalence tests compare every resolver with its pre-refactor version
(``tests/legacy_settings_77b63c1.py``, services/settings.py as it was), and
every migrated call site's typed read with the expression it replaced, over
missing, valid and malformed stored values. A stored value both versions
reject must read the same; the only allowed difference is where the old code
raised (it had no effective value) and the typed read now returns the default.

The snapshot is a one-time oracle for that refactor, not a specification.
Settings added since are not in it and are not compared, so adding one needs
nothing here. If you deliberately change an existing setting's default or
validation and an equivalence test fails, the oracle has done its job: delete
``tests/legacy_settings_77b63c1.py`` and the equivalence sections below, and
keep the tests of the declarations themselves.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import pickle
from typing import Any, Callable

import pytest

from config import config
from services import settings as new
from services.setting_types import (
    BoolSetting,
    ChoiceSetting,
    IntSetting,
    Setting,
)
from services.settings import SETTING_DEFAULTS, SettingsKey
from tests import legacy_settings_77b63c1 as old

MISSING = object()

#: Stored values every setting is tried with: JSON-shaped junk and near-misses.
JUNK: tuple[Any, ...] = (
    None, True, False, 0, 1, -3, 7, 100, 3.7, "", " ", "yes", "no", "0", "12",
    "auto", "  EN ", "garbage", [], ["a"], [1, 2], {}, {"a": 1}, {"claude-code": "m"},
)


def _battery(setting: Setting[Any]) -> list[Any]:
    """MISSING, the junk values, the default, and every declared choice/alias."""
    values: list[Any] = [MISSING, *JUNK, setting.default]
    values += list(getattr(setting, "choices", ()))
    values += list(getattr(setting, "aliases", {}))
    if isinstance(setting, IntSetting):
        values += [0, 1, 9, 10, 48, 49, 65535, 65536, -1, "47", 8767.0]
    return values


def _mapping(setting: str, value: Any) -> dict[str, Any]:
    return {} if value is MISSING else {str(setting): copy.deepcopy(value)}


def _same(new_reader: Callable[[dict], Any], old_reader: Callable[[dict], Any],
          settings: dict[str, Any]) -> None:
    try:
        expected = old_reader(copy.deepcopy(settings))
    except Exception:
        # The old code crashed on this value; the typed read must not.
        new_reader(copy.deepcopy(settings))
        return
    assert _plain(new_reader(copy.deepcopy(settings))) == _plain(expected), settings


def _plain(value: Any) -> Any:
    # The snapshot has its own copy of each dataclass.
    return dataclasses.asdict(value) if dataclasses.is_dataclass(value) else value


# --- The declarations -----------------------------------------------------


def test_every_key_is_a_declared_setting_with_its_old_name():
    old_keys = {name: value for name, value in vars(old.SettingsKey).items() if name.isupper()}
    new_keys = {name: value for name, value in vars(SettingsKey).items() if name.isupper()}
    # Every key the snapshot had is still declared; newer ones are welcome.
    assert set(old_keys) <= set(new_keys)
    for name, setting in new_keys.items():
        assert isinstance(setting, Setting), name
        assert type(setting.key) is str
        if name in old_keys:
            assert setting == old_keys[name] and setting.key == old_keys[name], name


def test_the_defaults_table_is_unchanged():
    # For every setting the snapshot had; a setting added since has no old default.
    assert {key: SETTING_DEFAULTS[key] for key in old.SETTING_DEFAULTS} == dict(
        old.SETTING_DEFAULTS)
    snapshot_keys = {value for name, value in vars(old.SettingsKey).items()
                     if name.isupper()}
    assert {key for key in SETTING_DEFAULTS if key in snapshot_keys} == set(
        old.SETTING_DEFAULTS)


def test_every_table_default_is_what_a_read_of_nothing_returns():
    for setting in new.ALL_SETTINGS:
        if setting.in_table:
            assert setting.read({}) == SETTING_DEFAULTS[setting], setting
        assert setting.read({}) == setting.default, setting


def test_defaults_are_fresh_copies():
    first = SettingsKey.ACTIONS.read({})
    first.append("changed")
    assert SettingsKey.ACTIONS.read({}) == []
    assert SETTING_DEFAULTS[SettingsKey.ACTIONS] == []


def test_a_setting_behaves_as_its_key_in_json_dicts_copies_and_pickles():
    key = SettingsKey.AUTO_PASTE
    saved = {key: False, SettingsKey.WHISPER_MODEL: "small"}
    assert json.loads(json.dumps(saved)) == {"auto_paste": False, "whisper_model": "small"}
    assert {"auto_paste": 1}[key] == 1
    assert copy.deepcopy(saved) == saved and copy.deepcopy(key) is key
    assert copy.copy(key) is key
    assert type(pickle.loads(pickle.dumps(key))) is str
    assert pickle.loads(pickle.dumps(key)) == "auto_paste"
    assert f"{key}" == "auto_paste" and repr(key) == "'auto_paste'"
    assert hash(key) == hash("auto_paste")


def test_settings_by_key_finds_each_declaration_from_its_bare_name():
    for setting in new.ALL_SETTINGS:
        assert new.SETTINGS_BY_KEY[str(setting)] is setting


def test_read_with_no_mapping_reads_the_settings_file(monkeypatch):
    monkeypatch.setattr(
        new.settings_manager, "load_all_settings",
        lambda: {SettingsKey.LOCAL_ASR_LANGUAGE: "auto", SettingsKey.AUTO_PASTE: "yes"},
    )
    assert SettingsKey.LOCAL_ASR_LANGUAGE.read() == "auto"
    assert SettingsKey.AUTO_PASTE.read() is True
    assert SettingsKey.COPY_CLIPBOARD.read() is True


def test_stored_tells_an_unset_or_invalid_value_from_the_default():
    assert SettingsKey.MEETING_ASR_MODEL.stored({}) is None
    assert SettingsKey.MEETING_ASR_MODEL.stored({SettingsKey.MEETING_ASR_MODEL: 3}) is None
    assert SettingsKey.MEETING_ASR_MODEL.stored({SettingsKey.MEETING_ASR_MODEL: "x"}) == "x"


def test_kinds_validate_as_documented():
    flag = BoolSetting("flag", True)
    assert [flag.parse(v) for v in (False, 0, "no", None)] == [False, True, True, True]
    port = IntSetting("port", 80, strict=True, valid=(1, 65535))
    assert [port.parse(v) for v in (8080, "8080", True, 0, 70000)] == [8080, 80, 80, 80, 80]
    size = IntSetting("size", 12, clamp=(10, 48))
    assert [size.parse(v) for v in ("60", 3.9, "x")] == [48, 10, 12]
    look = ChoiceSetting("look", "a", ("a", "b"), aliases={"old": "b"})
    assert [look.parse(v) for v in ("b", "old", "c", ["b"])] == ["b", "b", "a", "a"]
    with pytest.raises(ValueError):
        ChoiceSetting("bad", "z", ("a",))


# --- Equivalence with the pre-refactor resolvers ----------------------------

#: Old resolvers that read exactly one key, and that key.
SINGLE_KEY_RESOLVERS = {
    name: key
    for name, key in {
        "resolve_recording_trigger_mode": SettingsKey.RECORDING_TRIGGER_MODE,
        "resolve_recording_overlay_look": SettingsKey.RECORDING_OVERLAY_LOOK,
        "resolve_streaming_overlay_layout": SettingsKey.STREAMING_OVERLAY_LAYOUT,
        "resolve_transcript_cleanup_reasoning": SettingsKey.TRANSCRIPT_CLEANUP_REASONING,
        "resolve_transcript_batch_relation": SettingsKey.TRANSCRIPT_BATCH_RELATION,
        "resolve_meeting_agent_core": SettingsKey.MEETING_AGENT_CORE,
        "resolve_meeting_backup_agent": SettingsKey.MEETING_BACKUP_AGENT,
        "resolve_meeting_live_thinking": SettingsKey.MEETING_LIVE_THINKING,
        "resolve_meeting_final_thinking": SettingsKey.MEETING_FINAL_THINKING,
        "resolve_meeting_server_bind": SettingsKey.MEETING_SERVER_BIND,
        "resolve_transcript_cleanup_level": SettingsKey.TRANSCRIPT_CLEANUP_LEVEL,
        "resolve_typesafe_provider": SettingsKey.TYPESAFE_PROVIDER,
        "resolve_typesafe_text_model": SettingsKey.TYPESAFE_TEXT_MODEL,
        "resolve_walkthrough_page_size": SettingsKey.WALKTHROUGH_PAGE_SIZE,
        "resolve_walkthrough_copy_format": SettingsKey.WALKTHROUGH_COPY_FORMAT,
        "resolve_walkthrough_write_up_agent": SettingsKey.WALKTHROUGH_WRITE_UP_AGENT,
        "resolve_walkthrough_snap_mode": SettingsKey.WALKTHROUGH_SNAP_MODE,
        "resolve_walkthrough_capture_delay": SettingsKey.WALKTHROUGH_CAPTURE_DELAY,
        "resolve_walkthrough_title": SettingsKey.WALKTHROUGH_TITLE,
        "resolve_walkthrough_write_up_instructions":
            SettingsKey.WALKTHROUGH_WRITE_UP_INSTRUCTIONS,
        "resolve_ui_font_scale": SettingsKey.UI_FONT_SCALE,
        "resolve_streaming_overlay_font_size": SettingsKey.STREAMING_OVERLAY_FONT_SIZE,
        "resolve_transcript_batch_custom_instructions":
            SettingsKey.TRANSCRIPT_BATCH_CUSTOM_INSTRUCTIONS,
        "resolve_meeting_language": SettingsKey.MEETING_LANGUAGE,
        "resolve_meeting_server_port": SettingsKey.MEETING_SERVER_PORT,
        "resolve_meeting_agent_models": SettingsKey.MEETING_AGENT_MODELS,
        "resolve_transcript_cleanup_rules": SettingsKey.TRANSCRIPT_CLEANUP_RULES,
        "resolve_typesafe_voice_command_names": SettingsKey.TYPESAFE_VOICE_COMMAND_NAMES,
        "resolve_meeting_context_folder_path": SettingsKey.MEETING_CONTEXT_FOLDER_PATH,
        "resolve_meeting_linux_preview_ack": SettingsKey.MEETING_LINUX_PREVIEW_ACK_VERSION,
        "resolve_meeting_speaker_id_backend": SettingsKey.MEETING_SPEAKER_ID_BACKEND,
        "resolve_ui_theme": SettingsKey.UI_THEME,
    }.items()
}
# Every generated on/off resolver.
SINGLE_KEY_RESOLVERS.update({
    name: SettingsKey.__dict__[key_name]
    for name in dir(old)
    if name.startswith("resolve_")
    for key_name in [
        key_name for key_name, value in vars(old.SettingsKey).items()
        if key_name.isupper() and getattr(old, name).__doc__
        and f"``{value}`` setting, or" in (getattr(old, name).__doc__ or "")
    ]
})


def test_the_on_off_resolvers_were_all_found():
    assert len([n for n, k in SINGLE_KEY_RESOLVERS.items()
                if isinstance(k, BoolSetting)]) >= 45


@pytest.mark.parametrize("name", sorted(SINGLE_KEY_RESOLVERS))
def test_single_key_resolver_matches_the_old_one(name):
    key = SINGLE_KEY_RESOLVERS[name]
    for value in _battery(key):
        _same(getattr(new, name), getattr(old, name), _mapping(key, value))


@pytest.mark.parametrize("name", sorted(SINGLE_KEY_RESOLVERS))
def test_the_declaration_reads_what_its_resolver_returns(name):
    key = SINGLE_KEY_RESOLVERS[name]
    if name in {
        # Derived values: a resolver on top of the stored value.
        "resolve_transcript_cleanup_rules", "resolve_typesafe_voice_command_names",
        "resolve_meeting_context_folder_path", "resolve_meeting_linux_preview_ack",
        "resolve_meeting_speaker_id_backend", "resolve_ui_theme",
        "resolve_meeting_agent_models",
    }:
        pytest.skip("derived value")
    for value in _battery(key):
        _same(key.read, getattr(old, name), _mapping(key, value))


#: Old resolvers that combine keys: tried with each key's battery in turn.
MULTI_KEY_RESOLVERS = {
    "resolve_max_saved_recordings": (
        SettingsKey.RECORDING_RETENTION_MODE, SettingsKey.MAX_SAVED_RECORDINGS),
    "resolve_max_saved_recordings_bytes": (
        SettingsKey.RECORDING_RETENTION_MODE, SettingsKey.MAX_SAVED_RECORDINGS_MB),
    "resolve_transcript_cleanup_provider": (
        SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER, SettingsKey.TRANSCRIPT_CLEANUP_MODEL),
    "resolve_transcript_cleanup_model": (
        SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER, SettingsKey.TRANSCRIPT_CLEANUP_MODEL),
    "resolve_meeting_llm_provider": (
        SettingsKey.MEETING_LLM_PROVIDER, SettingsKey.MEETING_LLM_MODEL),
    "resolve_meeting_llm_model": (
        SettingsKey.MEETING_LLM_PROVIDER, SettingsKey.MEETING_LLM_MODEL),
    "resolve_meeting_speech": (
        SettingsKey.MEETING_SPEECH_SEPARATE, SettingsKey.MEETING_ASR_SOURCE,
        SettingsKey.SELECTED_MODEL, SettingsKey.WHISPER_MODEL,
        SettingsKey.MEETING_ASR_MODEL, SettingsKey.MEETING_WHISPER_MODEL),
    "own_meeting_speech": (
        SettingsKey.MEETING_ASR_SOURCE, SettingsKey.SELECTED_MODEL,
        SettingsKey.MEETING_ASR_MODEL, SettingsKey.MEETING_WHISPER_MODEL),
    "resolve_meeting_report_views": (
        SettingsKey.MEETING_REPORT_RIBBON, SettingsKey.MEETING_REPORT_BRIEF,
        SettingsKey.MEETING_REPORT_SIGNAL),
    "resolve_meeting_insight_review": (
        SettingsKey.MEETING_INSIGHT_REVIEW, SettingsKey.MEETING_INSIGHT_REVIEW_CONSENT,
        SettingsKey.MEETING_INSIGHT_REVIEW_SENSITIVITY, SettingsKey.TYPESAFE_ENABLED),
    "resolve_typesafe_topic_shift_enabled": (
        SettingsKey.TYPESAFE_ENABLED, SettingsKey.TYPESAFE_TOPIC_SHIFT_ENABLED),
    "resolve_typesafe_voice_commands_enabled": (
        SettingsKey.TYPESAFE_ENABLED, SettingsKey.TYPESAFE_VOICE_COMMANDS_ENABLED),
    "resolve_api_transcription_model": (
        SettingsKey.API_TRANSCRIPTION_MODEL, SettingsKey.SELECTED_MODEL),
}

#: Valid backgrounds the multi-key batteries vary one key against.
BACKGROUNDS: tuple[dict[str, Any], ...] = (
    {},
    {SettingsKey.RECORDING_RETENTION_MODE: "size_limit",
     SettingsKey.TYPESAFE_ENABLED: True,
     SettingsKey.MEETING_INSIGHT_REVIEW: True,
     SettingsKey.MEETING_INSIGHT_REVIEW_CONSENT: old.INSIGHT_REVIEW_CONSENT,
     SettingsKey.MEETING_SPEECH_SEPARATE: True,
     SettingsKey.SELECTED_MODEL: "local_whisper",
     SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openai",
     SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "gpt-4o-mini",
     SettingsKey.MEETING_LLM_PROVIDER: "openrouter",
     SettingsKey.MEETING_LLM_MODEL: "some/model"},
    {SettingsKey.RECORDING_RETENTION_MODE: "keep_all",
     SettingsKey.SELECTED_MODEL: "api_gpt4o",
     SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "claude-code",
     SettingsKey.TRANSCRIPT_CLEANUP_MODEL: " opus ",
     SettingsKey.MEETING_ASR_SOURCE: "remote"},
    {SettingsKey.SELECTED_MODEL: "parakeet", SettingsKey.MEETING_SPEECH_SEPARATE: False},
)


@pytest.mark.parametrize("name", sorted(MULTI_KEY_RESOLVERS))
def test_multi_key_resolver_matches_the_old_one(name):
    for background in BACKGROUNDS:
        for key in MULTI_KEY_RESOLVERS[name]:
            for value in _battery(key):
                settings = {**background, **_mapping(key, value)}
                if value is MISSING:
                    settings.pop(str(key), None)
                _same(getattr(new, name), getattr(old, name), settings)


def test_resolve_bool_setting_matches_the_old_one_for_every_on_off_key():
    for setting in new.ALL_SETTINGS:
        if isinstance(setting, BoolSetting) and setting.in_table:
            for value in _battery(setting):
                _same(lambda s, k=setting: new.resolve_bool_setting(k, s),
                      lambda s, k=setting: old.resolve_bool_setting(k, s),
                      _mapping(setting, value))


def test_resolve_bool_setting_refuses_a_key_that_is_not_on_off():
    with pytest.raises(TypeError):
        new.resolve_bool_setting(SettingsKey.WHISPER_MODEL, {})


# --- Equivalence of migrated call sites ------------------------------------
#
# Each entry is (setting, the expression the call site used at 77b63c1, the
# typed read that replaced it), with ``s`` for the loaded mapping. They must
# agree on a missing key, on every stored value the declaration accepts as
# it is, and wherever the old expression itself produced such a value. Where
# the old expression handed an invalid stored value (wrong type, not one of
# the choices, out of range, None) on to the code after it, which no setting
# the app writes can hold, the typed read gives the default instead: the one
# allowed difference.

D = old.SETTING_DEFAULTS
CALL_SITES: dict[str, list[tuple[Setting[Any], Callable[[dict], Any],
                                  Callable[[dict], Any], Any]]] = {}


def call_site(module: str, setting: Setting[Any], before: Callable[[dict], Any],
              after: Callable[[dict], Any] | None = None, result: Any = None) -> None:
    """Register a migrated read; ``result`` is the type of what the two return
    when it is not the setting's value (a predicate on it, say)."""
    CALL_SITES.setdefault(module, []).append((setting, before, after or setting.read, result))


def _valid(setting: Setting[Any], result: Any, value: Any) -> bool:
    """Whether ``value`` is a setting value the declaration keeps as it is."""
    if result is not None:
        return isinstance(value, result)
    return value is not None and setting.stored({str(setting): value}) == value and (
        type(setting.stored({str(setting): value})) is type(value))


# Registered call sites, by module.
call_site("runtime.streaming", SettingsKey.STREAMING_CHUNK_DURATION,
          lambda s: old.setting_value(SettingsKey.STREAMING_CHUNK_DURATION, s))
call_site("cleanup_prompts", SettingsKey.TRANSCRIPT_CLEANUP_PROMPT,
          lambda s: (lambda v: v if isinstance(v, str) else "")(
              s.get(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT)))
call_site("dictation_language", SettingsKey.SELECTED_MODEL,
          lambda s: (lambda v: "api" if isinstance(v, str) and v in old.LEGACY_API_MODELS
                     else v if isinstance(v, str) and v in config.MODEL_VALUE_MAP.values()
                     else D[SettingsKey.SELECTED_MODEL])(s.get(SettingsKey.SELECTED_MODEL)))
call_site("dictation_language", SettingsKey.WHISPER_MODEL,
          lambda s: old.setting_value(SettingsKey.WHISPER_MODEL, s))
call_site("dictation_language", SettingsKey.DICTATION_LANGUAGES,
          lambda s: (lambda v: v if isinstance(v, (list, tuple)) else [])(
              s.get(SettingsKey.DICTATION_LANGUAGES)))
call_site("dictation_language", SettingsKey.DICTATION_ACTIVE_LANGUAGE,
          lambda s: (lambda v: v if isinstance(v, str) else "")(
              s.get(SettingsKey.DICTATION_ACTIVE_LANGUAGE)))
call_site("dictation_language", SettingsKey.LOCAL_ASR_LANGUAGE,
          lambda s: (lambda v: v if isinstance(v, str) else None)(
              old.setting_value(SettingsKey.LOCAL_ASR_LANGUAGE, s)))
call_site("dictation_language", SettingsKey.DICTATION_ACTIVE_LANGUAGE,
          lambda s: s.get(SettingsKey.DICTATION_ACTIVE_LANGUAGE) not in ("", "en", "fr"),
          lambda s: SettingsKey.DICTATION_ACTIVE_LANGUAGE.stored(s) not in ("", "en", "fr"),
          result=bool)
call_site("cleanup_profiles", SettingsKey.TRANSCRIPT_CLEANUP_PROFILES,
          lambda s: s.get(SettingsKey.TRANSCRIPT_CLEANUP_PROFILES))
call_site("cleanup_profiles", SettingsKey.QUICK_RECORD_PROFILE,
          lambda s: s.get(SettingsKey.QUICK_RECORD_PROFILE) or "",
          lambda s: SettingsKey.QUICK_RECORD_PROFILE.read(s) or "")
call_site("cleanup_profiles", SettingsKey.HOTKEYS,
          lambda s: (lambda v: bool(isinstance(v, dict) and v.get("command_mode")))(
              s.get(SettingsKey.HOTKEYS)),
          lambda s: bool((SettingsKey.HOTKEYS.read(s) or {}).get("command_mode")),
          result=bool)
# What load_client_pairing goes on to read: the pairing's fields, or none.
call_site("remote_asr.settings", SettingsKey.REMOTE_ENGINE_CLIENT,
          lambda s: (lambda v: v if isinstance(v, dict) else {})(
              s.get(SettingsKey.REMOTE_ENGINE_CLIENT)),
          lambda s: SettingsKey.REMOTE_ENGINE_CLIENT.read(s) or {},
          result=dict)
for _key in (SettingsKey.REMOTE_HOST_ENABLED, SettingsKey.REMOTE_HOST_MODEL_MANAGEMENT,
             SettingsKey.REMOTE_HOST_KEEP_RECORDS, SettingsKey.REMOTE_HOST_MANAGE_MCP,
             SettingsKey.REMOTE_CLIENT_HISTORY):
    call_site("remote_asr.settings", _key, lambda s, k=_key: s.get(k) is True)
call_site("remote_asr.settings", SettingsKey.REMOTE_HOST_TAILSCALE_TRUST,
          lambda s: s.get(SettingsKey.REMOTE_HOST_TAILSCALE_TRUST) is not False)
call_site("remote_asr.settings", SettingsKey.REMOTE_RECORDS_LOCATION,
          lambda s: (lambda v: v if v in ("local", "host", "both") else "local")(
              s.get(SettingsKey.REMOTE_RECORDS_LOCATION)))


def _old_host_port(s):
    from services.remote_asr import protocol

    raw = s.get(SettingsKey.REMOTE_HOST_PORT, protocol.DEFAULT_PORT)
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return protocol.DEFAULT_PORT
    return port if 1 <= port <= 65535 else protocol.DEFAULT_PORT


call_site("remote_asr.settings", SettingsKey.REMOTE_HOST_PORT, _old_host_port)
call_site("remote_asr.settings", SettingsKey.REMOTE_HOST_DEVICES,
          lambda s: (lambda v: v if isinstance(v, list) else [])(
              s.get(SettingsKey.REMOTE_HOST_DEVICES)))


def _old_mcp_port(s):
    from services.agent_mcp.runtime import DEFAULT_PORT, _is_valid_port

    port = s.get(SettingsKey.MCP_PORT, DEFAULT_PORT)
    return port if _is_valid_port(port) else DEFAULT_PORT


for _module in ("agent_mcp.runtime", "agent_mcp.host_control", "automation.channel_bridge"):
    call_site(_module, SettingsKey.MCP_PORT, _old_mcp_port)
    call_site(_module, SettingsKey.MCP_ENABLED,
              lambda s: s.get(SettingsKey.MCP_ENABLED, False) is True)
    call_site(_module, SettingsKey.MCP_TAILSCALE_ENABLED,
              lambda s: s.get(SettingsKey.MCP_TAILSCALE_ENABLED, False) is True)
for _key in (SettingsKey.MCP_SETTINGS_ACCESS, SettingsKey.MCP_RETITLE_TRANSCRIPTIONS,
             SettingsKey.MCP_RETITLE_MEETINGS):
    call_site("agent_mcp.controls", _key, lambda s, k=_key: s.get(k) is True)
call_site("agent_mcp.controls", SettingsKey.MCP_WRITABLE_SETTINGS,
          lambda s: (lambda v: v if isinstance(v, dict) else {})(
              s.get(SettingsKey.MCP_WRITABLE_SETTINGS, {})))


def test_port_defaults_match_their_servers():
    from services.agent_mcp.runtime import DEFAULT_PORT as MCP_PORT
    from services.remote_asr.protocol import DEFAULT_PORT as REMOTE_PORT

    assert SettingsKey.MCP_PORT.default == new.MCP_DEFAULT_PORT == MCP_PORT
    assert SettingsKey.REMOTE_HOST_PORT.default == new.REMOTE_HOST_DEFAULT_PORT == REMOTE_PORT
call_site("ui_qt.dialogs.settings_models", SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT,
          lambda s: (lambda v: v if v in old.TranscriptCleanupModelSort.ALL
                     else config.TRANSCRIPT_CLEANUP_MODEL_SORT)(
              s.get(SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT,
                    D[SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT])))
for _key in (SettingsKey.CLEANUP_MODEL_MEMORY, SettingsKey.MEETING_AGENT_MODELS):
    call_site("ui_qt.dialogs.settings_models", _key,
              lambda s, k=_key: (lambda v: dict(v) if isinstance(v, dict) else {})(s.get(k)),
              lambda s, k=_key: dict(k.read(s)))
for _key in (SettingsKey.WHISPER_DEVICE, SettingsKey.WHISPER_COMPUTE_TYPE,
             SettingsKey.WHISPER_MODEL, SettingsKey.SELECTED_MODEL):
    call_site("ui_qt.dialogs.settings_models", _key,
              lambda s, k=_key: old.setting_value(k, s))


def test_config_is_where_the_defaults_come_from():
    # The same check as tests/test_setting_defaults.py, on the declarations.
    assert SettingsKey.STREAMING_CHUNK_DURATION.default == config.STREAMING_CHUNK_DURATION_SEC
    assert SettingsKey.SELECTED_MODEL.default == config.DEFAULT_BACKEND


@pytest.mark.parametrize("module", sorted(CALL_SITES) or ["(none)"])
def test_migrated_call_sites_read_what_they_read_before(module):
    for setting, before, after, result in CALL_SITES.get(module, []):
        for value in _battery(setting):
            settings = _mapping(setting, value)
            try:
                expected = before(copy.deepcopy(settings))
            except Exception:
                after(copy.deepcopy(settings))
                continue
            if not _valid(setting, result, expected):
                got = after(copy.deepcopy(settings))
                assert got == expected or _valid(setting, result, got), (module, setting, value)
                continue
            assert after(copy.deepcopy(settings)) == expected, (module, setting, value)
