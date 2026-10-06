"""What OpenWhisper knows about apps: their names, kinds of writing and sites.

Apps are keyed by Windows executable name, macOS bundle id or Linux window
class, all compared case-insensitively. A browser's window title is read only
to name the site it shows (Gmail, Slack, Google Docs...); the title itself is
never kept, logged or returned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Iterable, Mapping, Optional

# Same values as services.app_styles.AppCategory and Surface, which import
# this module (focus_context must not import app_styles).
EMAIL: Final[str] = "email"
WORK: Final[str] = "work"
PERSONAL: Final[str] = "personal"
OTHER: Final[str] = "other"
CATEGORIES: Final[tuple[str, ...]] = (EMAIL, WORK, PERSONAL, OTHER)

TEXT: Final[str] = "text"
CODE: Final[str] = "code"
TERMINAL: Final[str] = "terminal"

MAX_OVERRIDES: Final[int] = 100
MAX_MATCH_CHARS: Final[int] = 120


@dataclass(frozen=True)
class KnownApp:
    name: str
    category: str = OTHER
    surface: str = TEXT
    ids: tuple[str, ...] = ()
    browser: bool = False


@dataclass(frozen=True)
class KnownSite:
    name: str
    category: str = OTHER
    #: Other ways a title names the site, compared case-insensitively.
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppKind:
    """Where a dictation goes, as far as styles and prompts care."""
    category: str
    surface: str
    #: The site when a browser shows a known one, else the app's name.
    name: str


def _apps(category: str, surface: str, *entries) -> tuple[KnownApp, ...]:
    """Entries are ``(name, ids)``, or ``(name, ids, True)`` for a browser."""
    return tuple(
        KnownApp(entry[0], category, surface, entry[1], len(entry) > 2 and entry[2])
        for entry in entries
    )


_APPS: Final[tuple[KnownApp, ...]] = (
    *_apps(EMAIL, TEXT,
        ("Outlook", ("outlook.exe", "olk.exe", "com.microsoft.outlook")),
        ("Mail", ("hxoutlook.exe", "com.apple.mail")),
        ("Thunderbird", ("thunderbird.exe", "thunderbird", "org.mozilla.thunderbird",
                         "net.thunderbird.thunderbird")),
        ("Spark", ("spark.exe", "spark desktop.exe", "com.readdle.smartemail-mac",
                   "com.readdle.spark-desktop")),
        ("Mailspring", ("mailspring.exe", "mailspring", "com.mailspring.mailspring")),
        ("eM Client", ("mailclient.exe",)),
        ("Superhuman", ("superhuman.exe", "com.superhuman.electron")),
        ("Airmail", ("it.bloop.airmail2",)),
        ("Geary", ("geary", "org.gnome.geary")),
        ("Evolution", ("evolution", "org.gnome.evolution")),
    ),
    *_apps(WORK, TEXT,
        ("Slack", ("slack.exe", "slack", "com.tinyspeck.slackmacgap")),
        ("Teams", ("ms-teams.exe", "teams.exe", "com.microsoft.teams2",
                   "com.microsoft.teams", "teams-for-linux")),
        ("Zoom", ("zoom.exe", "zoom", "us.zoom.xos")),
        ("Mattermost", ("mattermost.exe", "mattermost", "com.mattermost.desktop")),
        ("Webex", ("webex.exe", "ciscocollabhost.exe", "com.cisco.webexmeetingsapp")),
        ("Zulip", ("zulip.exe", "zulip", "org.zulip.zulip-electron")),
        ("Rocket.Chat", ("rocket.chat.exe", "rocket.chat", "chat.rocket")),
    ),
    *_apps(PERSONAL, TEXT,
        ("WhatsApp", ("whatsapp.exe", "whatsapp.root.exe", "net.whatsapp.whatsapp",
                      "whatsapp")),
        ("Telegram", ("telegram.exe", "ru.keepcoder.telegram", "org.telegram.desktop",
                      "telegramdesktop", "telegram-desktop")),
        ("Signal", ("signal.exe", "signal", "org.whispersystems.signal-desktop")),
        ("Messenger", ("messenger.exe", "com.facebook.archon")),
        ("Discord", ("discord.exe", "discord", "com.hnc.discord")),
        ("Messages", ("com.apple.mobilesms",)),
        ("Skype", ("skype.exe", "com.skype.skype", "skype")),
        ("Viber", ("viber.exe", "com.viber.osx", "viber")),
    ),
    *_apps(OTHER, CODE,
        ("VS Code", ("code.exe", "code", "code-oss", "com.microsoft.vscode")),
        ("VSCodium", ("vscodium.exe", "vscodium", "com.vscodium")),
        ("Cursor", ("cursor.exe", "cursor", "com.todesktop.230313mzl4w4u92")),
        ("Windsurf", ("windsurf.exe", "windsurf", "com.exafunction.windsurf")),
        ("Visual Studio", ("devenv.exe",)),
        ("IntelliJ IDEA", ("idea64.exe", "idea.exe", "jetbrains-idea", "jetbrains-idea-ce",
                           "com.jetbrains.intellij", "com.jetbrains.intellij.ce")),
        ("PyCharm", ("pycharm64.exe", "pycharm.exe", "jetbrains-pycharm",
                     "jetbrains-pycharm-ce", "com.jetbrains.pycharm",
                     "com.jetbrains.pycharm.ce")),
        ("WebStorm", ("webstorm64.exe", "jetbrains-webstorm", "com.jetbrains.webstorm")),
        ("CLion", ("clion64.exe", "jetbrains-clion", "com.jetbrains.clion")),
        ("Rider", ("rider64.exe", "jetbrains-rider", "com.jetbrains.rider")),
        ("GoLand", ("goland64.exe", "jetbrains-goland", "com.jetbrains.goland")),
        ("PhpStorm", ("phpstorm64.exe", "jetbrains-phpstorm", "com.jetbrains.phpstorm")),
        ("RubyMine", ("rubymine64.exe", "jetbrains-rubymine", "com.jetbrains.rubymine")),
        ("RustRover", ("rustrover64.exe", "jetbrains-rustrover", "com.jetbrains.rustrover")),
        ("DataGrip", ("datagrip64.exe", "jetbrains-datagrip", "com.jetbrains.datagrip")),
        ("Android Studio", ("studio64.exe", "jetbrains-studio", "com.google.android.studio")),
        ("Xcode", ("com.apple.dt.xcode",)),
        ("Sublime Text", ("sublime_text.exe", "sublime_text", "com.sublimetext.4",
                          "com.sublimetext.3")),
        ("Zed", ("zed.exe", "zed", "dev.zed.zed")),
        ("Neovim", ("nvim-qt.exe", "nvim-qt", "neovide.exe", "neovide",
                    "com.neovide.neovide")),
        ("Vim", ("gvim.exe", "gvim", "org.vim.macvim")),
        ("Emacs", ("emacs.exe", "emacs", "org.gnu.emacs")),
        ("Notepad++", ("notepad++.exe",)),
        ("PowerShell ISE", ("powershell_ise.exe",)),
    ),
    *_apps(OTHER, TERMINAL,
        ("Windows Terminal", ("windowsterminal.exe", "openconsole.exe")),
        ("Console", ("conhost.exe", "cmd.exe")),
        ("PowerShell", ("powershell.exe", "pwsh.exe")),
        ("Git Bash", ("mintty.exe",)),
        ("WezTerm", ("wezterm-gui.exe", "wezterm", "org.wezfurlong.wezterm",
                     "com.github.wez.wezterm")),
        ("Alacritty", ("alacritty.exe", "alacritty", "org.alacritty")),
        ("kitty", ("kitty", "net.kovidgoyal.kitty")),
        ("Ghostty", ("ghostty", "com.mitchellh.ghostty")),
        ("foot", ("foot", "footclient")),
        ("iTerm2", ("com.googlecode.iterm2",)),
        ("Terminal", ("com.apple.terminal", "gnome-terminal-server", "org.gnome.terminal",
                      "org.gnome.console", "kgx", "xterm", "urxvt", "xfce4-terminal")),
        ("Konsole", ("konsole", "org.kde.konsole")),
        ("Warp", ("warp.exe", "dev.warp.warp-stable", "dev.warp.warp")),
        ("Tabby", ("tabby.exe", "org.tabby")),
        ("Terminator", ("terminator",)),
        ("Tilix", ("tilix", "com.gexperts.tilix")),
    ),
    *_apps(OTHER, TEXT,
        ("Chrome", ("chrome.exe", "google-chrome", "com.google.chrome"), True),
        ("Edge", ("msedge.exe", "microsoft-edge", "com.microsoft.edgemac"), True),
        ("Firefox", ("firefox.exe", "firefox", "org.mozilla.firefox"), True),
        ("Brave", ("brave.exe", "brave-browser", "com.brave.browser"), True),
        ("Arc", ("arc.exe", "company.thebrowser.browser"), True),
        ("Opera", ("opera.exe", "opera", "com.operasoftware.opera"), True),
        ("Vivaldi", ("vivaldi.exe", "vivaldi-stable", "com.vivaldi.vivaldi"), True),
        ("Chromium", ("chromium", "chromium-browser", "org.chromium.chromium"), True),
        ("Zen", ("zen.exe", "zen", "app.zen-browser.zen"), True),
        ("LibreWolf", ("librewolf.exe", "librewolf"), True),
        ("Safari", ("com.apple.safari",), True),
        ("Word", ("winword.exe", "com.microsoft.word")),
        ("PowerPoint", ("powerpnt.exe", "com.microsoft.powerpoint")),
        ("Excel", ("excel.exe", "com.microsoft.excel")),
        ("OneNote", ("onenote.exe", "com.microsoft.onenote.mac")),
        ("Notion", ("notion.exe", "notion.id", "notion")),
        ("Obsidian", ("obsidian.exe", "obsidian", "md.obsidian")),
        ("Notepad", ("notepad.exe",)),
        ("Notes", ("com.apple.notes",)),
        ("Pages", ("com.apple.iwork.pages",)),
        ("LibreOffice", ("soffice.bin", "soffice.exe", "libreoffice-writer")),
        ("Linear", ("linear.exe", "com.linear")),
        ("ChatGPT", ("chatgpt.exe", "com.openai.chat")),
        ("Claude", ("claude.exe", "com.anthropic.claudefordesktop")),
    ),
)

_SITES: Final[tuple[KnownSite, ...]] = (
    KnownSite("Gmail", EMAIL),
    KnownSite("Outlook", EMAIL, ("outlook.com", "microsoft outlook", "outlook web app")),
    KnownSite("Yahoo Mail", EMAIL),
    KnownSite("Proton Mail", EMAIL, ("protonmail",)),
    KnownSite("Fastmail", EMAIL),
    KnownSite("iCloud Mail", EMAIL),
    KnownSite("Zoho Mail", EMAIL),
    KnownSite("Superhuman", EMAIL),
    KnownSite("Slack", WORK),
    KnownSite("Teams", WORK, ("microsoft teams",)),
    KnownSite("Google Chat", WORK),
    KnownSite("Mattermost", WORK),
    KnownSite("Webex", WORK),
    KnownSite("Zulip", WORK),
    KnownSite("LinkedIn", WORK),
    KnownSite("WhatsApp", PERSONAL, ("whatsapp web",)),
    KnownSite("Messenger", PERSONAL),
    KnownSite("Telegram", PERSONAL, ("telegram web",)),
    KnownSite("Signal", PERSONAL),
    KnownSite("Discord", PERSONAL),
    KnownSite("Instagram", PERSONAL),
    KnownSite("Google Docs", OTHER),
    KnownSite("Google Sheets", OTHER),
    KnownSite("Google Slides", OTHER),
    KnownSite("Notion", OTHER),
    KnownSite("Jira", OTHER),
    KnownSite("Confluence", OTHER),
    KnownSite("Linear", OTHER),
    KnownSite("GitHub", OTHER),
    KnownSite("GitLab", OTHER),
    KnownSite("ChatGPT", OTHER),
    KnownSite("Claude", OTHER),
)

_BY_ID: Final[dict[str, KnownApp]] = {
    app_id: app for app in _APPS for app_id in app.ids
}
_SITE_BY_KEY: Final[dict[str, KnownSite]] = {
    key: site
    for site in _SITES
    for key in (site.name.casefold(), *site.aliases)
}

#: Shown on the Styles page as each category's examples, in this order.
FEATURED: Final[dict[str, tuple[str, ...]]] = {
    EMAIL: ("Outlook", "Gmail", "Mail", "Thunderbird"),
    WORK: ("Slack", "Teams", "Google Chat", "Zoom"),
    PERSONAL: ("WhatsApp", "Messages", "Telegram", "Discord"),
    OTHER: ("Word", "Notion", "Google Docs", "VS Code"),
}

# Edge puts a zero-width space in its own name, and other titles carry
# direction marks; none of them is ever part of a site's name.
_INVISIBLE = re.compile("[​‌‍‎‏⁠﻿]")
_SEPARATORS = re.compile(r"\s+[-–—|·/]\s+")
_COUNTER = re.compile(r"^(?:[●•*]\s*)?\(\d[\d,.]*\+?\)\s*|^[●•*]\s*")
_MORE_PAGES = re.compile(r"\s+and \d+ more pages?$", re.IGNORECASE)
_TITLE_LIMIT = 512
_SUFFIXES = (".exe", ".app", ".bin")


def lookup(app_id: str) -> Optional[KnownApp]:
    """The built-in entry for an exe name, bundle id or window class, if any."""
    return _BY_ID.get((app_id or "").strip().casefold())


def site(name: str) -> Optional[KnownSite]:
    return _SITE_BY_KEY.get((name or "").strip().casefold())


def is_browser(app_id: str) -> bool:
    app = lookup(app_id)
    return app is not None and app.browser


def pretty_name(app_id: str) -> str:
    """A readable name for an app the catalogue doesn't know."""
    name = (app_id or "").strip()
    lowered = name.casefold()
    for suffix in _SUFFIXES:
        if lowered.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if "." in name and " " not in name:
        # A reverse-DNS id: com.example.FooBar names the app last.
        name = name.rsplit(".", 1)[-1]
    name = re.sub(r"[_-]+", " ", name).strip()
    if name and name == name.lower():
        name = " ".join(word[:1].upper() + word[1:] for word in name.split())
    return name or (app_id or "")


