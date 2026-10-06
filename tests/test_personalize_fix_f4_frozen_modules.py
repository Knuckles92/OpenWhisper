"""The frozen build ships every app module, including ones loaded by name.

PyInstaller follows import statements, even inside functions, but not a
module named in a string (``importlib.import_module(path)``). A module only
reachable that way is missing from the installed app while source runs and
the rest of the suite pass, which is how Settings once failed to open.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "OpenWhisper.spec"
PACKAGES = ("services", "ui_qt", "meeting", "transcriber")

#: Personalization modules the app reaches only inside functions or by name. The
#: frozen self-test must import each, so a build missing one fails.
FLOW_ON_DEMAND = {
    "services.audio_devices",
    "services.audio_player",
    "services.dictation_stats",
    "services.focus_context._capture",
    "services.synthetic_keys",
    "services.text_rewrite",
    "services.vocabulary",
    "ui_qt.dialogs.cleanup_levels",
    "ui_qt.dialogs.settings_commands",
    "ui_qt.dialogs.settings_dictionary",
    "ui_qt.dialogs.settings_microphones",
    "ui_qt.dialogs.settings_snippets",
    "ui_qt.dialogs.settings_styles",
    "ui_qt.dialogs.stats_dialog",
    "ui_qt.history_actions",
    "ui_qt.widgets.command_settings",
    "ui_qt.widgets.dictionary_library",
    "ui_qt.widgets.history_playback",
    "ui_qt.widgets.hotkey_row",
    "ui_qt.widgets.language_menu",
    "ui_qt.widgets.scratchpad",
    "ui_qt.widgets.snippets_panel",
}
FLOW_PLATFORM = {
    "win32": {
        "services._mouse_hook_win",
        "services.focus_context._win",
        "services.focus_context._win_uia",
    },
    "darwin": {"services.focus_context._mac"},
    "linux": {"services.focus_context._linux"},
}


def _app_modules() -> dict[str, Path]:
    found = {}
    for package in PACKAGES:
        for path in (ROOT / package).rglob("*.py"):
            parts = path.relative_to(ROOT).with_suffix("").parts
            if "__pycache__" in parts:
                continue
            if parts[-1] == "__init__":
                parts = parts[:-1]
            found[".".join(parts)] = path
    return found


def _spec_coverage() -> tuple[set[str], set[str]]:
    """Return (listed hidden imports, collect_submodules roots) of the spec."""
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    listed, roots = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            listed.add(node.value)
        if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "collect_submodules"
                and node.args and isinstance(node.args[0], ast.Constant)):
            roots.add(node.args[0].value)
    return listed, roots


def _shipped(name: str, listed: set[str], roots: set[str]) -> bool:
    return name in listed or any(name == root or name.startswith(root + ".") for root in roots)


def _references(modules: dict[str, Path]) -> tuple[set[str], dict[str, str]]:
    """Return modules named by import statements, and by plain strings."""
    imported: set[str] = set()
    named: dict[str, str] = {}
    sources = list(modules.items()) + [("main", ROOT / "main.py"), ("config", ROOT / "config.py")]
    for owner, path in sources:
        package = owner if path.name == "__init__.py" else owner.rpartition(".")[0]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".")[: len(package.split(".")) - node.level + 1]
                    base = ".".join(parts + ([base] if base else []))
                imported.add(base)
                imported.update(f"{base}.{alias.name}" for alias in node.names)
            # Dotted only: bare package names double as words ("meeting").
            elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and "." in node.value and node.value in modules):
                named.setdefault(node.value, owner)
    return imported, named


def test_every_module_named_in_a_string_ships_in_the_frozen_build():
    modules = _app_modules()
    listed, roots = _spec_coverage()
    imported, named = _references(modules)
    unshipped = sorted(
        f"{name} (named in {owner})"
        for name, owner in named.items()
        if name not in imported and not _shipped(name, listed, roots)
    )
    assert unshipped == []


def test_spec_collects_every_settings_page_and_dialog_export():
    from ui_qt import dialogs, widgets
    from ui_qt.dialogs.settings_dialog import _PAGE_MODULES

    listed, roots = _spec_coverage()
    names = {path for _key, path, _icon in _PAGE_MODULES}
    names |= {module for module, _attr in dialogs._EXPORTS.values()}
    names |= {module for module, _attr in widgets._EXPORTS.values()}
    assert sorted(name for name in names if not _shipped(name, listed, roots)) == []


def test_self_test_imports_every_page_and_on_demand_flow_module():
    from services import package_checks
    from ui_qt.dialogs.settings_dialog import _PAGE_MODULES

    for platform, expected in FLOW_PLATFORM.items():
        names = set(package_checks.app_module_names(platform))
        assert FLOW_ON_DEMAND <= names, sorted(FLOW_ON_DEMAND - names)
        assert expected <= names, (platform, sorted(expected - names))
        assert {path for _key, path, _icon in _PAGE_MODULES} <= names


def test_every_listed_app_module_imports():
    from services import package_checks

    listed, _roots = _spec_coverage()
    modules = _app_modules()
    here = "win32" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
    for name in sorted({*package_checks.app_module_names(here), *(listed & modules.keys())}):
        importlib.import_module(name)
    for platform in FLOW_PLATFORM:
        for name in package_checks.app_module_names(platform):
            assert importlib.util.find_spec(name) is not None, name


def test_import_app_modules_loads_pages_and_exports():
    from services import package_checks
    from ui_qt import dialogs, widgets

    package_checks.import_app_modules()
    for package in (widgets, dialogs):
        for name in package.__all__:
            assert name in vars(package)
    for name in package_checks.app_module_names():
        assert name in sys.modules
