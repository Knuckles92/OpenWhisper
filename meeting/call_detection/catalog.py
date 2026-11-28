"""Known call applications, browsers, call websites, and candidate matching.

Pure, platform-agnostic catalog used across macOS, Windows, and Linux.
"""

from __future__ import annotations

import re
from typing import Final, Iterable, Optional
from urllib.parse import urlsplit

from meeting.call_detection.models import (
    ActiveAudioProcess,
    BrowserContext,
    CallCandidate,
    CallSource,
)

# ---------------------------------------------------------------------------
# Canonical app IDs and display labels
# ---------------------------------------------------------------------------

KNOWN_CALL_APPS: Final[dict[str, tuple[CallSource, str]]] = {
    "us.zoom.xos": (CallSource.ZOOM, "Zoom"),
    "com.microsoft.teams2": (CallSource.TEAMS, "Microsoft Teams"),
    "com.tinyspeck.slackmacgap": (CallSource.SLACK_HUDDLE, "Slack"),
    "com.hnc.Discord": (CallSource.DISCORD, "Discord"),
    "com.apple.FaceTime": (CallSource.FACETIME, "FaceTime"),
    "net.whatsapp.WhatsApp": (CallSource.WHATSAPP, "WhatsApp"),
    "com.cisco.webexmeetingsapp": (CallSource.WEBEX, "Webex"),
    "com.gather.GatherV2": (CallSource.GATHER, "Gather"),
    "org.whispersystems.signal-desktop": (CallSource.SIGNAL, "Signal"),
    "ru.keepcoder.Telegram": (CallSource.TELEGRAM, "Telegram"),
    "com.tencent.xinWeChat": (CallSource.WECHAT, "WeChat"),
    "jp.naver.line.mac": (CallSource.LINE, "LINE"),
    "im.riot.app": (CallSource.ELEMENT, "Element"),
}

KNOWN_BROWSERS: Final[dict[str, str]] = {
    "com.google.Chrome": "Google Chrome",
    "com.apple.Safari": "Safari",
    "com.microsoft.edgemac": "Microsoft Edge",
    "com.brave.Browser": "Brave",
    "org.mozilla.firefox": "Firefox",
    "company.thebrowser.Browser": "Arc",
    "com.vivaldi.Vivaldi": "Vivaldi",
    "app.zen-browser.zen": "Zen",
}

#: All known provider labels (apps + call websites) for Settings display.
KNOWN_PROVIDER_LABELS: Final[dict[str, str]] = {
    **{key: label for key, (_src, label) in KNOWN_CALL_APPS.items()},
    **KNOWN_BROWSERS,
    "meet.google.com": "Google Meet",
    "web.whatsapp.com": "WhatsApp Web",
    "app.gather.town": "Gather",
    "app.v2.gather.town": "Gather",
    "voice.google.com": "Google Voice",
    "messenger.com": "Messenger",
    "facetime.apple.com": "FaceTime (web)",
    "web.groupme.com": "GroupMe",
    "whereby.com": "Whereby",
    "meet.jit.si": "Jitsi Meet",
}

#: Common call providers offered in Settings for quick configuration.
DEFAULT_CONFIGURABLE_PROVIDERS: Final[tuple[tuple[str, str], ...]] = (
    ("us.zoom.xos", "Zoom"),
    ("com.microsoft.teams2", "Microsoft Teams"),
    ("meet.google.com", "Google Meet"),
    ("com.tinyspeck.slackmacgap", "Slack"),
    ("com.hnc.Discord", "Discord"),
    ("com.apple.FaceTime", "FaceTime"),
    ("com.cisco.webexmeetingsapp", "Webex"),
    ("net.whatsapp.WhatsApp", "WhatsApp"),
)

# ---------------------------------------------------------------------------
# Cross-platform identifier normalization (macOS bundle IDs, Windows EXEs,
# Linux PipeWire binaries & helper processes)
# ---------------------------------------------------------------------------

