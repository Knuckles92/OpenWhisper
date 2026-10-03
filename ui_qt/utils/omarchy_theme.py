"""Map Omarchy's public palette to the shared OpenWhisper widget roles."""

from __future__ import annotations

import re
import tomllib

from PyQt6.QtGui import QColor

from services.desktop_session import omarchy_theme_paths, use_omarchy_ui
from ui_qt.utils.palette import DARK, LIGHT, Palette, palette_for

_HEX = re.compile(r"#[0-9a-fA-F]{6}\Z")


def load_omarchy_palette(fallback: str = DARK) -> Palette:
    for path in omarchy_theme_paths():
        try:
            # Theme files are data. Ignore invalid values rather than allowing
            # arbitrary CSS or commands to enter the widget stylesheets.
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
            colors = {
                k: v for k, v in raw.items() if isinstance(v, str) and _HEX.fullmatch(v)
            }
            if "background" in colors and "foreground" in colors:
                return palette_from_colors(colors, raw.get("mode"))
        except (OSError, ValueError):
            continue
    return palette_for(fallback)


def palette_from_colors(colors: dict[str, str], mode: str | None = None) -> Palette:
    bg, fg = colors["background"], colors["foreground"]
    dark = mode != LIGHT if mode in (DARK, LIGHT) else QColor(bg).lightnessF() < 0.5
    base = palette_for(DARK if dark else LIGHT)
    tokens = dict(base.tokens)

    def mix(left: str, right: str, fraction: float) -> str:
        a, b = QColor(left), QColor(right)
        return QColor(
            *(
                round(x + (y - x) * fraction)
                for x, y in zip(a.getRgb()[:3], b.getRgb()[:3], strict=True)
            )
        ).name()

    surface = colors.get(
        "lighter_background", colors.get("lighter_bg", mix(bg, fg, 0.06))
    )
    hover = mix(bg, fg, 0.12)
    border = mix(bg, fg, 0.24)
    secondary = mix(bg, fg, 0.74)
    accent = colors.get("accent", colors.get("blue", colors.get("color4", fg)))
    bright = colors.get("bright_foreground", colors.get("bright_fg", fg))
    groups = [
        (
            bg,
            (
                "bg",
                "surface-sunken",
                "splash-bg",
                "slate-bg",
                "slate-rail",
                "overlay-bg",
                "code-on-surface",
            ),
        ),
        (
            surface,
            (
                "surface",
                "surface-disabled",
                "slate-surface",
                "slate-field",
                "slate-disabled",
                "code-on-bg",
                "quote-on-bg",
                "quote-on-surface",
            ),
        ),
        (
            hover,
            (
                "surface-hover",
                "surface-active",
                "slate-surface-hover",
                "slate-rail-hover",
                "slate-raised",
                "slate-check-disabled",
            ),
        ),
        (
            border,
            (
                "border",
                "border-strong",
                "border-hover",
                "splash-border",
                "slate-border",
                "slate-border-strong",
                "slate-border-hover",
                "overlay-border",
            ),
        ),
        (mix(bg, fg, 0.13), ("border-subtle", "slate-border-subtle")),
        (
            fg,
            (
                "text",
                "text-body",
                "text-soft",
                "overlay-text",
                "slate-text",
                "slate-text-2",
            ),
        ),
        (bright, ("text-heading", "knob", "knob-border")),
        (
            secondary,
            (
                "text-secondary",
                "text-secondary-strong",
                "text-tertiary",
                "slate-text-3",
            ),
        ),
        (
            mix(bg, fg, 0.48),
            ("text-muted", "text-faint", "slate-text-4", "slate-text-disabled"),
        ),
        (accent, ("accent", "accent-border", "accent-soft")),
    ]
    # Iterate pairs instead of relying on color uniqueness (monochrome themes).
    for color, roles in groups:
        for role in roles:
            tokens[role] = color
    tokens["accent-hover"] = mix(accent, fg, 0.14)
    tokens["accent-pressed"] = mix(accent, bg, 0.16)
    tokens["accent-cyan"] = colors.get("cyan", colors.get("color6", accent))
    for role, amount in (
        ("accent-tint", 0.10),
        ("accent-tint-strong", 0.18),
        ("accent-tint-border", 0.4),
        ("accent-tint-border-strong", 0.6),
        ("accent-muted", 0.22),
    ):
        tokens[role] = mix(bg, accent, amount)
    # All filled action buttons use this foreground: favor the palette's base.
    tokens["on-accent"] = bg if QColor(accent).lightnessF() > 0.5 else "#ffffff"
    for role, canonical, legacy in (
        ("success", "green", "color2"),
        ("danger", "red", "color1"),
        ("warning", "yellow", "color3"),
        ("purple", "magenta", "color5"),
    ):
        color = colors.get(canonical, colors.get(legacy, tokens[role]))
        for key in tokens:
            if key == role or key.startswith(role + "-") and not key.endswith("-rgb"):
                tokens[key] = mix(bg, color, 0.18 if "tint" in key else 1)
    for key in tokens:
        if key.endswith("-rgb") and key[:-4] in tokens:
            color = QColor(tokens[key[:-4]])
            tokens[key] = f"{color.red()}, {color.green()}, {color.blue()}"
    return Palette(base.name, tokens)


def desktop_stylesheet(source: str) -> str:
    """Apply the same geometry/font treatment to global and local QSS."""
    if not use_omarchy_ui():
        return source
    source = re.sub(
        r"border(?:-top-left|-top-right|-bottom-left|-bottom-right)?-radius\s*:\s*[^;}{]+",
        "border-radius: 2px",
        source,
    )
    return re.sub(
        r"font-family\s*:\s*[^;}{]+",
        'font-family: "Noto Sans", "DejaVu Sans", sans-serif',
        source,
    )
