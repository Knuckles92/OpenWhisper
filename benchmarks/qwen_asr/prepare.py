"""Prepare app-identical float32 windows from a local labeled WAV corpus."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.local_asr.audio import windows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--groups", nargs="+", default=[])
    parser.add_argument("--clip-ids", nargs="+", default=[])
    args = parser.parse_args()
    source = json.loads(args.corpus_manifest.read_text(encoding="utf-8"))
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = dict(windows=[], source_manifest_sha256=hashlib.sha256(args.corpus_manifest.read_bytes()).hexdigest(),
                    sources=source.get("sources", {}))
    for clip in source["clips"]:
        if (args.groups or args.clip_ids) and clip.get("group") not in args.groups and clip["id"] not in args.clip_ids:
            continue
        for number, (offset, audio) in enumerate(windows(str(args.corpus_manifest.parent / clip["audio_path"]))):
            path = output / f"{len(manifest['windows']):06d}.f32"
            audio.tofile(path)
            manifest["windows"].append(dict(
                id=f"{clip['id']}-{number}", clip_id=clip["id"], offset_s=offset,
                duration_s=len(audio) / 16000, reference=clip.get("reference", ""),
                audio_path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            ))
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"{len(manifest['windows'])} windows: {output / 'manifest.json'}")


if __name__ == "__main__":
    main()
