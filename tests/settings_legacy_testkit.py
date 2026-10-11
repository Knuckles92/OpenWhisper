"""Mount retained Basic components explicitly for their unit regressions.

Production Settings always uses the six categories. These components still
share save handlers with Settings; constructing one here does not make a
saved Basic preference or a legacy route restore the retired presentation.
"""
from PyQt6.QtWidgets import QApplication


def mount_legacy_basic(dialog, key):
    dialog.ensure_page(key)
    dialog.stack.setCurrentWidget(dialog._page_scrolls[key])
    page = dialog._basic_pages[key]
    page.refresh()
    QApplication.processEvents()
    return page