# Lowercased exact matches or prefixes -> canonical ID.
_EXACT_ALIASES: Final[dict[str, str]] = {
    # Zoom
    "us.zoom.xos": "us.zoom.xos",
    "us.zoom.videomeetings": "us.zoom.xos",
    "zoom": "us.zoom.xos",
    "zoom.exe": "us.zoom.xos",
    "zoom.us": "us.zoom.xos",
    "cpthost.exe": "us.zoom.xos",
    # Microsoft Teams
    "com.microsoft.teams2": "com.microsoft.teams2",
    "com.microsoft.teams": "com.microsoft.teams2",
    "ms-teams": "com.microsoft.teams2",
    "ms-teams.exe": "com.microsoft.teams2",
    "teams": "com.microsoft.teams2",
    "teams.exe": "com.microsoft.teams2",
    "teams-for-linux": "com.microsoft.teams2",
    # Slack
    "com.tinyspeck.slackmacgap": "com.tinyspeck.slackmacgap",
    "slack": "com.tinyspeck.slackmacgap",
    "slack.exe": "com.tinyspeck.slackmacgap",
    # Discord
    "com.hnc.discord": "com.hnc.Discord",
    "discord": "com.hnc.Discord",
    "discord.exe": "com.hnc.Discord",
    "discordptb": "com.hnc.Discord",
    "discordptb.exe": "com.hnc.Discord",
    "discordcanary": "com.hnc.Discord",
    "discordcanary.exe": "com.hnc.Discord",
    "vesktop": "com.hnc.Discord",
    "vesktop.exe": "com.hnc.Discord",
    # FaceTime
    "com.apple.facetime": "com.apple.FaceTime",
    "com.apple.avconferenced": "com.apple.FaceTime",
    # WhatsApp
    "net.whatsapp.whatsapp": "net.whatsapp.WhatsApp",
    "whatsapp": "net.whatsapp.WhatsApp",
    "whatsapp.exe": "net.whatsapp.WhatsApp",
    "whatsapp.root.exe": "net.whatsapp.WhatsApp",
    # Webex
    "com.cisco.webexmeetingsapp": "com.cisco.webexmeetingsapp",
    "com.cisco.webex.meetings": "com.cisco.webexmeetingsapp",
    "cisco-webex-meetings": "com.cisco.webexmeetingsapp",
    "ciscocollabhost.exe": "com.cisco.webexmeetingsapp",
    "webex": "com.cisco.webexmeetingsapp",
    "webex.exe": "com.cisco.webexmeetingsapp",
    "atmgr.exe": "com.cisco.webexmeetingsapp",
    # Gather
    "com.gather.gatherv2": "com.gather.GatherV2",
    "com.gather.gather": "com.gather.GatherV2",
    "gather": "com.gather.GatherV2",
    "gather.exe": "com.gather.GatherV2",
    # Signal
    "org.whispersystems.signal-desktop": "org.whispersystems.signal-desktop",
    "signal": "org.whispersystems.signal-desktop",
    "signal.exe": "org.whispersystems.signal-desktop",
    "signal-desktop": "org.whispersystems.signal-desktop",
    # Telegram
    "ru.keepcoder.telegram": "ru.keepcoder.Telegram",
    "org.telegram.desktop": "ru.keepcoder.Telegram",
    "telegram": "ru.keepcoder.Telegram",
    "telegram.exe": "ru.keepcoder.Telegram",
    "telegram-desktop": "ru.keepcoder.Telegram",
    # WeChat
    "com.tencent.xinwechat": "com.tencent.xinWeChat",
    "wechat": "com.tencent.xinWeChat",
    "wechat.exe": "com.tencent.xinWeChat",
    # LINE
    "jp.naver.line.mac": "jp.naver.line.mac",
    "line": "jp.naver.line.mac",
    "line.exe": "jp.naver.line.mac",
    # Element
    "im.riot.app": "im.riot.app",
    "element": "im.riot.app",
    "element.exe": "im.riot.app",
    "element-desktop": "im.riot.app",
    # Browsers: Chrome
    "com.google.chrome": "com.google.Chrome",
    "com.google.chrome.canary": "com.google.Chrome",
    "com.google.chrome.beta": "com.google.Chrome",
    "org.chromium.chromium": "com.google.Chrome",
    "chrome": "com.google.Chrome",
    "chrome.exe": "com.google.Chrome",
    "google-chrome": "com.google.Chrome",
    "google-chrome-stable": "com.google.Chrome",
    "chromium": "com.google.Chrome",
    "chromium-browser": "com.google.Chrome",
    # Safari
    "com.apple.safari": "com.apple.Safari",
    "com.apple.safaritechnologypreview": "com.apple.Safari",
    # Edge
    "com.microsoft.edgemac": "com.microsoft.edgemac",
    "com.microsoft.edgemac.beta": "com.microsoft.edgemac",
    "msedge": "com.microsoft.edgemac",
    "msedge.exe": "com.microsoft.edgemac",
    "microsoft-edge": "com.microsoft.edgemac",
    "microsoft-edge-stable": "com.microsoft.edgemac",
    # Brave
    "com.brave.browser": "com.brave.Browser",
    "brave": "com.brave.Browser",
    "brave.exe": "com.brave.Browser",
    "brave-browser": "com.brave.Browser",
    # Firefox
    "org.mozilla.firefox": "org.mozilla.firefox",
    "org.mozilla.firefoxdeveloperedition": "org.mozilla.firefox",
    "org.mozilla.nightly": "org.mozilla.firefox",
    "firefox": "org.mozilla.firefox",
    "firefox.exe": "org.mozilla.firefox",
    "firefox-bin": "org.mozilla.firefox",
    "firefox-esr": "org.mozilla.firefox",
    # Arc
    "company.thebrowser.browser": "company.thebrowser.Browser",
    "arc": "company.thebrowser.Browser",
    "arc.exe": "company.thebrowser.Browser",
    # Vivaldi
    "com.vivaldi.vivaldi": "com.vivaldi.Vivaldi",
    "vivaldi": "com.vivaldi.Vivaldi",
    "vivaldi.exe": "com.vivaldi.Vivaldi",
    "vivaldi-bin": "com.vivaldi.Vivaldi",
    # Zen
    "app.zen-browser.zen": "app.zen-browser.zen",
    "zen": "app.zen-browser.zen",
    "zen-bin": "app.zen-browser.zen",
    "zen.exe": "app.zen-browser.zen",
}

