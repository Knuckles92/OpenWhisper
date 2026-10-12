"""Exercise the worker with app packages beside its source, as in a frozen build."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def packaged_worker(tmp_path):
    root = Path(__file__).resolve().parents[1]
    app = tmp_path / "app" / "_internal"
    worker_dir = app / "services" / "local_asr"
    worker_dir.mkdir(parents=True)
    for relative in (
        "services/__init__.py", "services/local_asr/__init__.py",
        "services/local_asr/languages.py", "services/local_asr/worker.py",
    ):
        shutil.copyfile(root / relative, app / relative)

    runtime = tmp_path / "asr-qwen"
    runtime.mkdir()
    for folder, version in ((app, "0.23.1"), (runtime, "0.22.2")):
        package = folder / "tokenizers"
        package.mkdir()
        (package / "__init__.py").write_text(f"__version__ = {version!r}\n", encoding="utf-8")
        metadata = folder / f"tokenizers-{version}.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: tokenizers\nVersion: {version}\n", encoding="utf-8",
        )
    return worker_dir / "worker.py", runtime


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_packaged_worker_loads_qwen_with_runtime_tokenizers(packaged_worker, tmp_path, device):
    worker, runtime = packaged_worker
    (runtime / "torch.py").write_text("""
from types import SimpleNamespace
float16 = 'f16'
float32 = 'f32'
cuda = SimpleNamespace(is_available=lambda: True,
    get_device_capability=lambda index: (12, 0), get_arch_list=lambda: ['sm_120'])
""", encoding="utf-8")
    (runtime / "qwen_asr.py").write_text("""
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
import tokenizers
from services.local_asr.languages import qwen_language_name

# Transformers checks distribution metadata; the imported package must agree.
if version('tokenizers') != '0.22.2':
    raise ImportError('Incompatible tokenizers: ' + version('tokenizers'))
assert tokenizers.__version__ == '0.22.2'
assert Path(tokenizers.__file__).parent.parent == Path(__file__).parent
assert qwen_language_name('en') == 'English'

class Qwen3ASRModel:
    @classmethod
    def from_pretrained(cls, *args, **kwargs):
        return SimpleNamespace(model=SimpleNamespace(generate=lambda *a, **k: None))
""", encoding="utf-8")
    bootstrap = """
from pathlib import Path
import runpy, sys
sys.path.insert(0, sys.argv[2])
sys.path.insert(0, str(Path(sys.argv[1]).parent))
runpy.run_path(sys.argv[1], run_name='__main__')
"""
    requests = [
        dict(id=1, op="load", backend="qwen_asr", device=device,
             runtime=str(runtime), model_path="fixture"),
        dict(id=2, op="shutdown"),
    ]
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", bootstrap, str(worker), str(runtime)],
        cwd=tmp_path, input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True, text=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert responses == [{"id": 1, "result": {"device": device}}], result.stderr


def test_worker_directory_does_not_shadow_runtime_modules(packaged_worker, tmp_path):
    worker, runtime = packaged_worker
    for name in ("mlx", "nvidia"):
        (worker.parent / f"{name}.py").write_text(
            "raise ImportError('App adapter shadowed the runtime package')\n", encoding="utf-8",
        )
        package = runtime / name
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
    bootstrap = """
from pathlib import Path
import runpy, sys
sys.path.insert(0, sys.argv[2])
sys.path.insert(0, str(Path(sys.argv[1]).parent))
runpy.run_path(sys.argv[1])
import mlx, nvidia
for module in (mlx, nvidia):
    assert Path(module.__file__).parent.parent == Path(sys.argv[2])
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", bootstrap, str(worker), str(runtime)],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
