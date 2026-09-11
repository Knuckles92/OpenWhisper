"""Default recorder output must never touch files in the caller's directory."""
import os
from pathlib import Path
import subprocess
import sys


def test_default_recorder_test_preserves_preexisting_recording(tmp_path):
    sentinel = tmp_path/"recorded_audio.wav"
    sentinel.write_bytes(b"existing recording")
    test = Path(__file__).with_name("test_recorder.py")
    result = subprocess.run([sys.executable, "-m", "pytest",
        str(test)+"::TestAudioRecorder::test_start_recording",
        str(test)+"::TestAudioRecorder::test_save_recording_default_filename",
        "-q", "-p", "no:cacheprovider"], cwd=tmp_path, capture_output=True, text=True,
        timeout=45, env={**os.environ, "PYTHONIOENCODING":"utf-8"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert sentinel.read_bytes() == b"existing recording"
