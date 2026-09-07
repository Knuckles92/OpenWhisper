"""Every @token referenced anywhere must exist in both palettes; both palettes must define the same keys."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_qt.utils.palette import _DARK_TOKENS, _LIGHT_TOKENS  # noqa: E402

root = Path(__file__).resolve().parents[1]
token_re = re.compile(r"@([a-z][a-z0-9-]*)")
used = {}
for path in [root / "ui_qt/styles/theme.qss", *root.glob("ui_qt/**/*.py")]:
    text = path.read_text(encoding="utf-8")
    for m in token_re.finditer(text):
        used.setdefault(m.group(1), set()).add(path.name)

dark, light = set(_DARK_TOKENS), set(_LIGHT_TOKENS)
print("dark-only:", sorted(dark - light))
print("light-only:", sorted(light - dark))
ignore = {"token", "tokens", "icon", "property", "staticmethod", "dataclass", "abstractmethod", "pyqtslot", "pytest", "classmethod", "override", "functools", "cached-property", "contextmanager", "wraps", "lru-cache"}
missing = {t: f for t, f in used.items() if t not in dark and t not in ignore}
for t, files in sorted(missing.items()):
    print("MISSING", t, sorted(files))
unused = sorted(dark - set(used))
print("unused tokens:", unused)
