"""The app catalogue: names, categories, surfaces, sites from titles, overrides."""

import pytest

from services.app_styles import AppCategory, Surface
from services.focus_context import AppIdentity, catalog


def _identity(app_id, name="", title_hint=""):
    return AppIdentity(app_id, name or catalog.app_name(app_id), title_hint=title_hint)


def test_constants_match_app_styles():
    assert catalog.CATEGORIES == AppCategory.ALL
    assert (catalog.TEXT, catalog.CODE, catalog.TERMINAL) == (
        Surface.TEXT, Surface.CODE, Surface.TERMINAL,
    )


@pytest.mark.parametrize("app_id,name,category,surface", [
    ("OUTLOOK.EXE", "Outlook", "email", "text"),
    ("olk.exe", "Outlook", "email", "text"),
    ("thunderbird", "Thunderbird", "email", "text"),
    ("com.apple.mail", "Mail", "email", "text"),
    ("slack.exe", "Slack", "work", "text"),
    ("ms-teams.exe", "Teams", "work", "text"),
    ("com.tinyspeck.slackmacgap", "Slack", "work", "text"),
    ("WhatsApp.exe", "WhatsApp", "personal", "text"),
    ("com.apple.MobileSMS", "Messages", "personal", "text"),
    ("discord", "Discord", "personal", "text"),
    ("Code.exe", "VS Code", "other", "code"),
    ("pycharm64.exe", "PyCharm", "other", "code"),
    ("devenv.exe", "Visual Studio", "other", "code"),
    ("WindowsTerminal.exe", "Windows Terminal", "other", "terminal"),
    ("conhost.exe", "Console", "other", "terminal"),
    ("OpenConsole.exe", "Windows Terminal", "other", "terminal"),
    ("pwsh.exe", "PowerShell", "other", "terminal"),
    ("com.googlecode.iterm2", "iTerm2", "other", "terminal"),
    ("winword.exe", "Word", "other", "text"),
])
def test_known_apps(app_id, name, category, surface):
    kind = catalog.classify(AppIdentity(app_id.lower(), catalog.app_name(app_id)))
    assert (kind.name, kind.category, kind.surface) == (name, category, surface)


@pytest.mark.parametrize("app_class", [
    "foot", "kitty", "alacritty", "com.mitchellh.ghostty", "org.wezfurlong.wezterm",
])
def test_hyprland_terminal_classes_are_terminals(app_class):
    assert catalog.is_terminal(AppIdentity(app_class, app_class))


def test_unknown_apps_are_other_text_with_a_tidy_name():
    kind = catalog.classify(AppIdentity("sumatra_pdf.exe", ""))
    assert kind == catalog.AppKind("other", "text", "Sumatra Pdf")
    assert catalog.pretty_name("com.example.FooBar") == "FooBar"
    assert catalog.pretty_name("my-tool") == "My Tool"
    assert catalog.app_name("whatever.exe", "Whatever Studio") == "Whatever Studio"
    assert catalog.classify(None) is None
    assert catalog.classify(AppIdentity("", "")) is None


@pytest.mark.parametrize("title,site", [
    ("Inbox (3) - dylan@example.com - Gmail - Google Chrome", "Gmail"),
    ("Inbox (3,021) - dylan@example.com - Gmail", "Gmail"),
    # Edge writes a zero-width space into its own name, and a profile.
    ("Mail - Dylan - Outlook - Personal - Microsoft​ Edge", "Outlook"),
    ("Inbox - Gmail and 3 more pages - Work - Microsoft​ Edge", "Gmail"),
    ("general (Channel) - Acme - Slack — Mozilla Firefox", "Slack"),
    ("Chat | Microsoft Teams - Google Chrome", "Teams"),
    ("Discord | #general | Friends - Brave", "Discord"),
    ("(2) WhatsApp - Google Chrome", "WhatsApp"),
    ("Messenger | Facebook - Opera", "Messenger"),
    ("Telegram Web - Google Chrome", "Telegram"),
    ("(5) Messaging | LinkedIn - Google Chrome", "LinkedIn"),
    ("Roadmap notes - Google Docs - Google Chrome", "Google Docs"),
    ("Notes about Slack - Google Docs", "Google Docs"),
    ("[ABC-12] Fix login - Jira - Google Chrome", "Jira"),
    ("Pull requests · acme/app · GitHub - Arc", "GitHub"),
    ("Weather in Paris - Google Search - Google Chrome", ""),
    ("", ""),
])
def test_sites_from_browser_titles(title, site):
    assert catalog.site_from_title(title) == site


