"""Settings → Personalize → Dictionary: words dictation should spell your way.

Settings imports this module when it registers its pages, before the page is
built, so module-level imports stay light; ``build`` imports the widgets.
"""

TITLE = "Dictionary"
SUBTITLE = "Names, jargon, and spellings OpenWhisper should get right every time."
SEARCH_FIELDS = [
    (
        "dictionary_tile",
        "Dictionary",
        "Coming soon: names and terms that dictation always spells your way.",
    ),
]
CONTROL_ATTRS = ("dictionary_tile",)


def build(dialog, layout) -> None:
    from ui_qt.utils.icons import design_icon
    from ui_qt.widgets.setting_tile import InfoTile

    _attr, title, description = SEARCH_FIELDS[0]
    dialog.dictionary_tile = InfoTile(title, description, design_icon("book-blue.svg"))
    dialog._tile_group(layout, "", [dialog.dictionary_tile], columns=1)


def load(dialog, settings: dict) -> None:
    pass


def rail_value(settings: dict) -> str:
    return "Coming soon"


def refresh(dialog) -> None:
    pass
