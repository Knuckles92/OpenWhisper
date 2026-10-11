"""Saved webhooks, programs and text filters (Settings > Hooks).

Only Settings creates or changes these; the live API and MCP never do. Each
list keeps fixed field names (the backup scrub drops keys that look secret)
and its secrets live in the credential store.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from services.automation.catalog import known_types
from services.settings import SettingsKey

MAX_WEBHOOKS = 20
MAX_PROGRAMS = 20
MAX_FILTERS = 10
NAME_MAX_CHARS = 80
MAX_ARGS = 32
ARG_MAX_CHARS = 1000
URL_MAX_CHARS = 2000

BEFORE_CLEANUP = "before_cleanup"
AFTER_CLEANUP = "after_cleanup"
STAGES: tuple[str, ...] = (BEFORE_CLEANUP, AFTER_CLEANUP)
FILTER_HTTP = "http"
FILTER_PROGRAM = "program"
FILTER_KINDS: tuple[str, ...] = (FILTER_HTTP, FILTER_PROGRAM)

PROGRAM_TIMEOUT_DEFAULT_S = 30
PROGRAM_TIMEOUT_MAX_S = 300
FILTER_TIMEOUT_DEFAULT_MS = 1500
FILTER_TIMEOUT_MIN_MS = 100
FILTER_TIMEOUT_MAX_MS = 5000


def new_id() -> str:
    return secrets.token_hex(6)


def webhook_secret_name(hook_id: str) -> str:
    return f"OPENWHISPER_WEBHOOK_SECRET_{hook_id}"


@dataclass(frozen=True)
class Webhook:
    id: str
    name: str
    url: str
    events: tuple[str, ...] = ()
    enabled: bool = True

    def to_dict(self) -> dict:
        data = asdict(self)
        data["events"] = list(self.events)
        return data


@dataclass(frozen=True)
class Program:
    id: str
    name: str
    program: str
    args: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    enabled: bool = True
    timeout_s: int = PROGRAM_TIMEOUT_DEFAULT_S

    def to_dict(self) -> dict:
        data = asdict(self)
        data["args"] = list(self.args)
        data["events"] = list(self.events)
        return data


@dataclass(frozen=True)
class TextFilter:
    id: str
    name: str
    kind: str
    stage: str
    url: str = ""
    program: str = ""
    args: tuple[str, ...] = field(default=())
    timeout_ms: int = FILTER_TIMEOUT_DEFAULT_MS
    enabled: bool = True

    def to_dict(self) -> dict:
        data = asdict(self)
        data["args"] = list(self.args)
        return data


# -- parsing ------------------------------------------------------------------

def _text(item: Mapping, key: str) -> str:
    value = item.get(key)
    return value.strip() if isinstance(value, str) else ""


def _strings(value: Any, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str))[:limit]


def _bool(value: Any, default: bool = True) -> bool:
    return value if isinstance(value, bool) else default


def _int(value: Any, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(max(value, low), high)


def _rows(settings: Mapping, key: str) -> list:
    raw = (settings or {}).get(key)
    return raw if isinstance(raw, list) else []


def load_webhooks(settings: Mapping) -> list[Webhook]:
    hooks, seen = [], set()
    for item in _rows(settings, SettingsKey.AUTOMATION_WEBHOOKS):
        if not isinstance(item, dict):
            continue
        hook_id, name, url = _text(item, "id"), _text(item, "name"), _text(item, "url")
        if not hook_id or not name or not url or hook_id in seen:
            continue
        seen.add(hook_id)
        hooks.append(Webhook(
            hook_id, name, url,
            tuple(known_types(_strings(item.get("events"), 64))),
            _bool(item.get("enabled")),
        ))
        if len(hooks) >= MAX_WEBHOOKS:
            break
    return hooks


def load_programs(settings: Mapping) -> list[Program]:
    programs, seen = [], set()
    for item in _rows(settings, SettingsKey.AUTOMATION_PROGRAMS):
        if not isinstance(item, dict):
            continue
        program_id, name, path = _text(item, "id"), _text(item, "name"), _text(item, "program")
        if not program_id or not name or not path or program_id in seen:
            continue
        seen.add(program_id)
        programs.append(Program(
            program_id, name, path,
            _strings(item.get("args"), MAX_ARGS),
            tuple(known_types(_strings(item.get("events"), 64))),
            _bool(item.get("enabled")),
            _int(item.get("timeout_s"), PROGRAM_TIMEOUT_DEFAULT_S, 1, PROGRAM_TIMEOUT_MAX_S),
        ))
        if len(programs) >= MAX_PROGRAMS:
            break
    return programs


def load_filters(settings: Mapping) -> list[TextFilter]:
    filters, seen = [], set()
    for item in _rows(settings, SettingsKey.AUTOMATION_FILTERS):
        if not isinstance(item, dict):
            continue
        filter_id, name = _text(item, "id"), _text(item, "name")
        kind, stage = _text(item, "kind"), _text(item, "stage")
        if (not filter_id or not name or filter_id in seen
                or kind not in FILTER_KINDS or stage not in STAGES):
            continue
        url, program = _text(item, "url"), _text(item, "program")
        if (kind == FILTER_HTTP and not url) or (kind == FILTER_PROGRAM and not program):
            continue
        seen.add(filter_id)
        filters.append(TextFilter(
            filter_id, name, kind, stage, url, program,
            _strings(item.get("args"), MAX_ARGS),
            _int(item.get("timeout_ms"), FILTER_TIMEOUT_DEFAULT_MS,
                 FILTER_TIMEOUT_MIN_MS, FILTER_TIMEOUT_MAX_MS),
            _bool(item.get("enabled")),
        ))
        if len(filters) >= MAX_FILTERS:
            break
    return filters


# -- validation ---------------------------------------------------------------

def validate_url(url: str) -> None:
    """Raise ValueError unless ``url`` is an http(s) URL with a host."""
    if not url or len(url) > URL_MAX_CHARS:
        raise ValueError("Enter the address to send to.")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ValueError("That address isn't valid.") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Use an address that starts with http:// or https://.")
    if any(ch.isspace() for ch in url):
        raise ValueError("Addresses can't contain spaces.")


def url_is_private(url: str) -> bool:
    """True for loopback, link-local and private hosts (no encryption warning needed)."""
    import ipaddress

    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    if host in ("localhost",) or host.endswith((".localhost", ".local", ".lan", ".home.arpa")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _check_name(name: str, others: list, item_id: str, noun: str) -> None:
    name = name.strip()
    if not name:
        raise ValueError(f"Give the {noun} a name.")
    if len(name) > NAME_MAX_CHARS:
        raise ValueError(f"Use a name with {NAME_MAX_CHARS} characters or fewer.")
    if any(o.id != item_id and o.name.casefold() == name.casefold() for o in others):
        raise ValueError(f"A {noun} with that name already exists.")


def _check_room(others: list, item_id: str, limit: int, noun: str) -> None:
    if all(o.id != item_id for o in others) and len(others) >= limit:
        raise ValueError(f"You can keep up to {limit} {noun}s. Delete one to add another.")


def _check_events(events) -> None:
    if not known_types(events):
        raise ValueError("Choose at least one event.")


def _check_program(path: str, args) -> None:
    if not path:
        raise ValueError("Choose the program to run.")
    if not os.path.isabs(path):
        raise ValueError("Choose the program with its full path.")
    if len(args) > MAX_ARGS or any(len(a) > ARG_MAX_CHARS for a in args):
        raise ValueError(f"Use at most {MAX_ARGS} arguments of {ARG_MAX_CHARS} characters.")


def validate_webhook(hook: Webhook, settings: Mapping) -> None:
    existing = load_webhooks(settings)
    _check_name(hook.name, existing, hook.id, "webhook")
    validate_url(hook.url)
    _check_events(hook.events)
    _check_room(existing, hook.id, MAX_WEBHOOKS, "webhook")


def validate_program(program: Program, settings: Mapping) -> None:
    existing = load_programs(settings)
    _check_name(program.name, existing, program.id, "program")
    _check_program(program.program, program.args)
    _check_events(program.events)
    if not 1 <= int(program.timeout_s) <= PROGRAM_TIMEOUT_MAX_S:
        raise ValueError(f"Use a time limit from 1 to {PROGRAM_TIMEOUT_MAX_S} seconds.")
    _check_room(existing, program.id, MAX_PROGRAMS, "program")


def validate_filter(text_filter: TextFilter, settings: Mapping) -> None:
    existing = load_filters(settings)
    _check_name(text_filter.name, existing, text_filter.id, "filter")
    if text_filter.kind not in FILTER_KINDS:
        raise ValueError("Choose whether the filter is a web address or a program.")
    if text_filter.stage not in STAGES:
        raise ValueError("Choose when the filter runs.")
    if text_filter.kind == FILTER_HTTP:
        validate_url(text_filter.url)
    else:
        _check_program(text_filter.program, text_filter.args)
    if not FILTER_TIMEOUT_MIN_MS <= int(text_filter.timeout_ms) <= FILTER_TIMEOUT_MAX_MS:
        raise ValueError(
            f"Use a time limit from {FILTER_TIMEOUT_MIN_MS} to {FILTER_TIMEOUT_MAX_MS} ms."
        )
    _check_room(existing, text_filter.id, MAX_FILTERS, "filter")


# -- mutators (pass to settings_manager.mutate_settings) ----------------------

def _replace(items: list, item) -> list:
    index = next((i for i, o in enumerate(items) if o.id == item.id), len(items))
    items[index:index + 1] = [item]
    return items


def _require_id(item_id: str, noun: str) -> None:
    if not item_id or not item_id.strip():
        raise ValueError(f"This {noun} has no id.")


def save_webhook(settings: dict, hook: Webhook) -> None:
    hook = Webhook(hook.id.strip(), hook.name.strip(), hook.url.strip(),
                   tuple(known_types(hook.events)), bool(hook.enabled))
    _require_id(hook.id, "webhook")
    validate_webhook(hook, settings)
    settings[SettingsKey.AUTOMATION_WEBHOOKS] = [
        h.to_dict() for h in _replace(load_webhooks(settings), hook)
    ]


def save_program(settings: dict, program: Program) -> None:
    program = Program(program.id.strip(), program.name.strip(), program.program.strip(),
                      tuple(program.args), tuple(known_types(program.events)),
                      bool(program.enabled), int(program.timeout_s))
    _require_id(program.id, "program")
    validate_program(program, settings)
    settings[SettingsKey.AUTOMATION_PROGRAMS] = [
        p.to_dict() for p in _replace(load_programs(settings), program)
    ]


def save_filter(settings: dict, text_filter: TextFilter) -> None:
    text_filter = TextFilter(
        text_filter.id.strip(), text_filter.name.strip(), text_filter.kind,
        text_filter.stage, text_filter.url.strip(), text_filter.program.strip(),
        tuple(text_filter.args), int(text_filter.timeout_ms), bool(text_filter.enabled),
    )
    _require_id(text_filter.id, "filter")
    validate_filter(text_filter, settings)
    settings[SettingsKey.AUTOMATION_FILTERS] = [
        f.to_dict() for f in _replace(load_filters(settings), text_filter)
    ]


def delete_webhook(settings: dict, hook_id: str) -> None:
    settings[SettingsKey.AUTOMATION_WEBHOOKS] = [
        h.to_dict() for h in load_webhooks(settings) if h.id != hook_id
    ]


def delete_program(settings: dict, program_id: str) -> None:
    settings[SettingsKey.AUTOMATION_PROGRAMS] = [
        p.to_dict() for p in load_programs(settings) if p.id != program_id
    ]


def delete_filter(settings: dict, filter_id: str) -> None:
    settings[SettingsKey.AUTOMATION_FILTERS] = [
        f.to_dict() for f in load_filters(settings) if f.id != filter_id
    ]


def find_webhook(settings: Mapping, hook_id: str) -> Optional[Webhook]:
    return next((h for h in load_webhooks(settings) if h.id == hook_id), None)


def find_program(settings: Mapping, program_id: str) -> Optional[Program]:
    return next((p for p in load_programs(settings) if p.id == program_id), None)


def find_filter(settings: Mapping, filter_id: str) -> Optional[TextFilter]:
    return next((f for f in load_filters(settings) if f.id == filter_id), None)
