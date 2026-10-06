"""Settings → Personalize → Commands: edit text by voice, and saved rewrites.

Settings imports this module when it registers its pages, before the page is
built, so module-level imports stay light; ``build`` imports the widgets.
"""

TITLE = "Commands"
SUBTITLE = "Rewrite selected text by voice, and keep the rewrites you use often."
SEARCH_FIELDS = [
    (
        "commands_tile",
        "Command Mode",
        "Select text anywhere, use the shortcut, and say how to change it. "
        "The rewrite replaces your selection.",
    ),
    (
        "commands_insert_tile",
        "Write new text when nothing is selected",
        "Say what to write, like “a short thank-you note”, and it’s "
        "added at the cursor.",
    ),
    (
        "transforms_panel",
        "Transforms",
        "Saved rewrites for selected text. Give one a shortcut to use it in any app.",
    ),
    (
        "commands_profiles_tile",
        "Profile shortcuts",
        "Profiles record with their own shortcuts and format what you say.",
    ),
]
CONTROL_ATTRS = (
    "commands_tile",
    "commands_insert_tile",
    "commands_gate_tile",
    "transforms_panel",
    "commands_profiles_tile",
)

_FIELDS = {attr: (title, description) for attr, title, description in SEARCH_FIELDS}
_COMMAND_ACTION = "command_mode"


def _command_hotkey(settings: dict) -> str:
    hotkeys = settings.get("hotkeys")
    value = hotkeys.get(_COMMAND_ACTION, "") if isinstance(hotkeys, dict) else ""
    return value if isinstance(value, str) else ""


def _link(text: str, tooltip: str, on_click):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QPushButton

    link = QPushButton(text)
    link.setObjectName("cleanupRulesOpenCleanupLink")
    link.setFlat(True)
    link.setCursor(Qt.CursorShape.PointingHandCursor)
    link.setToolTip(tooltip)
    link.clicked.connect(on_click)
    return link


def _manager():
    # The store the dialog itself reads and writes, so the page never
    # disagrees with the rest of Settings about what is saved.
    from ui_qt.dialogs import settings_dialog

    return settings_dialog.settings_manager


def build(dialog, layout) -> None:
    from ui_qt.dialogs.settings_destinations import CLEANUP_PROFILES
    from ui_qt.dialogs.settings_fields import group_title, settings_caption
    from ui_qt.utils.icons import design_icon
    from ui_qt.widgets.command_settings import (
        CommandShortcutField,
        ShowWatcher,
        TransformsPanel,
    )
    from ui_qt.widgets.setting_tile import InfoTile, SettingTile
    from ui_qt.widgets.wrapped_label import WrappedLabel

    dialog.commands_gate_tile = InfoTile(
        "Set up AI cleanup first",
        "Command Mode and transforms use your AI cleanup model, even while "
        "cleanup is turned off.",
        design_icon("info-warning.svg"),
    )
    dialog.commands_gate_tile.setProperty("kind", "notice")
    dialog.commands_gate_tile.add_trailing(_link(
        "Open AI cleanup", "Choose a provider and model for AI cleanup",
        dialog.focus_cleanup_model,
    ))
    dialog.commands_gate_tile.hide()
    layout.addWidget(dialog.commands_gate_tile)

    title, description = _FIELDS["commands_tile"]
    dialog.commands_tile = InfoTile(title, description, design_icon("wand-purple.svg"))
    dialog.commands_shortcut = CommandShortcutField(dialog)
    dialog.commands_tile.add_body(dialog.commands_shortcut)
    dialog.commands_mode_hint = WrappedLabel("")
    dialog.commands_mode_hint.setObjectName("infoLabel")
    dialog.commands_tile.add_body(dialog.commands_mode_hint)

    title, description = _FIELDS["commands_insert_tile"]
    dialog.commands_insert_tile = SettingTile(title, description, design_icon("text-plus-green.svg"))
    dialog.commands_insert_tile.checkbox.toggled.connect(
        lambda checked: _on_insert_toggled(dialog, checked)
    )
    dialog._tile_group(layout, "", [dialog.commands_tile, dialog.commands_insert_tile])
    layout.addWidget(settings_caption(
        "Text you rewrite goes to your AI cleanup model and is saved in History, "
        "with the original kept alongside it."
    ))
    layout.addSpacing(8)

    title, description = _FIELDS["transforms_panel"]
    group_title(layout, title)
    layout.addWidget(settings_caption(description))
    dialog.transforms_panel = TransformsPanel(manager=_manager())
    dialog.transforms_panel.capture_changed.connect(dialog.set_hotkey_capture_suspended)
    dialog.transforms_panel.transforms_changed.connect(
        lambda: dialog.notify_changed("transforms")
    )
    layout.addWidget(dialog.transforms_panel)
    layout.addSpacing(8)

    title, description = _FIELDS["commands_profiles_tile"]
    dialog.commands_profiles_tile = InfoTile(title, description, design_icon("keyboard-green.svg"))
    dialog.commands_profiles_list = WrappedLabel("")
    dialog.commands_profiles_list.setObjectName("infoLabel")
    dialog.commands_profiles_tile.add_body(dialog.commands_profiles_list)
    dialog.commands_profiles_tile.add_trailing(_link(
        "Open Profiles", "Change profile recording shortcuts",
        lambda: dialog.select_destination(CLEANUP_PROFILES),
    ))
    dialog._tile_group(layout, "", [dialog.commands_profiles_tile], columns=1)

    # Shortcuts and the AI cleanup provider can change on other pages while
    # this one is hidden.
    dialog._commands_show_watcher = ShowWatcher(
        layout.parentWidget(), lambda: _show_state(dialog, dialog._settings_snapshot())
    )


