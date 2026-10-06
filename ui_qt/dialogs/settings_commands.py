"""Settings → Personalize → Commands: edit text by voice, and saved rewrites.

Settings imports this module when it registers its pages, before the page is
built, so module-level imports stay light; ``build`` imports the widgets.
"""

TITLE = "Commands"
SUBTITLE = "Rewrite selected text by voice, and keep the rewrites you use often."
SEARCH_FIELDS = [
    (
        "commands_tile",
        "Commands",
        "Coming soon: Command Mode rewrites the selected text from a spoken instruction.",
    ),
]
CONTROL_ATTRS = ("commands_tile",)


def build(dialog, layout) -> None:
    from ui_qt.utils.icons import design_icon
    from ui_qt.widgets.setting_tile import InfoTile

    _attr, title, description = SEARCH_FIELDS[0]
    dialog.commands_tile = InfoTile(title, description, design_icon("wand-purple.svg"))
    dialog._tile_group(layout, "", [dialog.commands_tile], columns=1)


def load(dialog, settings: dict) -> None:
    pass


def rail_value(settings: dict) -> str:
    return "Coming soon"


def refresh(dialog) -> None:
    pass