# Helper bundle ID prefixes on macOS (e.g. "com.google.chrome.helper").
_HELPER_PREFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("us.zoom.xos.", "us.zoom.xos"),
    ("com.microsoft.teams2.", "com.microsoft.teams2"),
    ("com.microsoft.teams.", "com.microsoft.teams2"),
    ("com.tinyspeck.slackmacgap.", "com.tinyspeck.slackmacgap"),
    ("com.hnc.discord.", "com.hnc.Discord"),
    ("net.whatsapp.whatsapp.", "net.whatsapp.WhatsApp"),
    ("com.cisco.webexmeetingsapp.", "com.cisco.webexmeetingsapp"),
    ("com.gather.gatherv2.", "com.gather.GatherV2"),
    ("org.whispersystems.signal-desktop.", "org.whispersystems.signal-desktop"),
    ("ru.keepcoder.telegram.", "ru.keepcoder.Telegram"),
    ("com.google.chrome.", "com.google.Chrome"),
    ("org.chromium.chromium.", "com.google.Chrome"),
    ("com.microsoft.edgemac.", "com.microsoft.edgemac"),
    ("com.brave.browser.", "com.brave.Browser"),
    ("org.mozilla.firefox.", "org.mozilla.firefox"),
    ("company.thebrowser.browser.", "company.thebrowser.Browser"),
    ("com.vivaldi.vivaldi.", "com.vivaldi.Vivaldi"),
    ("app.zen-browser.zen.", "app.zen-browser.zen"),
)

