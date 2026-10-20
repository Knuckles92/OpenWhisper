"""Portable benchmark configuration; no installs or downloads."""
import hashlib
import os
from pathlib import Path
import subprocess
import wave


def add_common(parser, *, host=True):
    parser.add_argument('--audio', type=Path, required=True,
                        help='Existing 16kHz mono PCM16 WAV; never downloaded automatically')
    parser.add_argument('--output', type=Path, required=True, help='JSON result file')
    parser.add_argument('--ffmpeg', default='ffmpeg', help='FFmpeg executable or absolute path')
    parser.add_argument('--opus-library', help='Optional explicit installed libopus shared-library path')
    if host:
        parser.add_argument('--source-root', type=Path, required=True, help='OpenWhisper source worktree')
        parser.add_argument('--expected-revision', required=True,
                            help='Required git commit/ref; run aborts if worktree HEAD differs or tracked files are dirty')


def configure(parser, args, *, host=True):
    args.audio = args.audio.expanduser().resolve()
    if not args.audio.is_file():
        parser.error('--audio must name an existing local WAV')
    with wave.open(str(args.audio)) as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (16000, 1, 2, 'NONE'):
            parser.error('--audio must be uncompressed 16kHz mono PCM16 WAV')
        if audio.getnframes() == 0:
            parser.error('--audio must not be empty')
    args.audio_sha256 = hashlib.sha256(args.audio.read_bytes()).hexdigest()
    args.output = args.output.expanduser().resolve()
    if args.output.exists():
        parser.error('--output already exists; choose a new path to preserve prior evidence')
    if host:
        args.source_root = str(args.source_root.expanduser().resolve())
        def git(*parts):
            return subprocess.check_output(['git', '-C', args.source_root, *parts], text=True).strip()
        try:
            args.source_revision = git('rev-parse', 'HEAD')
            expected = git('rev-parse', args.expected_revision + '^{commit}')
            dirty = git('diff', 'HEAD', '--name-only')
        except (OSError, subprocess.CalledProcessError) as exc:
            parser.error(f'Cannot validate source revision: {exc}')
        if args.source_revision != expected or dirty:
            parser.error('Source HEAD does not match --expected-revision, or tracked files are dirty')
        if not (Path(args.source_root)/'services/remote_asr/host.py').is_file():
            parser.error('--source-root is not an OpenWhisper source tree')
    if args.opus_library:
        os.environ['OW_BENCH_OPUS_LIBRARY'] = str(Path(args.opus_library).expanduser().resolve())
    os.environ['OW_BENCH_FFMPEG'] = args.ffmpeg
    if hasattr(args,"reps") and args.reps < 1:
        parser.error("--reps must be positive")
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost,127.0.0.1,::1"
    os.environ["ORT_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    import loopback_guard  # noqa: F401 - installs network isolation before app imports.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    return args
