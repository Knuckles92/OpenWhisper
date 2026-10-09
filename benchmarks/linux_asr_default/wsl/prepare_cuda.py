"""Stage the pinned production Linux CUDA libraries inside the benchmark."""
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
source = ROOT.parent/'source/services/components.py'
tree = ast.parse(source.read_text(encoding='utf-8'))
spec = next(ast.literal_eval(n.value) for n in tree.body
            if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
            and n.target.id == '_BUILTIN_GPU_ARCHIVES_LINUX')
cache = ROOT/'cuda-archives'
libs = ROOT/'cuda-lib'
cache.mkdir(exist_ok=True)
libs.mkdir(exist_ok=True)

def stage(archive):
    target = cache/archive['name']
    if not target.exists():
        print('DOWNLOAD',archive['name'],flush=True)
        urllib.request.urlretrieve(archive['url'],target)
    digest = hashlib.file_digest(target.open('rb'),'sha256').hexdigest()
    if target.stat().st_size != archive['size_bytes'] or digest != archive['sha256']:
        raise ValueError('Archive identity mismatch: '+archive['name'])
    with zipfile.ZipFile(target) as z:
        for entry in z.infolist():
            if '/lib/' in entry.filename and '.so' in Path(entry.filename).name:
                (libs/Path(entry.filename).name).write_bytes(z.read(entry))
    print('STAGED',archive['name'],flush=True)
    return archive

with ThreadPoolExecutor(max_workers=3) as pool:
    staged = list(pool.map(stage,spec))
(ROOT/'cuda-provenance.json').write_text(json.dumps(staged,indent=2),encoding='utf-8')
