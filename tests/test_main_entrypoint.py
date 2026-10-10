"""Tests for the thin Qt entrypoint import behavior and startup profiler."""
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ui_qt.startup_profiler import StartupProfiler

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_startup_data(tmp_path, monkeypatch):
    # Importing main applies pending restores before importing the UI. Child
    # import probes must inherit a disposable root as well as the parent.
    monkeypatch.setenv("OPENWHISPER_DATA_DIR", str(tmp_path))


@pytest.mark.parametrize("module", ["ui_qt.main_window", "ui_qt.widgets"])
def test_ui_import_in_fresh_process(module):
    # Collection imports SettingsDialog first, which can hide circular imports.
    # Exercise the main-window/widget-first order on every supported platform.
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_usage_and_exits_without_opening_the_app(flag):
    result = subprocess.run(
        [sys.executable, "main.py", flag],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    usage = result.stdout
    assert usage.startswith("Usage: openwhisper")
    for option in ("--version", "--diagnostics OUTPUT.json", "--api [--database PATH] [--port N]",
                   "OPENWHISPER_API_TOKEN", "--self-test", "--workflow-smoke [REPORT.json]",
                   "OPENWHISPER_UI=omarchy|classic"):
        assert option in usage
    for internal in ("--local-asr-worker", "--isolated-worker", "--macos-update-helper", "internal"):
        assert internal not in usage


def test_help_loads_neither_qt_nor_the_speech_runtimes():
    code = """
import runpy, sys
sys.argv = ['main.py', '--help']
try:
    runpy.run_path('main.py', run_name='__main__')
except SystemExit as exit:
    assert exit.code == 0, exit.code
loaded = [name for name in sys.modules
          if name.split('.')[0] in ('PyQt6', 'ctranslate2', 'torch', 'numpy', 'services', 'ui_qt')]
assert not loaded, loaded
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_main_import_does_not_eagerly_import_application_controller():
    code = """
import sys
import main
assert hasattr(main, 'main')
assert 'services.application_controller' not in sys.modules
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("activation_fails", [False, True])
def test_source_startup_activates_components_before_backend_imports(activation_fails):
    code = """
import sys
import types

import services.components

calls = []
def prune_orphans():
    assert 'ctranslate2' not in sys.modules
    calls.append('pruned')
    if sys.argv[1] == 'broken':
        raise OSError('Component store unavailable')
services.components.prune_orphans = prune_orphans

activation = types.ModuleType('services.component_runtime')
def activate_components():
    assert 'ctranslate2' not in sys.modules
    assert 'services.application_controller' not in sys.modules
    calls.append('activated')
    if sys.argv[1] == 'broken':
        raise RuntimeError('Component unavailable')
activation.activate_components = activate_components
sys.modules['services.component_runtime'] = activation
assert not getattr(sys, 'frozen', False)

import main

assert calls == ['pruned', 'activated'], calls
assert hasattr(main, 'main')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, "broken" if activation_fails else "ok"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr


# Startup profiling hooks.

def test_startup_profiler_records_elapsed_times():
    with patch(
        "ui_qt.startup_profiler.time.perf_counter",
        side_effect=[10.25, 10.75],
    ):
        profiler = StartupProfiler(start_time=10.0)
        profiler.mark("first")
        profiler.mark("second")

    assert profiler.events == [("first", 0.25), ("second", 0.75)]


def test_startup_profiler_logs_totals_and_deltas(caplog):
    profiler = StartupProfiler(
        start_time=0.0,
        events=[("first", 0.5), ("second", 0.8)],
    )

    with caplog.at_level(logging.INFO, logger="ui_qt.startup_profiler"):
        profiler.log_summary()

    assert "Startup timing summary:" in caplog.text
    assert "first" in caplog.text
    assert "total=  0.500s" in caplog.text
    assert "delta=  0.500s" in caplog.text
    assert "second" in caplog.text
    assert "total=  0.800s" in caplog.text
    assert "delta=  0.300s" in caplog.text