def app_name(app_id: str, fallback: str = "") -> str:
    """The catalogue's name for ``app_id``, else ``fallback``, else a tidy id."""
    app = lookup(app_id)
    if app is not None:
        return app.name
    return (fallback or "").strip() or pretty_name(app_id)


def site_from_title(title: str) -> str:
    """The known site or product a browser title names, or ""."""
    if not title:
        return ""
    cleaned = _INVISIBLE.sub("", title[:_TITLE_LIMIT])
    # The page's own name comes first and the browser and profile last; the
    # site is usually just before them, so the last known segment wins.
    for segment in reversed(_SEPARATORS.split(cleaned)):
        key = _MORE_PAGES.sub("", _COUNTER.sub("", segment.strip())).strip().casefold()
        known = _SITE_BY_KEY.get(key)
        if known is not None:
            return known.name
    return ""


def title_hint(app_id: str, title: str) -> str:
    """The site a browser window shows, from its title; "" for other apps."""
    return site_from_title(title) if is_browser(app_id) else ""


def _stem(app_id: str) -> str:
    lowered = (app_id or "").strip().casefold()
    for suffix in _SUFFIXES:
        if lowered.endswith(suffix):
            return lowered[: -len(suffix)]
    return lowered


def app_keys(identity) -> set[str]:
    """Every name an app-level override or exclusion may use for ``identity``."""
    app_id = getattr(identity, "app_id", "") or ""
    keys = {
        app_id.strip().casefold(),
        _stem(app_id),
        (getattr(identity, "name", "") or "").strip().casefold(),
        app_name(app_id).casefold(),
    }
    keys.discard("")
    return keys


