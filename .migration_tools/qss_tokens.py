"""One-off: rewrite colour literals in theme.qss (and python inline QSS) to @tokens."""
import re
import sys
from pathlib import Path

HEX = re.compile(r"#[0-9a-fA-F]{6}\b")
RGBA = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([0-9.]+)\s*)?\)")

# token by (hex) -> either str or dict keyed by 'color'|'bg'|'border'
MAP = {
    "#1c1c1e": "bg",
    "#19191b": "bg",
    "#1b1b1e": "bg",
    "#2c2c2e": {"bg": "surface", "border": "border-subtle", "color": "surface"},
    "#3a3a3c": {"bg": "surface-hover", "border": "border", "color": "surface-hover"},
    "#48484a": {"bg": "surface-active", "border": "border-strong", "color": "text-faint"},
    "#545456": {"bg": "border-hover", "border": "border-hover", "color": "border-hover"},
    "#636366": {"color": "text-muted", "bg": "text-muted", "border": "text-muted"},
    "#f5f5f7": {"color": "text", "bg": "knob", "border": "text"},
    "#ffffff": {"bg": "on-accent", "border": "on-accent", "color": "ON_ACCENT_OR_HEADING"},
    "#8e8e93": "text-secondary",
    "#98989d": "text-secondary-strong",
    "#aeaeb2": "text-tertiary",
    "#b0b0b5": "text-tertiary",
    "#9a9aa0": "text-secondary",
    "#7d7d82": "text-secondary",
    "#5c5c60": "text-muted",
    "#c7c7cc": "text-soft",
    "#d1d1d6": "text-body",
    "#d6d6da": "text-body",
    "#e5e5e7": "text-body",
    "#e5e5ea": "text-body",
    "#f2f2f7": "text",
    "#242426": {"bg": "surface", "border": "border", "color": "surface"},
    "#232326": {"bg": "surface", "border": "border", "color": "surface"},
    "#313135": "border",
    "#2f2f33": "border-subtle",
    "#2f2f31": "border",
    "#232325": "surface-disabled",
    # accent
    "#0a84ff": "accent",
    "#007aff": "accent-hover",
    "#0062cc": "accent-pressed",
    "#0060df": "accent-pressed",
    "#6fb1ff": "accent-soft", "#64b5ff": "accent-soft", "#8eb8ff": "accent-soft",
    "#8fc2ff": "accent-soft", "#8ec2ff": "accent-soft", "#83bdff": "accent-soft",
    "#62aaff": "accent-soft", "#4aa0ff": "accent-soft", "#4aa8ff": "accent-soft",
    "#409cff": "accent-soft", "#4da3ff": "accent-soft",
    "#318dff": "accent-border", "#3298ff": "accent-border", "#268cff": "accent-border",
    "#2492ff": "accent-hover", "#339dff": "accent-hover",
    "#64d2ff": "accent-cyan",
    "#142130": "accent-tint", "#172b45": "accent-tint", "#172538": "accent-tint",
    "#12222f": "accent-tint", "#121d28": "accent-tint",
    "#17263a": "accent-tint-strong", "#17345a": "accent-tint-strong",
    "#173d63": "accent-tint-strong", "#15283a": "accent-tint-strong",
    "#2a5382": "accent-tint-border", "#245079": "accent-tint-border",
    "#3a6aa3": "accent-tint-border-strong", "#2f6396": "accent-tint-border-strong",
    "#24425f": "accent-muted",
    # success
    "#30d158": {"color": "success-text", "bg": "success", "border": "success"},
    "#28cd41": "success-hover",
    "#1fb34a": "success-pressed", "#248a3d": "success-pressed",
    "#32d74b": "success-text-strong", "#42df91": "success-text-strong",
    # danger
    "#ff453a": {"color": "danger-text", "bg": "danger", "border": "danger"},
    "#ff3b30": "danger-hover",
    "#d70015": "danger-pressed",
    "#ff6b6b": "danger-text-soft", "#ff6961": "danger-text-soft",
    "#a93636": "danger-pressed", "#c34545": "danger",
    # warning
    "#ff9f0a": {"color": "warning-text", "bg": "warning", "border": "warning"},
    "#ff9500": "warning-hover",
    "#cc7700": "warning-pressed",
    "#e0a052": "warning-text-soft",
    "#ffd08a": "warning-text-strong", "#ffd60a": "warning-text-strong",
    "#1c1914": "warning-tint", "#2b2118": "warning-tint", "#332817": "warning-tint",
    "#5a4a2a": "warning-tint-border", "#64491f": "warning-tint-border", "#8a5a14": "warning-tint-border",
    # purple
    "#d29bf5": "purple-text",
    # slate
    "#10161c": "slate-bg",
    "#0c1116": "slate-rail",
    "#141d26": "slate-rail-hover",
    "#141b22": "slate-surface",
    "#182028": "slate-surface-hover",
    "#161e26": "slate-field", "#151c23": "slate-field", "#151d25": "slate-field", "#0f141a": "slate-field",
    "#1a232c": "slate-field",
    "#1b252e": "slate-raised", "#1b242e": "slate-raised", "#1c2731": "slate-raised",
    "#22303b": "slate-raised", "#232f3b": "slate-raised", "#243039": "slate-raised", "#1f2a35": "slate-raised",
    "#12181e": "slate-disabled", "#12181f": "slate-disabled",
    "#303b45": "slate-border", "#2a3540": "slate-border", "#263240": "slate-border", "#2c363f": "slate-border",
    "#232d36": "slate-border-subtle", "#263038": "slate-border-subtle", "#2a333c": "slate-border-subtle",
    "#3b4752": "slate-border-strong", "#35404a": "slate-border-strong", "#3a4652": "slate-border-strong",
    "#3d4a57": "slate-border-strong", "#3a4650": "slate-border-strong",
    "#4b5966": "slate-border-hover", "#52606d": "slate-border-hover",
    "#5d6873": {"color": "slate-text-disabled", "bg": "slate-border-hover", "border": "slate-border-hover"},
    "#e8edf2": "slate-text", "#f2f5f8": "slate-text", "#f0f3f6": "slate-text", "#eef2f6": "slate-text",
    "#f1f4f7": "slate-text", "#eaeff4": "slate-text", "#edf6ff": "slate-text",
    "#c7d0d9": "slate-text-2", "#aeb8c3": "slate-text-2", "#b7c0cb": "slate-text-2",
    "#c3ccd6": "slate-text-2", "#c5ced8": "slate-text-2", "#cfd8e2": "slate-text-2",
    "#98a3b0": "slate-text-3", "#9da8b5": "slate-text-3", "#8e99a6": "slate-text-3", "#8d9aa7": "slate-text-3",
    "#6b7885": "slate-text-4", "#79858f": "slate-text-4", "#7d8b99": "slate-text-4", "#7b8794": "slate-text-4",
    "#6f7b87": "slate-text-4", "#66707a": "slate-text-4", "#67717c": "slate-text-4", "#5c6770": "slate-text-4",
    "#1c242c": "slate-check-disabled",
}

