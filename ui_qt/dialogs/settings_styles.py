"""Settings → Personalize → Styles: the app you dictate into and the tone it gets.

Settings imports this module when it registers its pages, before the page is
built, so module-level imports stay light; ``build`` imports the widgets.
"""

TITLE = "Apps & styles"
SUBTITLE = "Match the tone of a dictation to the app you're writing in."
SEARCH_FIELDS = [
    (
        "styles_tile",
        "Apps & styles",
        "Coming soon: formal email, casual chat, and awareness of the app you dictate into.",
    ),
]
CONTROL_ATTRS = ("styles_tile",)


def build(dialog, layout) -> None:
    from ui_qt.utils.icons import design_icon
    from ui_qt.widgets.setting_tile import InfoTile

    _attr, title, description = SEARCH_FIELDS[0]
    dialog.styles_tile = InfoTile(title, description, design_icon("palette-purple.svg"))
    dialog._tile_group(layout, "", [dialog.styles_tile], columns=1)


def load(dialog, settings: dict) -> None:
    pass


def rail_value(settings: dict) -> str:
    return "Coming soon"


def refresh(dialog) -> None:
    pass
