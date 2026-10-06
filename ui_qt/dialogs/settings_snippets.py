"""Settings → Personalize → Snippets: spoken shortcuts for text you type often.

Settings imports this module when it registers its pages, before the page is
built, so module-level imports stay light; ``build`` imports the widgets.
"""

from services.settings import SettingsKey, resolve_snippets_enabled
from services.snippets import load_snippets

TITLE = "Snippets"
SUBTITLE = "Say a short phrase to insert text you use often, exactly as written."
SEARCH_FIELDS = [
    (
        "snippets_enabled_tile",
        "Expand snippets as you dictate",
        "Say a trigger phrase, alone or mid-sentence, and its text is typed "
        "exactly as saved, with no AI. Live dictation only.",
    ),
    (
        "snippets_library_tile",
        "Your snippets",
        "Pick one to edit, or add a new one. Triggers ignore capitals and "
        "punctuation.",
    ),
]
CONTROL_ATTRS = (
    "snippets_enabled_tile",
    "snippets_enabled_check",
    "snippets_library_tile",
    "snippets_panel",
)


def _count(settings: dict) -> str:
    count = len(load_snippets(settings))
    return f"{count} snippet{'' if count == 1 else 's'}" if count else ""


def build(dialog, layout) -> None:
    from ui_qt.dialogs import settings_dialog
    from ui_qt.utils.icons import design_icon
    from ui_qt.widgets.setting_tile import InfoTile, SettingTile
    from ui_qt.widgets.snippets_panel import SnippetsPanel

    (enabled_attr, enabled_title, enabled_description), (
        library_attr, library_title, library_description,
    ) = SEARCH_FIELDS
    tile = SettingTile(enabled_title, enabled_description, design_icon("text-plus-green.svg"))
    setattr(dialog, enabled_attr, tile)
    dialog.snippets_enabled_check = tile.checkbox
    tile.checkbox.toggled.connect(lambda checked: _on_enabled_toggled(dialog, checked))
    dialog._tile_group(layout, "", [tile], columns=1)

    library = InfoTile(library_title, library_description, design_icon("notes-blue.svg"))
    setattr(dialog, library_attr, library)
    # The module-level name, so a Settings window bound to another store
    # (tests, previews) edits that store.
    panel = SnippetsPanel(settings_dialog.settings_manager)
    dialog.snippets_panel = panel
    library.add_trailing(panel.count_label)
    library.add_body(panel)
    panel.snippets_changed.connect(lambda: dialog.notify_changed("snippets"))
    dialog._tile_group(layout, "", [library], columns=1)


def _on_enabled_toggled(dialog, checked: bool) -> None:
    if dialog._loading:
        return
    if dialog._persist(SettingsKey.SNIPPETS_ENABLED, bool(checked)):
        dialog.notify_changed("snippets")
        return
    # The save failed: show what is still saved. While loading, the toggle
    # this causes is ignored, and the tile still restyles.
    dialog._loading = True
    try:
        dialog.snippets_enabled_check.setChecked(not checked)
    finally:
        dialog._loading = False


def load(dialog, settings: dict) -> None:
    dialog.snippets_enabled_check.setChecked(resolve_snippets_enabled(settings))
    dialog.snippets_panel.refresh()


def rail_value(settings: dict) -> str:
    if not resolve_snippets_enabled(settings):
        return "Off"
    return _count(settings) or "None yet"


def refresh(dialog) -> None:
    dialog.snippets_panel.refresh()


def basic_rows(page, group) -> None:
    from ui_qt.dialogs.settings_destinations import SNIPPETS
    from ui_qt.widgets.buttons import Button, neutral_button

    add = neutral_button(Button("Add"))
    add.set_base_minimum_size(88, 34)

    def add_snippet() -> None:
        page.dialog.select_destination(SNIPPETS)
        page.dialog.snippets_panel.new_snippet()

    add.clicked.connect(add_snippet)
    detail = page._row(group, "Snippets", "", add)
    add.setAccessibleName("Add a snippet")

    def show(settings: dict) -> None:
        count = _count(settings)
        if not resolve_snippets_enabled(settings):
            detail.setText(f"Off · {count}" if count else "Off")
        else:
            detail.setText(count or "None yet. Say a phrase to insert text you use often.")

    page.add_refresh_hook(show)