# Exact call sites: host -> CallSource.
CALL_SITES: Final[dict[str, CallSource]] = {
    "teams.microsoft.com": CallSource.TEAMS,
    "teams.cloud.microsoft": CallSource.TEAMS,
    "teams.live.com": CallSource.TEAMS,
    "web.whatsapp.com": CallSource.WHATSAPP,
    "app.gather.town": CallSource.GATHER,
    "app.v2.gather.town": CallSource.GATHER,
    "voice.google.com": CallSource.GOOGLE_VOICE,
    "messenger.com": CallSource.MESSENGER,
    "www.messenger.com": CallSource.MESSENGER,
    "facetime.apple.com": CallSource.FACETIME,
    "web.groupme.com": CallSource.GROUPME,
    "whereby.com": CallSource.WHEREBY,
    "meet.jit.si": CallSource.JITSI,
}

_GOOGLE_MEET_SITE: Final[str] = "meet.google.com"
_MEET_CODE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$", re.IGNORECASE
)
_MEET_TITLE_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^Meet\s*[-–—]\s*(.+)$", re.IGNORECASE
)
_BROWSER_TITLE_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"\s*[-–—]\s*(?:Google Chrome|Brave|Microsoft Edge|Safari|Mozilla Firefox|Firefox|Arc|Vivaldi|Zen Browser|Zen)\s*$",
    re.IGNORECASE,
)


def canonical_app_id(
    bundle_id: Optional[str],
    name: Optional[str] = None,
) -> Optional[str]:
    """Map an OS bundle ID, executable name, or helper ID to a canonical ID."""
    raw = (bundle_id or "").strip().lower()
    raw_name = (name or "").strip().lower()

    if raw:
        # Safari plays/records via com.apple.WebKit.GPU on macOS.
        if raw == "com.apple.webkit.gpu":
            if "safari" in raw_name:
                return "com.apple.Safari"
            return None

        if raw in _EXACT_ALIASES:
            return _EXACT_ALIASES[raw]

        for prefix, canonical in _HELPER_PREFIXES:
            if raw.startswith(prefix):
                return canonical

    if raw_name and raw_name in _EXACT_ALIASES:
        return _EXACT_ALIASES[raw_name]

    return None


def is_browser_id(canonical_id: Optional[str]) -> bool:
    return bool(canonical_id and canonical_id in KNOWN_BROWSERS)


def is_native_call_app_id(canonical_id: Optional[str]) -> bool:
    return bool(canonical_id and canonical_id in KNOWN_CALL_APPS)


def is_process_of(
    bundle_id: Optional[str],
    target_canonical_id: str,
    name: Optional[str] = None,
) -> bool:
    """Whether a process with ``bundle_id``/``name`` belongs to ``target_canonical_id``."""
    return canonical_app_id(bundle_id, name) == target_canonical_id


def known_provider_label(key: str) -> Optional[str]:
    if key in KNOWN_PROVIDER_LABELS:
        return KNOWN_PROVIDER_LABELS[key]
    if _is_webex_host(key):
        return CallSource.WEBEX.label
    if _is_zoom_host(key):
        return CallSource.ZOOM.label
    return None


def provider_key_for_site(host: str) -> str:
    """Map a call website host to its canonical provider key.

    Teams web and Zoom web share their desktop app's provider key so one
    policy covers both desktop and browser calls for that service.
    """
    normalized = host.strip().lower().removeprefix("www.")
    if normalized in ("teams.microsoft.com", "teams.cloud.microsoft", "teams.live.com"):
        return "com.microsoft.teams2"
    if _is_zoom_host(normalized):
        return "us.zoom.xos"
    if _is_webex_host(normalized):
        return "com.cisco.webexmeetingsapp"
    return normalized


def deduplicate_processes(
    processes: Iterable[ActiveAudioProcess],
) -> list[ActiveAudioProcess]:
    """Deduplicate active audio processes by canonical app ID (or raw bundle_id)."""
    seen: dict[str, ActiveAudioProcess] = {}
    for proc in processes:
        key = canonical_app_id(proc.bundle_id, proc.name) or proc.bundle_id.lower()
        if key not in seen:
            seen[key] = proc
    return sorted(
        seen.values(),
        key=lambda p: canonical_app_id(p.bundle_id, p.name) or p.bundle_id.lower(),
    )


# ---------------------------------------------------------------------------
# Browser URL and window-title matching
# ---------------------------------------------------------------------------

