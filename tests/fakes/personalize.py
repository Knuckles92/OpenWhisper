"""Shared fakes and harnesses for the tests/test_personalize_*.py files."""
import os
import tempfile
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

from services.settings import SettingsManager


@contextmanager
def settings_dialogs(*, defaults=None, mocks=(), close=False, **dialog_kwargs):
    """Yield ``build(values) -> (dialog, store)``: Settings on a throwaway store, no model cache.

    ``defaults`` are saved under each build's values, ``mocks`` names the
    dialog callbacks to replace with a MagicMock, and ``close`` closes every
    dialog built before the patches are undone. ``build.dialogs`` lists them.
    """
    from ui_qt.dialogs import settings_dialog as settings_dialog_module
    from ui_qt.dialogs import settings_downloads as downloads_module
    from ui_qt.dialogs import settings_models as models_module

    stacks = []
    temp = tempfile.TemporaryDirectory()

    def build(values=None):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({**(defaults or {}), **(values or {})})
        stack = ExitStack()
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(patch.object(settings_dialog_module.history_manager, "set_retention"))
        for module in (models_module, downloads_module):
            stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        stacks.append(stack)
        dialog = settings_dialog_module.SettingsDialog(background_cache_scan=False, **dialog_kwargs)
        for name in mocks:
            setattr(dialog, name, MagicMock())
        build.dialogs.append(dialog)
        return dialog, store

    build.dialogs = []
    yield build
    if close:
        for dialog in build.dialogs:
            dialog.close()
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def large_fonts(monkeypatch, ui_mode):
    """Switch to ``ui_mode`` at the 130% font scale; returns the QApplication."""
    from PyQt6.QtWidgets import QApplication

    from ui_qt.utils.font_scale import apply_ui_font_scale
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    apply_ui_font_scale(130, app=app, theme_manager=ThemeManager())
    return app


def assert_wrapped_labels_fit(widget):
    """No visible word-wrapped label under ``widget`` is cut off."""
    from PyQt6.QtWidgets import QLabel

    for label in widget.findChildren(QLabel):
        if label.isVisible() and label.wordWrap():
            assert label.height() >= label.heightForWidth(label.width()), label.text()


def assert_page_ends_in_view(dialog, key):
    """Scrolled to the bottom, the page ends inside the viewport."""
    from PyQt6.QtWidgets import QApplication

    page, scroll = dialog._pages[key], dialog._page_scrolls[key]
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    QApplication.processEvents()
    assert page.mapTo(scroll.viewport(), page.rect().bottomRight()).y() <= scroll.viewport().height()