def _on_insert_toggled(dialog, checked: bool) -> None:
    from PyQt6.QtCore import QSignalBlocker

    from services.settings import SettingsKey

    if dialog._loading:
        return
    if not dialog._persist(SettingsKey.COMMAND_MODE_INSERT_WITHOUT_SELECTION, bool(checked)):
        with QSignalBlocker(dialog.commands_insert_tile.checkbox):
            dialog.commands_insert_tile.checkbox.setChecked(not checked)


def _profile_shortcuts(settings: dict) -> str:
    from services.cleanup_profiles import load_cleanup_profiles
    from services.hotkey_manager import format_hotkey_display

    lines = [
        f"{profile.name} · {format_hotkey_display(profile.hotkey)}"
        for profile in load_cleanup_profiles(settings)
        if profile.hotkey
    ]
    return "\n".join(lines) if lines else "No profile has a shortcut yet."


def _show_state(dialog, settings: dict) -> None:
    from PyQt6.QtCore import QSignalBlocker

    from services.settings import resolve_command_mode_insert_without_selection
    from services.text_rewrite import provider_ready
    from ui_qt.utils.restyle import set_style_property
    from ui_qt.widgets.command_settings import command_mode_hint

    if "commands_tile" not in dialog.__dict__:
        return
    dialog.commands_gate_tile.setVisible(not provider_ready(settings))
    dialog.commands_shortcut.sync()
    dialog.commands_mode_hint.setText(command_mode_hint(settings))
    insert = resolve_command_mode_insert_without_selection(settings)
    tile = dialog.commands_insert_tile
    with QSignalBlocker(tile.checkbox):
        tile.checkbox.setChecked(insert)
    set_style_property(tile, "checked", insert)
    dialog.commands_profiles_list.setText(_profile_shortcuts(settings))


def load(dialog, settings: dict) -> None:
    _show_state(dialog, settings)


def rail_value(settings: dict) -> str:
    from services.text_transforms import load_transforms

    count = len(load_transforms(settings))
    shortcut = "Shortcut set" if _command_hotkey(settings) else "No shortcut"
    return f"{shortcut} · {count} transform{'' if count == 1 else 's'}"


def refresh(dialog) -> None:
    _show_state(dialog, dialog._settings_snapshot())
    panel = dialog.__dict__.get("transforms_panel")
    if panel is not None:
        panel.refresh()


def cancel_capture(dialog) -> None:
    for name in ("commands_shortcut", "transforms_panel"):
        widget = dialog.__dict__.get(name)
        if widget is not None:
            widget.cancel_capture()
    row = dialog.__dict__.get("commands_basic_shortcut")
    if row is not None:
        row.cancel_capture()


def basic_rows(page, group) -> None:
    from ui_qt.widgets.command_settings import CommandShortcutField, command_mode_hint

    dialog = page.dialog
    field = CommandShortcutField(dialog, change_button=True, clear_button=False, inline_errors=False)
    field.input.setMaximumWidth(210)
    dialog.commands_basic_shortcut = field
    detail = page._row(group, "Command Mode shortcut", "", field)

    def refresh_row(settings: dict) -> None:
        field.sync()
        detail.setText(
            "Select text, then say how to change it. " + command_mode_hint(settings)
        )

    page.add_refresh_hook(refresh_row)
