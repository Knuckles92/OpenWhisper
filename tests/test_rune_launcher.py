from pathlib import Path
from unittest.mock import Mock

from services import rune_launcher


def test_launcher_passes_the_current_python_and_forwards_preview_arguments(monkeypatch):
    build = Mock(return_value=Mock(returncode=0))
    run = Mock(return_value=0)
    monkeypatch.setattr(rune_launcher.shutil, "which", lambda name: "/tools/cargo")
    monkeypatch.setattr(rune_launcher.subprocess, "run", build)
    monkeypatch.setattr(rune_launcher.subprocess, "call", run)
    monkeypatch.setenv("CARGO_TARGET_DIR", "custom-target")
    assert rune_launcher.main(["--demo"]) == 0
    assert "--locked" in build.call_args.args[0]
    arguments = run.call_args.args[0]
    name = "openwhisper-rune.exe" if rune_launcher.os.name == "nt" else "openwhisper-rune"
    assert Path(arguments[0]).parts[-3:] == ("custom-target", "debug", name)
    assert arguments[1:] == ["--demo"]
    assert run.call_args.kwargs["env"]["OPENWHISPER_PYTHON"] == rune_launcher.sys.executable


def test_build_failure_does_not_launch_a_stale_binary(monkeypatch):
    run = Mock()
    monkeypatch.setattr(rune_launcher.shutil, "which", lambda name: "/tools/cargo")
    monkeypatch.setattr(rune_launcher.subprocess, "run", lambda *a, **k: Mock(returncode=17))
    monkeypatch.setattr(rune_launcher.subprocess, "call", run)
    assert rune_launcher.main([]) == 17
    run.assert_not_called()


def test_missing_toolchain_explains_how_to_start(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(rune_launcher.shutil, "which", lambda name: None)
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path))
    assert rune_launcher.main([]) == 1
    assert "Install Rust with Cargo" in capsys.readouterr().err
