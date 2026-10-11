"""Settings → Automate → Hooks: webhooks, programs and text filters.

Settings imports this module when it registers its pages, so module-level
imports stay light; ``build`` imports the widgets (ui_qt/widgets/hooks_panel.py).
See docs/design/automation/design.md, decisions 6–8.
"""

TITLE = "Hooks"
SUBTITLE = "Tell your own tools when you dictate, and let them change the words before they're pasted."
SEARCH_FIELDS = [
    (
        "hooks_webhooks_panel",
        "Webhooks",
        "Send events such as finished dictations to a web address, signed so it can check them.",
    ),
    (
        "hooks_programs_panel",
        "Programs",
        "Run a program on this computer when something happens, with the event as JSON on its input.",
    ),
    (
        "hooks_filters_panel",
        "Text filters",
        "Let a web address or a program change your words, before or after AI cleanup.",
    ),
]
CONTROL_ATTRS = (
    "hooks_webhooks_panel",
    "hooks_programs_panel",
    "hooks_filters_panel",
    "hooks_webhooks",
    "hooks_programs",
    "hooks_filters",
)

#: (tile attr, section attr, icon, section class name, notify kind)
_SECTIONS = (
    ("hooks_webhooks_panel", "hooks_webhooks", "webhook-blue.svg", "WebhookSection", "webhooks"),
    ("hooks_programs_panel", "hooks_programs", "terminal-green.svg", "ProgramSection", "programs"),
    ("hooks_filters_panel", "hooks_filters", "filter-purple.svg", "FilterSection", "filters"),
)
_FIELDS = {attr: (title, description) for attr, title, description in SEARCH_FIELDS}


def _manager():
    # The store the dialog itself reads and writes, so a Settings window bound
    # to another store (tests, previews) edits that store.
    from ui_qt.dialogs import settings_dialog

    return settings_dialog.settings_manager


def build(dialog, layout) -> None:
    from ui_qt.dialogs.settings_destinations import MCP
    from ui_qt.utils.icons import design_icon
    from ui_qt.widgets import hooks_panel
    from ui_qt.widgets.setting_tile import InfoTile

    manager = _manager()
    for tile_attr, section_attr, icon, class_name, kind in _SECTIONS:
        title, description = _FIELDS[tile_attr]
        tile = InfoTile(title, description, design_icon(icon))
        section = getattr(hooks_panel, class_name)(manager)
        tile.add_trailing(section.count_label)
        tile.add_body(section)
        # The editor needs the room more than the indent, most of all when narrow.
        tile.set_body_indented(False)
        section.changed.connect(lambda kind=kind: dialog.notify_changed(kind))
        setattr(dialog, tile_attr, tile)
        setattr(dialog, section_attr, section)
        dialog._tile_group(layout, "", [tile], columns=1)
    dialog.hooks_filters.leases.open_api_requested.connect(
        lambda: dialog.select_destination(MCP)
    )


def _sections(dialog):
    return [
        section for section in (dialog.__dict__.get(attr) for _t, attr, *_ in _SECTIONS)
        if section is not None
    ]


def load(dialog, settings: dict) -> None:
    for section in _sections(dialog):
        section.refresh()


def rail_value(settings: dict) -> str:
    from services.automation.models import load_filters, load_programs, load_webhooks

    items = [*load_webhooks(settings), *load_programs(settings), *load_filters(settings)]
    if not items:
        return "None yet"
    on = sum(1 for item in items if item.enabled)
    count = f"{len(items)} hook{'' if len(items) == 1 else 's'}"
    return count if on == len(items) else f"{count} · {on} on"


def refresh(dialog) -> None:
    for section in _sections(dialog):
        section.refresh()


def save_drafts(dialog, *, ask: bool = False) -> bool:
    return all(section.save_draft(ask=ask) for section in _sections(dialog))
