"""Frozen helper invocations must exit before the application's bootstrap."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("helper", ["resource_tracker", "spawn", "forkserver"])
def test_frozen_helper_is_dispatched_before_application_imports(helper):
    pytest.importorskip("PyInstaller")
    # Execute the real PyInstaller runtime hook in a fresh interpreter, but
    # substitute helper bodies so regression testing cannot create a spawn loop.
    code = r'''
import importlib.abc
import multiprocessing
import multiprocessing.resource_tracker
import multiprocessing.spawn
import runpy
import sys
from pathlib import Path
from subprocess import _args_from_interpreter_flags
import PyInstaller

helper = sys.argv[1]
calls = []
def probe(*args, **kwargs):
    calls.append((args, kwargs))

if helper == "spawn":
    multiprocessing.spawn.spawn_main = probe
    arguments = ["--multiprocessing-fork", "tracker_fd=6", "pipe_handle=8"]
    expected = [((), {"tracker_fd": 6, "pipe_handle": 8})]
elif helper == "resource_tracker":
    multiprocessing.resource_tracker.main = probe
    arguments = [*_args_from_interpreter_flags(), "-c",
                 "from multiprocessing.resource_tracker import main;main(22)"]
    expected = [((22,), {})]
else:
    import types
    module = types.ModuleType("multiprocessing.forkserver")
    module.main = probe
    sys.modules[module.__name__] = module
    arguments = [*_args_from_interpreter_flags(), "-c",
                 "from multiprocessing.forkserver import main;main(3, 4, [], **{})"]
    expected = [((3, 4, []), {})]

hook = Path(PyInstaller.__file__).parent / "hooks/rthooks/pyi_rth_multiprocessing.py"
runpy.run_path(str(hook))

imports = []
class BlockAppImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"config", "services", "ui_qt", "PyQt6", "transcriber"}:
            imports.append(fullname)
            raise AssertionError("Helper attempted application import: " + fullname)
sys.meta_path.insert(0, BlockAppImports())
sys.frozen = True
sys.argv = ["OpenWhisper", *arguments]
try:
    runpy.run_path("main.py", run_name="__main__")
except SystemExit as exc:
    assert exc.code in (None, 0), exc.code
else:
    raise AssertionError("Helper did not exit")
assert calls == expected, calls
assert not imports, imports
'''
    result = subprocess.run(
        [sys.executable, "-c", code, helper],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_package_multiprocessing_check_spawns_a_clean_worker():
    from services.package_checks import check_multiprocessing

    check_multiprocessing()
