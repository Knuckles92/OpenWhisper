"""Resolve and validate the managed, offline-capable OpenCode component."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
import subprocess
import tempfile

from services.opencode_catalog import ARCHIVES, SDK_VERSION, BUN_VERSION

#: Platforms with a Bun build and prebuilt native SDK dependencies.
SUPPORTED_PLATFORMS = tuple(ARCHIVES)

# No ambient provider keys, Bun preloads, OpenCode plugins, CLI settings, or loader paths
# (LD_PRELOAD / LD_LIBRARY_PATH from a frozen app must not reach the runtime).
_ALLOWED_ENV = {
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT",
    "LANG", "LC_ALL", "LC_CTYPE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "OPENWHISPER_SIDECAR_TOKEN", "OPENWHISPER_LLM_API_KEY",
    "OPENWHISPER_LLM_BASE_URL", "PI_MODEL",
}


def runtime_name(windows: bool | None = None) -> str:
    """The Bun executable's file name in the payload for this (or the given) OS."""
    return "bun.exe" if (os.name == "nt" if windows is None else windows) else "bun"


def _required() -> tuple[str, ...]:
    return (
        runtime_name(), "main.mjs", "self-test.mjs", "payload.json",
        "node_modules/@opencode/sdk/package.json",
        "node_modules/@opencode/core/package.json",
        "node_modules/@opencode/plugin/package.json",
        "THIRD_PARTY_NOTICES.txt",
    )


def isolated_environment(source: dict[str, str], root: str) -> dict[str, str]:
    env = {k: v for k, v in source.items() if k.upper() in _ALLOWED_ENV}
    env.update(OPENWHISPER_OPENCODE_ROOT=root, OPENCODE_CONFIG_DIR=root,
               OPENCODE_TEST_HOME=root, HOME=root, USERPROFILE=root)
    for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                "TMP", "TEMP", "TMPDIR"):
        directory = os.path.join(root, key.lower())
        os.makedirs(directory, exist_ok=True)
        env[key] = directory
    return env


def _metadata(root: Path) -> dict:
    metadata = json.loads((root / "payload.json").read_text(encoding="utf-8"))
    if (metadata.get("schema"), metadata.get("sdk_version"), metadata.get("bun_version")) != (1, SDK_VERSION, BUN_VERSION):
        raise ValueError("OpenCode payload version does not match this app")
    for package in ("sdk", "core", "plugin"):
        data = json.loads((root / f"node_modules/@opencode/{package}/package.json").read_text(encoding="utf-8"))
        if data.get("version") != SDK_VERSION:
            raise ValueError("OpenCode SDK packages have mixed versions")
    return metadata


def runnable(root: str) -> bool:
    try:
        path = Path(root)
        if any(not (path / name).is_file() or (path / name).is_symlink() for name in _required()):
            return False
        if os.name != "nt" and not os.access(path / runtime_name(), os.X_OK):
            return False
        _metadata(path)
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def payload_dir() -> str | None:
    from services import components as c
    if c.current_platform_tag() not in SUPPORTED_PLATFORMS:
        return None
    component_id = c.ComponentId.MEETING_AGENT_OPENCODE
    if c.is_installed(component_id):
        manifest = c.read_manifest(component_id)
        root = c.component_dir(component_id)
        if manifest and not c.check_compatibility(manifest) and runnable(root):
            return root
        # An installed but broken payload is an error; do not mask it with a dev build.
        return None
    if not c.is_frozen():
        source = os.path.join(c.bundle_root(), "sidecar-opencode", "dist")
        if runnable(source):
            return source
    return None


def restore_executable_bits(root: Path, metadata: dict, windows: bool | None = None) -> None:
    """Zip extraction drops POSIX modes; re-mark the inventoried executables."""
    if os.name == "nt" if windows is None else windows:
        return
    executables = metadata.get("executables")
    if not isinstance(executables, list) or runtime_name(False) not in executables:
        raise ValueError("Payload does not declare its runtime executable")
    files = metadata["files"]
    for name in executables:
        path = root / name
        if name not in files or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Invalid executable inventory path")
        path.chmod(0o755)


def validate_payload(target_dir: str) -> None:
    from services.components import ComponentError, current_platform_tag
    root = Path(target_dir).resolve()
    try:
        metadata = _metadata(root)
        if metadata.get("platform") != current_platform_tag():
            raise ValueError("OpenCode payload was built for another platform")
        files = metadata["files"]
        if not isinstance(files, dict) or not set(_required()).difference({"payload.json"}).issubset(files):
            raise ValueError("Incomplete payload inventory")
        restore_executable_bits(root, metadata)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ComponentError("The OpenCode component is incomplete or has incompatible SDK versions.") from exc
    if not runnable(str(root)):
        raise ComponentError("The OpenCode component is incomplete or has incompatible SDK versions.")
    runtime = str(root / runtime_name())
    try:
        for name, expected in files.items():
            path = root / name
            if not path.resolve().is_relative_to(root) or path.is_symlink() or not path.is_file():
                raise ValueError("Invalid payload inventory path")
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            if digest != expected:
                raise ValueError("Payload inventory checksum mismatch")
        with tempfile.TemporaryDirectory(prefix="openwhisper-opencode-verify-") as temporary:
            env = isolated_environment(os.environ, temporary)
            # The installer self-test may contact only its own loopback mock.
            for key in list(env):
                if key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                   "OPENWHISPER_LLM_API_KEY", "OPENWHISPER_LLM_BASE_URL", "PI_MODEL"}:
                    del env[key]
            env["NO_PROXY"] = "127.0.0.1,localhost"
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            version = subprocess.run(
                [runtime, "--version"], env=env, cwd=temporary,
                capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
            if version.returncode or version.stdout.strip() != BUN_VERSION:
                raise ValueError("Unexpected Bun runtime")
            tested = subprocess.run(
                [runtime, "--no-install", str(root / "self-test.mjs")],
                env=env, cwd=temporary, capture_output=True, text=True, timeout=45,
                stdin=subprocess.DEVNULL, creationflags=flags,
            )
            if tested.returncode or tested.stdout.strip() != "OPENWHISPER_OPENCODE_OK":
                raise ValueError("Offline SDK self-test failed")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise ComponentError("OpenCode component verification failed. Reinstall it from Downloads.") from exc


def clean_stale_runtime_dirs() -> None:
    """Remove only marked SDK roots whose owning OpenWhisper process has exited."""
    from meeting.recovery import _pid_alive
    temporary = Path(tempfile.gettempdir()).resolve()
    for root in temporary.glob("openwhisper-opencode-*"):
        if not re.fullmatch(r"openwhisper-opencode-\d+-[a-z0-9_]+", root.name):
            continue
        try:
            if root.is_symlink() or root.resolve().parent != temporary:
                continue
            marker = json.loads((root / ".openwhisper-owner.json").read_text(encoding="utf-8"))
            pid = int(root.name.split("-")[2])
            if marker != {"application": "OpenWhisper", "pid": pid} or _pid_alive(pid):
                continue
            shutil.rmtree(root)
        except (OSError, ValueError, TypeError):
            # A scanner, another reaper, or an unfinished creation may own the path.
            continue
