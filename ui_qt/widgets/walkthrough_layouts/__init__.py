"""Prototype layouts for the Walkthrough tab.

Five ways the tab could be arranged, each a subclass of the real
``WalkthroughTab`` that lays its own controls out again, so every choice
saves and behaves exactly as it does today. None is on unless asked for:

    OPENWHISPER_WALKTHROUGH_LAYOUT=sentence python main.py

Names: ``sentence``, ``stages``, ``goals``, ``gallery``, ``studio``. Anything
else (or nothing) is the tab as shipped. These are for choosing a direction;
the one picked gets built into ``walkthrough_tab.py`` and the rest deleted.
"""
from __future__ import annotations

import importlib
import os

ENV = "OPENWHISPER_WALKTHROUGH_LAYOUT"

#: name -> (module, class)
LAYOUTS = {
    "sentence": ("sentence", "SentenceTab"),
    "stages": ("stages", "StagesTab"),
    "goals": ("goals", "GoalsTab"),
    "gallery": ("gallery", "GalleryTab"),
    "studio": ("studio", "StudioTab"),
}


def tab_class():
    """The Walkthrough tab to build: a prototype when the environment names one."""
    from ui_qt.widgets.walkthrough_tab import WalkthroughTab

    name = os.environ.get(ENV, "").strip().lower()
    if name not in LAYOUTS:
        return WalkthroughTab
    module, cls = LAYOUTS[name]
    return getattr(importlib.import_module(f"{__name__}.{module}"), cls)
