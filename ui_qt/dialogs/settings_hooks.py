"""Settings → Automate → Hooks: webhooks, programs and text filters.

Stream H builds this page (docs/design/automation/design.md). Settings imports
this module when it registers its pages, so module-level imports stay light;
``build`` imports the widgets.
"""

TITLE = "Hooks"
SUBTITLE = "Tell your own tools when you dictate, and let them change the words before they're pasted."
SEARCH_FIELDS = [
    (
        "hooks_webhooks_panel",
        "Webhooks",
        "Send events such as finished dictations to a web address, signed so it can check them.",
    ),
]
CONTROL_ATTRS = ("hooks_webhooks_panel",)


def build(dialog, layout) -> None:
    from ui_qt.utils.icons import design_icon
    from ui_qt.widgets.setting_tile import InfoTile

    title, description = SEARCH_FIELDS[0][1], SEARCH_FIELDS[0][2]
    dialog.hooks_webhooks_panel = InfoTile(title, description, design_icon("server-blue.svg"))
    layout.addWidget(dialog.hooks_webhooks_panel)


def load(dialog, settings: dict) -> None:
    """Nothing to load until the page has its editors."""


def rail_value(settings: dict) -> str:
    from services.automation.models import load_filters, load_programs, load_webhooks

    count = len(load_webhooks(settings)) + len(load_programs(settings)) + len(load_filters(settings))
    return f"{count} hook{'' if count == 1 else 's'}" if count else "None yet"


def refresh(dialog) -> None:
    """Nothing to refresh until the page has its editors."""