def _is_webex_host(host: str) -> bool:
    h = host.strip().lower()
    return h == "webex.com" or h.endswith(".webex.com")


def _is_zoom_host(host: str) -> bool:
    h = host.strip().lower()
    return h == "zoom.us" or h.endswith(".zoom.us")


def _extract_host(url: str) -> Optional[str]:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").strip().lower()
    return host or None


def _google_meet_url_code(url: str) -> Optional[str]:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    if (parsed.hostname or "").lower() != _GOOGLE_MEET_SITE:
        return None
    segments = [seg for seg in parsed.path.split("/") if seg]
    if not segments:
        return None
    code = segments[0].lower()
    if _MEET_CODE_RE.match(code):
        return code
    return None


def _strip_browser_suffix(title: str) -> str:
    return _BROWSER_TITLE_SUFFIX_RE.sub("", title.strip()).strip()


def _parse_google_meet_title(title: str) -> Optional[tuple[str, bool, Optional[str]]]:
    """Parse a Google Meet tab/window title like ``Meet - Design sync``.

    Returns ``(clean_title, is_named, optional_code)`` or ``None`` if the title
    does not look like an active Google Meet room.
    """
    cleaned = _strip_browser_suffix(title)
    match = _MEET_TITLE_PREFIX_RE.match(cleaned)
    if not match:
        return None
    remainder = match.group(1).strip()
    if not remainder:
        return None
    if _MEET_CODE_RE.match(remainder):
        return (CallSource.GOOGLE_MEET.label, False, remainder.lower())
    return (remainder, True, None)


def _match_browser_title(
    title: str,
    browser_id: str,
    browser_name: str,
) -> Optional[CallCandidate]:
    """Identify a known web call from the browser window title alone."""
    cleaned = _strip_browser_suffix(title)
    if not cleaned:
        return None
    lower = cleaned.lower()

    meet_parsed = _parse_google_meet_title(cleaned)
    if meet_parsed is not None:
        meet_title, is_named, code = meet_parsed
        return CallCandidate(
            key=f"google_meet:{code or cleaned.lower()}",
            title=meet_title,
            source=CallSource.GOOGLE_MEET,
            application=browser_id,
            application_name=browser_name,
            site=_GOOGLE_MEET_SITE,
            named=is_named,
            call_code=code,
        )

    if "microsoft teams" in lower or lower.endswith("| teams"):
        return CallCandidate(
            key="teams:web",
            title=CallSource.TEAMS.label,
            source=CallSource.TEAMS,
            application=browser_id,
            application_name=browser_name,
            site="teams.microsoft.com",
            named=False,
        )

    if "zoom" in lower and ("meeting" in lower or "webinar" in lower):
        return CallCandidate(
            key="zoom:web",
            title=CallSource.ZOOM.label,
            source=CallSource.ZOOM,
            application=browser_id,
            application_name=browser_name,
            site="zoom.us",
            named=False,
        )

    if "webex" in lower and ("meeting" in lower or "call" in lower):
        return CallCandidate(
            key="webex:web",
            title=CallSource.WEBEX.label,
            source=CallSource.WEBEX,
            application=browser_id,
            application_name=browser_name,
            site="webex.com",
            named=False,
        )

    if "jitsi meet" in lower:
        return CallCandidate(
            key="jitsi:web",
            title=CallSource.JITSI.label,
            source=CallSource.JITSI,
            application=browser_id,
            application_name=browser_name,
            site="meet.jit.si",
            named=False,
        )

    return None


