"""The right-click menu style shared by the history and Past Meetings lists.

These menus are roomier than the global ``QMenu`` rule in ``theme.qss``:
translucent, larger items, and a rounded frame.
"""
from PyQt6.QtWidgets import QMenu, QWidget

CONTEXT_MENU_STYLESHEET = """
    QMenu {
        background-color: rgba(@surface-rgb, 0.95);
        color: @text;
        border: 1px solid rgba(@overlay-rgb, 0.1);
        border-radius: 10px;
        padding: 6px;
    }
    QMenu::item {
        padding: 8px 28px 8px 14px;
        border-radius: 6px;
        font-size: 13px;
    }
    QMenu::item:selected {
        background-color: @accent;
        color: @on-accent;
    }
    QMenu::separator {
        background-color: rgba(@overlay-rgb, 0.08);
        height: 1px;
        margin: 4px 8px;
    }
    QMenu::item:disabled {
        color: @text-secondary;
    }
"""


def context_menu(parent: QWidget) -> QMenu:
    """A new list context menu owned by ``parent``."""
    menu = QMenu(parent)
    menu.setStyleSheet(CONTEXT_MENU_STYLESHEET)
    return menu
