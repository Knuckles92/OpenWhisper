"""Settings → Personalize → Snippets: spoken shortcuts for text you type often.

Settings imports this module when it registers its pages, before the page is
built, so module-level imports stay light; ``build`` imports the widgets.
"""

TITLE = "Snippets"
SUBTITLE = "Say a short phrase to insert text you use often, exactly as written."
SEARCH_FIELDS = [
    (
        "snippets_tile",
        "Snippets",
        "Coming soon: say a trigger phrase to insert an address, a link, or a signature.",
    ),
]
CONTROL_ATTRS = ("snippets_tile",)


def build(dialog, layout) -> None:
    from ui_qt.utils.icons import design_icon
    from ui_qt.widgets.setting_tile import InfoTile

    _attr, title, description = SEARCH_FIELDS[0]
    dialog.snippets_tile = InfoTile(title, description, design_icon("text-plus-green.svg"))
    dialog._tile_group(layout, "", [dialog.snippets_tile], columns=1)


def load(dialog, settings: dict) -> None:
    pass


def rail_value(settings: dict) -> str:
    return "Coming soon"


def refresh(dialog) -> None:
    pass