RGB_MAP = {
    (10, 132, 255): "accent-rgb",
    (48, 209, 88): "success-rgb",
    (255, 69, 58): "danger-rgb",
    (255, 159, 10): "warning-rgb",
    (191, 90, 242): "purple-rgb",
    (141, 154, 167): "slate-text-3-rgb",
    (44, 44, 46): "surface-rgb",
    (58, 58, 60): "surface-hover-rgb",
}

FILLED = {"accent", "accent-hover", "accent-pressed", "success", "success-hover", "success-pressed",
          "danger", "danger-hover", "danger-pressed", "warning", "warning-hover", "warning-pressed",
          "accent-border"}

unmapped = set()
decisions = []


def prop_class(prop: str) -> str:
    p = prop.strip().lower()
    if p == "color" or p.endswith("-color") and "background" not in p and "border" not in p and "selection" not in p:
        return "color"
    if p.startswith("background") or p.startswith("selection-background") or p == "background":
        return "bg"
    if p.startswith("border"):
        return "border"
    return "color"


def map_hex(hexv: str, cls: str):
    key = hexv.lower()
    m = MAP.get(key)
    if m is None:
        unmapped.add(key)
        return None
    if isinstance(m, dict):
        return m.get(cls, m.get("color"))
    return m


def block_filled(decls):
    """Does this block set a filled background?"""
    for prop, val in decls:
        if prop_class(prop) == "bg":
            for h in HEX.findall(val):
                t = map_hex(h, "bg")
                if t in FILLED:
                    return True
            for m in RGBA.finditer(val):
                pass
    return False


def convert_value(prop, val, filled, ctx):
    cls = prop_class(prop)

    def hex_sub(m):
        h = m.group(0)
        t = map_hex(h, cls)
        if t is None:
            return h
        if t == "ON_ACCENT_OR_HEADING":
            t = "on-accent" if filled else "text-heading"
            decisions.append((ctx, prop, h, t))
        return "@" + t

    def rgba_sub(m):
        r, g, b, a = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
        if (r, g, b) == (255, 255, 255):
            tok = "on-accent-rgb" if cls == "color" else "overlay-rgb"
            decisions.append((ctx, prop, m.group(0), tok))
        elif (r, g, b) == (0, 0, 0):
            if a in ("0.22",):
                return "@inset"
            if a in ("0.15",):
                return "@inset-soft"
            unmapped.add(m.group(0))
            return m.group(0)
        else:
            tok = RGB_MAP.get((r, g, b))
            if tok is None:
                unmapped.add(m.group(0))
                return m.group(0)
        if a is None:
            return f"rgb(@{tok})"
        return f"rgba(@{tok}, {a})"

    val = RGBA.sub(rgba_sub, val)
    val = HEX.sub(hex_sub, val)
    return val


def convert_qss(text: str) -> str:
    # Walk blocks: find "{ ... }" and process declarations inside.
    out = []
    i = 0
    while True:
        j = text.find("{", i)
        if j < 0:
            out.append(text[i:])
            break
        k = text.find("}", j)
        out.append(text[i:j + 1])
        body = text[j + 1:k]
        selector = text[text.rfind("}", 0, j) + 1:j].strip().splitlines()[-1] if text[:j].strip() else ""
        decls = []
        for line in body.split(";"):
            if ":" in line and not line.strip().startswith("/*"):
                # strip leading comment
                stripped = re.sub(r"/\*.*?\*/", "", line, flags=re.S)
                if ":" in stripped:
                    prop, val = stripped.split(":", 1)
                    decls.append((prop.strip(), val))
        filled = block_filled(decls)
        new_body = []
        for part in body.split(";"):
            stripped = re.sub(r"/\*.*?\*/", "", part, flags=re.S)
            if ":" in stripped and not part.strip().startswith("/*") or (":" in stripped and "/*" in part):
                # find prop name in the part (after any comment)
                pm = re.search(r"([A-Za-z-]+)\s*:", stripped)
                prop = pm.group(1) if pm else ""
                new_body.append(convert_value(prop, part, filled, selector))
            else:
                new_body.append(part)
        out.append(";".join(new_body))
        out.append("}")
        i = k + 1
    return "".join(out)


if __name__ == "__main__":
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    new = convert_qss(text)
    path.write_text(new, encoding="utf-8")
    print("UNMAPPED:", sorted(unmapped))
    for d in decisions:
        print("DECISION:", d)
