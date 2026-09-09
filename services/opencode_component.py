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

from services.opencode_catalog import SDK_VERSION, BUN_VERSION

_REQUIRED = (
    "bun.exe", "main.mjs", "self-test.mjs", "payload.json",
    "node_modules/@opencode/sdk/package.json",
    "node_modules/@opencode/core/package.json",
    "node_modules/@opencode/plugin/package.json",
    "THIRD_PARTY_NOTICES.txt",
)


def isolated_environment(source: dict[str, str], root: str) -> dict[str, str]:
    # No ambient provider keys, Bun preloads, OpenCode plugins or CLI settings.
    allowed = {
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
        "OPENWHISPER_SIDECAR_TOKEN", "OPENWHISPER_LLM_API_KEY",
        "OPENWHISPER_LLM_BASE_URL", "PI_MODEL",
    }
    env = {k: v for k, v in source.items() if k.upper() in allowed}
    env.update(OPENWHISPER_OPENCODE_ROOT=root, OPENCODE_CONFIG_DIR=root,
               OPENCODE_TEST_HOME=root, HOME=root, USERPROFILE=root)
    for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "TMP", "TEMP"):
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
        if any(not (path / name).is_file() or (path / name).is_symlink() for name in _REQUIRED):
            return False
        _metadata(path)
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def payload_dir() -> str | None:
    from services import components as c
    if c.current_platform_tag() != "win_amd64":
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


def validate_payload(target_dir: str) -> None:
    from services.components import ComponentError
    root = Path(target_dir).resolve()
    if not runnable(str(root)):
        raise ComponentError("The OpenCode component is incomplete or has incompatible SDK versions.")
    try:
        metadata = _metadata(root)
        files = metadata["files"]
        if not isinstance(files, dict) or not set(_REQUIRED).difference({"payload.json"}).issubset(files):
            raise ValueError("Incomplete payload inventory")
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
                [str(root / "bun.exe"), "--version"], env=env, cwd=temporary,
                capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
            if version.returncode or version.stdout.strip() != BUN_VERSION:
                raise ValueError("Unexpected Bun runtime")
            tested = subprocess.run(
                [str(root / "bun.exe"), "--no-install", str(root / "self-test.mjs")],
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