def test_title_hints_come_only_from_browsers():
    title = "Inbox - Gmail - Google Chrome"
    assert catalog.title_hint("chrome.exe", title) == "Gmail"
    assert catalog.title_hint("msedge.exe", "Chat | Microsoft Teams - Microsoft​ Edge") == "Teams"
    assert catalog.title_hint("code.exe", "github - repo - Visual Studio Code") == ""
    assert catalog.title_hint("winword.exe", title) == ""


def test_a_site_in_a_browser_takes_the_site_category():
    gmail = AppIdentity("chrome.exe", "Chrome", title_hint="Gmail")
    assert catalog.classify(gmail) == catalog.AppKind("email", "text", "Gmail")
    plain = AppIdentity("chrome.exe", "Chrome")
    assert catalog.classify(plain) == catalog.AppKind("other", "text", "Chrome")


def test_overrides_win_and_match_any_name_case_insensitively():
    overrides = catalog.parse_overrides([
        {"match": "notion", "category": "work"},
        {"match": "WINWORD", "category": "email"},
        {"match": "Gmail", "category": "personal"},
    ])
    assert catalog.classify(_identity("notion.exe"), overrides).category == "work"
    assert catalog.classify(_identity("winword.exe"), overrides).category == "email"
    gmail = AppIdentity("chrome.exe", "Chrome", title_hint="Gmail")
    assert catalog.classify(gmail, overrides).category == "personal"


def test_a_built_in_site_beats_a_choice_for_its_browser():
    overrides = catalog.parse_overrides([{"match": "chrome", "category": "work"}])
    assert catalog.classify(AppIdentity("chrome.exe", "Chrome"), overrides).category == "work"
    gmail = AppIdentity("chrome.exe", "Chrome", title_hint="Gmail")
    assert catalog.classify(gmail, overrides).category == "email"


def test_parse_overrides_drops_bad_and_repeated_rows():
    raw = [
        {"match": " Slack ", "category": "personal"},
        {"match": "slack", "category": "email"},
        {"match": "", "category": "work"},
        {"match": "Zoom", "category": "loud"},
        {"match": 3, "category": "work"},
        "Teams",
        {"match": "x" * 500, "category": "other"},
    ]
    pairs = catalog.parse_overrides(raw)
    assert pairs[0] == ("Slack", "personal")
    assert len(pairs) == 2 and len(pairs[1][0]) == catalog.MAX_MATCH_CHARS
    assert catalog.parse_overrides("nope") == ()
    many = [{"match": f"app{index}", "category": "work"} for index in range(300)]
    assert len(catalog.parse_overrides(many)) == catalog.MAX_OVERRIDES


def test_matches_uses_id_stem_name_and_site():
    identity = AppIdentity("slack.exe", "Slack", title_hint="")
    assert catalog.matches(identity, "SLACK.EXE")
    assert catalog.matches(identity, "slack")
    assert not catalog.matches(identity, "teams")
    assert not catalog.matches(None, "slack")
    assert not catalog.matches(identity, "  ")
    site = AppIdentity("firefox.exe", "Firefox", title_hint="Gmail")
    assert catalog.matches(site, "gmail") and catalog.matches(site, "Firefox")


def test_known_names_and_featured_examples_are_in_the_catalogue():
    names = catalog.known_names()
    assert names == sorted(set(names), key=str.casefold)
    for category, examples in catalog.FEATURED.items():
        for name in examples:
            assert name in names
            assert catalog.builtin_category(name) == category
    assert catalog.builtin_category("Unknown thing") is None
