"""Every ``SettingsKey.X`` in the app names a declared setting.

mypy catches a misspelt key in the modules it checks (see [tool.mypy] in
pyproject.toml); this covers the rest of the code until they are checked too.
"""
from __future__ import annotations

import ast
from pathlib import Path

from services.settings import SettingsKey

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("services", "transcriber", "ui_qt", "meeting", "benchmarks", "scripts")


def _python_files():
    yield from ROOT.glob("*.py")
    for folder in SOURCES:
        yield from (ROOT / folder).rglob("*.py")


def test_every_settings_key_reference_is_declared():
    declared = {name for name in vars(SettingsKey) if name.isupper()}
    unknown = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "SettingsKey" and node.attr.isupper()
                    and node.attr not in declared):
                unknown.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.attr}")
    assert not unknown, "Undeclared settings keys:\n" + "\n".join(unknown)