def site_key(identity) -> str:
    return (getattr(identity, "title_hint", "") or "").strip().casefold()


def matches(identity, value: str) -> bool:
    """Whether a user-entered app or site name refers to ``identity``."""
    key = (value or "").strip().casefold()
    if not key or identity is None:
        return False
    return key == site_key(identity) or key in app_keys(identity)


def parse_overrides(raw) -> tuple[tuple[str, str], ...]:
    """Valid ``(match, category)`` pairs from stored overrides, first one wins."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, Mapping):
            continue
        match, category = item.get("match"), item.get("category")
        if not isinstance(match, str) or category not in CATEGORIES:
            continue
        match = match.strip()[:MAX_MATCH_CHARS]
        key = match.casefold()
        if not match or key in seen:
            continue
        seen.add(key)
        pairs.append((match, category))
        if len(pairs) >= MAX_OVERRIDES:
            break
    return tuple(pairs)


def classify(identity, overrides: Iterable[tuple[str, str]] = ()) -> Optional[AppKind]:
    """Category, surface and name for ``identity``, or None when it is unknown.

    A site is more specific than the browser showing it, so the order is:
    the user's choice for the site, the built-in site, the user's choice for
    the app, the built-in app, else Other.
    """
    if identity is None or not getattr(identity, "app_id", ""):
        return None
    app = lookup(identity.app_id)
    surface = app.surface if app is not None else TEXT
    name = app_name(identity.app_id, getattr(identity, "name", ""))
    by_key = {match.casefold(): category for match, category in overrides}
    hint = site_key(identity)
    known_site = site(hint) if hint else None
    if hint:
        name = known_site.name if known_site is not None else identity.title_hint
        if hint in by_key:
            return AppKind(by_key[hint], surface, name)
        if known_site is not None:
            return AppKind(known_site.category, surface, name)
    for key in app_keys(identity):
        if key in by_key:
            return AppKind(by_key[key], surface, name)
    return AppKind(app.category if app is not None else OTHER, surface, name)


def surface_for(identity) -> str:
    app = lookup(getattr(identity, "app_id", "")) if identity is not None else None
    return app.surface if app is not None else TEXT


def is_terminal(identity) -> bool:
    return identity is not None and surface_for(identity) == TERMINAL


def known_names() -> list[str]:
    """Every app and site name the catalogue knows, sorted, without repeats."""
    names = {app.name for app in _APPS} | {known.name for known in _SITES}
    return sorted(names, key=str.casefold)


def builtin_category(name: str) -> Optional[str]:
    """The category a known app or site name has before any user choice."""
    key = (name or "").strip().casefold()
    known = _SITE_BY_KEY.get(key)
    if known is not None:
        return known.category
    for app in _APPS:
        if app.name.casefold() == key:
            return app.category
    return None
