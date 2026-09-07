"""Semantic colour tokens for the dark and light themes.

``theme.qss`` and widget-local stylesheets name colours by role (``@surface``,
``@text-secondary``) rather than by value; ``Palette.resolve`` substitutes the
active theme's value for each token. Painted widgets read the same roles as
``QColor`` through :func:`current_palette`.

Two neutral families exist because the product does. The main window and its
dialogs use Apple's system greys; Settings, Model Manager, Downloads, and the
export dialogs use a cooler slate. Each family has its own light values so the
two keep their distinct character in both themes.

Colour tokens ending in ``-rgb`` are bare ``"r, g, b"`` triples for use inside
``rgba(@accent-rgb, 0.12)``, where the alpha stays in the stylesheet.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Final, Mapping

from PyQt6.QtGui import QColor

DARK: Final[str] = "dark"
LIGHT: Final[str] = "light"
THEMES: Final[tuple[str, ...]] = (DARK, LIGHT)

_TOKEN_RE = re.compile(r"@([a-z][a-z0-9]*(?:-[a-z0-9]+)*)")
_RGB_TRIPLE_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*$")

_DARK_TOKENS: Final[Dict[str, str]] = {
    # Neutral surfaces (main window, transcription tabs, most dialogs)
    "bg": "#1c1c1e",
    "surface": "#2c2c2e",
    "surface-hover": "#3a3a3c",
    "surface-active": "#48484a",
    "surface-disabled": "#232325",
    "surface-sunken": "#232326",
    "border": "#3a3a3c",
    "border-subtle": "#2c2c2e",
    "border-strong": "#48484a",
    "border-hover": "#545456",
    # Neutral text
    "text": "#f5f5f7",
    "text-heading": "#ffffff",
    "text-soft": "#c7c7cc",
    "text-body": "#d1d1d6",
    "text-secondary": "#8e8e93",
    "text-secondary-strong": "#98989d",
    "text-tertiary": "#aeaeb2",
    "text-muted": "#636366",
    "text-faint": "#48484a",
    "knob": "#f5f5f7",
    "knob-border": "#f5f5f7",
    "surface-rgb": "44, 44, 46",
    "surface-hover-rgb": "58, 58, 60",
    # Suffix on the grey/slate chevron assets: the dark set is the bare name,
    # the light set is redrawn in a deeper stroke that reads on white fields.
    "icon-suffix": "",
    # Accent (system blue)
    "accent": "#0a84ff",
    "accent-hover": "#007aff",
    "accent-pressed": "#0062cc",
    "accent-soft": "#6fb1ff",
    "accent-border": "#318dff",
    "accent-cyan": "#64d2ff",
    "accent-rgb": "10, 132, 255",
    "accent-cyan-rgb": "100, 210, 255",
    "accent-tint": "#142130",
    "accent-tint-strong": "#17345a",
    "accent-tint-border": "#2a5382",
    "accent-tint-border-strong": "#3a6aa3",
    "accent-muted": "#24425f",
    "on-accent": "#ffffff",
    "on-accent-rgb": "255, 255, 255",
    # Status colours. The ``-text`` variants are for type on the surface;
    # the base value is for fills and borders.
    "success": "#30d158",
    "success-hover": "#28cd41",
    "success-pressed": "#1fb34a",
    "success-text": "#30d158",
    "success-text-strong": "#32d74b",
    "success-rgb": "48, 209, 88",
    "danger": "#ff453a",
    "danger-hover": "#ff3b30",
    "danger-pressed": "#d70015",
    "danger-text": "#ff453a",
    "danger-text-soft": "#ff6961",
    "danger-rgb": "255, 69, 58",
    "warning": "#ff9f0a",
    "warning-hover": "#ff9500",
    "warning-pressed": "#cc7700",
    "warning-text": "#ff9f0a",
    "warning-text-soft": "#e0a052",
    "warning-text-strong": "#ffd08a",
    "warning-rgb": "255, 159, 10",
    "warning-tint": "#2b2118",
    "warning-tint-border": "#64491f",
    "purple": "#bf5af2",
    "purple-text": "#d29bf5",
    "purple-rgb": "191, 90, 242",
    # Translucent overlays: hover washes lighten a dark surface and darken a
    # light one, so the base colour flips with the theme.
    "overlay-rgb": "255, 255, 255",
    "inset": "rgba(0, 0, 0, 0.22)",
    "inset-soft": "rgba(0, 0, 0, 0.15)",
    "shadow-rgb": "0, 0, 0",
    # Floating surfaces painted by hand: the waveform HUD, the loading
    # screen, and the hotkey hover hint.
    "overlay-bg": "rgba(28, 28, 30, 0.93)",
    "overlay-border": "rgba(84, 84, 86, 0.67)",
    "overlay-text": "#f5f5f7",
    "splash-bg": "#1c1c1e",
    "splash-border": "#1e293b",
    # Slate family (Settings, Model Manager, Downloads, export dialogs)
    "slate-bg": "#10161c",
    "slate-rail": "#0c1116",
    "slate-rail-hover": "#141d26",
    "slate-surface": "#141b22",
    "slate-surface-hover": "#182028",
    "slate-field": "#161e26",
    "slate-raised": "#1b252e",
    "slate-disabled": "#12181e",
    "slate-border": "#303b45",
    "slate-border-subtle": "#232d36",
    "slate-border-strong": "#3b4752",
    "slate-border-hover": "#4b5966",
    "slate-text": "#e8edf2",
    "slate-text-2": "#c7d0d9",
    "slate-text-3": "#98a3b0",
    "slate-text-4": "#6b7885",
    "slate-text-disabled": "#5d6873",
    "slate-text-3-rgb": "141, 154, 167",
    "slate-check-disabled": "#1c242c",
}

_LIGHT_TOKENS: Final[Dict[str, str]] = {
    "bg": "#f2f2f7",
    "surface": "#ffffff",
    "surface-hover": "#e9e9ee",
    "surface-active": "#dcdce1",
    "surface-disabled": "#f0f0f3",
    "surface-sunken": "#f7f7f9",
    "border": "#d9d9de",
    "border-subtle": "#e5e5ea",
    "border-strong": "#c7c7cc",
    "border-hover": "#b4b4b9",
    "text": "#1d1d1f",
    "text-heading": "#111113",
    "text-soft": "#48484a",
    "text-body": "#3a3a3c",
    "text-secondary": "#6e6e73",
    "text-secondary-strong": "#636368",
    "text-tertiary": "#7c7c82",
    "text-muted": "#aeaeb2",
    "text-faint": "#c7c7cc",
    "knob": "#ffffff",
    "knob-border": "#c7c7cc",
    "surface-rgb": "255, 255, 255",
    "surface-hover-rgb": "233, 233, 238",
    "icon-suffix": "-light",
    "accent": "#007aff",
    "accent-hover": "#0071e3",
    "accent-pressed": "#0058b8",
    "accent-soft": "#0066cc",
    "accent-border": "#007aff",
    "accent-cyan": "#0b7fcf",
    "accent-rgb": "0, 122, 255",
    "accent-cyan-rgb": "11, 127, 207",
    "accent-tint": "#e8f1fd",
    "accent-tint-strong": "#d6e7fb",
    "accent-tint-border": "#9cc4f2",
    "accent-tint-border-strong": "#6ea8e8",
    "accent-muted": "#a9c9ee",
    "on-accent": "#ffffff",
    "on-accent-rgb": "255, 255, 255",
    "success": "#34c759",
    "success-hover": "#2db24f",
    "success-pressed": "#248a3d",
    "success-text": "#1e8e3e",
    "success-text-strong": "#1a7f37",
    "success-rgb": "52, 199, 89",
    "danger": "#ff3b30",
    "danger-hover": "#e6342a",
    "danger-pressed": "#c20012",
    "danger-text": "#d70015",
    "danger-text-soft": "#c8281f",
    "danger-rgb": "255, 59, 48",
    "warning": "#ff9500",
    "warning-hover": "#f08a00",
    "warning-pressed": "#cc7700",
    "warning-text": "#b25e00",
    "warning-text-soft": "#a35a00",
    "warning-text-strong": "#8a4b00",
    "warning-rgb": "255, 149, 0",
    "warning-tint": "#fff4e5",
    "warning-tint-border": "#f0c98a",
    "purple": "#af52de",
    "purple-text": "#8e3fc4",
    "purple-rgb": "175, 82, 222",
    "overlay-rgb": "0, 0, 0",
    "inset": "rgba(0, 0, 0, 0.04)",
    "inset-soft": "rgba(0, 0, 0, 0.03)",
    "shadow-rgb": "0, 0, 0",
    "overlay-bg": "rgba(252, 252, 253, 0.96)",
    "overlay-border": "rgba(0, 0, 0, 0.14)",
    "overlay-text": "#1d1d1f",
    "splash-bg": "#f7f7fa",
    "splash-border": "#cfd8e3",
    "slate-bg": "#f3f5f8",
    "slate-rail": "#e9edf2",
    "slate-rail-hover": "#dde3ea",
    "slate-surface": "#ffffff",
    "slate-surface-hover": "#f7f9fb",
    "slate-field": "#f4f6f9",
    "slate-raised": "#e8ecf1",
    "slate-disabled": "#eef1f4",
    "slate-border": "#dbe1e7",
    "slate-border-subtle": "#e6ebf0",
    "slate-border-strong": "#c9d2db",
    "slate-border-hover": "#a9b5c0",
    "slate-text": "#1b232c",
    "slate-text-2": "#46525e",
    "slate-text-3": "#66727e",
    "slate-text-4": "#8a959f",
    "slate-text-disabled": "#a3adb7",
    "slate-text-3-rgb": "102, 114, 126",
    "slate-check-disabled": "#e6ebf0",
}


@dataclass(frozen=True)
class Palette:
    name: str
    tokens: Mapping[str, str]

    @property
    def is_dark(self) -> bool:
        return self.name == DARK

    def css(self, token: str) -> str:
        """The stylesheet value for ``token`` (``"#1c1c1e"``, ``"10, 132, 255"``)."""
        try:
            return self.tokens[token]
        except KeyError:
            raise KeyError(f"Unknown palette token '@{token}'") from None

    def color(self, token: str, alpha: int | None = None) -> QColor:
        """``token`` as a ``QColor``; ``-rgb`` triples become opaque colours.

        Args:
            token: Palette role, without the leading ``@``.
            alpha: Replacement alpha in 0–255, or None to keep the token's own.
        """
        value = self.css(token)
        triple = _RGB_TRIPLE_RE.match(value)
        if triple:
            color = QColor(*(int(part) for part in triple.groups()))
        elif value.startswith("rgba("):
            parts = [p.strip() for p in value[5:-1].split(",")]
            color = QColor(int(parts[0]), int(parts[1]), int(parts[2]))
            color.setAlphaF(float(parts[3]))
        else:
            color = QColor(value)
        if alpha is not None:
            color.setAlpha(alpha)
        return color

    def resolve(self, text: str) -> str:
        """Substitute every ``@token`` in ``text``.

        Raises:
            KeyError: A token the palette does not define, so a typo cannot
                ship as literal ``@name`` text that Qt silently ignores.
        """
        if "@" not in text:
            return text
        return _TOKEN_RE.sub(lambda match: self.css(match.group(1)), text)


DARK_PALETTE: Final[Palette] = Palette(DARK, _DARK_TOKENS)
LIGHT_PALETTE: Final[Palette] = Palette(LIGHT, _LIGHT_TOKENS)
PALETTES: Final[Dict[str, Palette]] = {DARK: DARK_PALETTE, LIGHT: LIGHT_PALETTE}

_current: Palette = DARK_PALETTE


def current_palette() -> Palette:
    """The palette the application is currently styled with (dark by default)."""
    return _current


def set_current_palette(palette: Palette) -> None:
    global _current
    _current = palette


def palette_for(theme: str) -> Palette:
    return PALETTES.get(theme, DARK_PALETTE)


def resolve_tokens(text: str) -> str:
    """Substitute ``@token`` references with the current palette's values."""
    return _current.resolve(text)


def token_color(token: str, alpha: int | None = None) -> QColor:
    """Shorthand for ``current_palette().color(token, alpha)``."""
    return _current.color(token, alpha)