def candidate_from_microphone(
    process: ActiveAudioProcess,
    browser_context: Optional[BrowserContext] = None,
) -> Optional[CallCandidate]:
    """Convert an active microphone process (and optional browser context)
    into a :class:`CallCandidate`, or ``None`` if it is not a supported call app.
    """
    canon = canonical_app_id(process.bundle_id, process.name)
    if canon is None:
        return None

    # 1. Native call app
    if canon in KNOWN_CALL_APPS:
        source, app_name = KNOWN_CALL_APPS[canon]
        return CallCandidate(
            key=f"{source.value}:{canon}",
            title=source.label,
            source=source,
            application=canon,
            application_name=app_name,
            site=None,
            named=False,
        )

    # 2. Browser holding the microphone
    if canon in KNOWN_BROWSERS:
        browser_name = KNOWN_BROWSERS[canon]
        ctx = browser_context or BrowserContext()

        if ctx.url:
            host = _extract_host(ctx.url)
            if host == _GOOGLE_MEET_SITE:
                code = _google_meet_url_code(ctx.url)
                if code is not None:
                    meet_parsed = _parse_google_meet_title(ctx.title)
                    if meet_parsed is not None and meet_parsed[1]:
                        title, named = meet_parsed[0], True
                    else:
                        title, named = CallSource.GOOGLE_MEET.label, False
                    return CallCandidate(
                        key=f"google_meet:{code}",
                        title=title,
                        source=CallSource.GOOGLE_MEET,
                        application=canon,
                        application_name=browser_name,
                        site=_GOOGLE_MEET_SITE,
                        named=named,
                        call_code=code,
                    )
            elif host:
                normalized_host = host.removeprefix("www.")
                if host in CALL_SITES:
                    source = CALL_SITES[host]
                    return CallCandidate(
                        key=f"{source.value}:{normalized_host}",
                        title=source.label,
                        source=source,
                        application=canon,
                        application_name=browser_name,
                        site=normalized_host,
                        named=False,
                    )
                if _is_zoom_host(host):
                    return CallCandidate(
                        key=f"zoom:{normalized_host}",
                        title=CallSource.ZOOM.label,
                        source=CallSource.ZOOM,
                        application=canon,
                        application_name=browser_name,
                        site=normalized_host,
                        named=False,
                    )
                if _is_webex_host(host):
                    return CallCandidate(
                        key=f"webex:{normalized_host}",
                        title=CallSource.WEBEX.label,
                        source=CallSource.WEBEX,
                        application=canon,
                        application_name=browser_name,
                        site=normalized_host,
                        named=False,
                    )
                # Known host, generic browser call
                return CallCandidate(
                    key=f"browser_call:{canon}:{normalized_host}",
                    title=normalized_host,
                    source=CallSource.BROWSER_CALL,
                    application=canon,
                    application_name=browser_name,
                    site=normalized_host,
                    named=False,
                )

        # No URL (e.g., Automation off or Windows/Linux title-only inspection)
        if ctx.title:
            matched = _match_browser_title(ctx.title, canon, browser_name)
            if matched is not None:
                return matched

        return CallCandidate(
            key=f"browser_call:{canon}",
            title=browser_name,
            source=CallSource.BROWSER_CALL,
            application=canon,
            application_name=browser_name,
            site=None,
            named=False,
        )

    return None


def pick_primary_process(
    processes: Iterable[ActiveAudioProcess],
) -> Optional[ActiveAudioProcess]:
    """Choose the best supported microphone process (native call apps beat browsers)."""
    supported = [
        proc
        for proc in deduplicate_processes(processes)
        if canonical_app_id(proc.bundle_id, proc.name) is not None
    ]
    if not supported:
        return None
    supported.sort(
        key=lambda p: (
            1 if is_browser_id(canonical_app_id(p.bundle_id, p.name)) else 0,
            canonical_app_id(p.bundle_id, p.name) or "",
        )
    )
    return supported[0]


def only_native_call_app(
    processes: Iterable[ActiveAudioProcess],
) -> Optional[tuple[str, str]]:
    """Return ``(canonical_id, label)`` if exactly one native call app holds the mic.

    Used to link a manually-started Meeting Mode recording to an active native
    call app for auto-stop. Returns ``None`` if zero or multiple distinct native
    call apps are active (browsers are ignored here).
    """
    found: Optional[tuple[str, str]] = None
    for proc in deduplicate_processes(processes):
        canon = canonical_app_id(proc.bundle_id, proc.name)
        if canon is None or is_browser_id(canon):
            continue
        entry = KNOWN_CALL_APPS.get(canon)
        if entry is None:
            continue
        source, _ = entry
        if found is not None and found[0] != canon:
            return None
        found = (canon, source.label)
    return found
