"""Settings → Automate → Actions: what a hotkey, a spoken name or the API does with your words.

Stream G builds this page (docs/design/automation/design.md). Settings imports
this module when it registers its pages, so module-level imports stay light;
``build`` imports the widgets.
"""

TITLE = "Actions"
SUBTITLE = "Send what you say somewhere other than the cursor: a chat, your agent, or your own tools."
SEARCH_FIELDS = [
    (
        "actions_panel",
        "Actions",
        "Saved actions. Give one a shortcut, or say its name with Route by voice.",
    ),
]
CONTROL_ATTRS = ("actions_panel",)


def build(dialog, layout) -> None:
    from ui_qt.utils.icons import design_icon
    from ui_qt.widgets.setting_tile import InfoTile

    title, description = SEARCH_FIELDS[0][1], SEARCH_FIELDS[0][2]
    dialog.actions_panel = InfoTile(title, description, design_icon("bolt-green.svg"))
    layout.addWidget(dialog.actions_panel)


def load(dialog, settings: dict) -> None:
    """Nothing to load until the page has its editor."""


def rail_value(settings: dict) -> str:
    from services.actions.model import load_actions

    count = len(load_actions(settings))
    return f"{count} action{'' if count == 1 else 's'}" if count else "None yet"


def refresh(dialog) -> None:
    """Nothing to refresh until the page has its editor."""
