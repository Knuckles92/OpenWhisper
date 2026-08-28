"""Serve a built release as a stand-in for GitHub ``/releases/latest``.

Lets an installed OpenWhisper update to a build that has not been published,
so the native update path can be soaked before it ships::

    python scripts\\serve_update_feed.py            # serves installer\\Output
    python scripts\\serve_update_feed.py --dir D:\\some\\dir --port 8765

Then, in a second shell, start the *installed* app with the feed override::

    $env:OPENWHISPER_UPDATE_FEED_URL = "http://127.0.0.1:8765/releases/latest"
    & "$env:LOCALAPPDATA\\Programs\\OpenWhisper\\OpenWhisper.exe"

Help -> Check for Updates now offers the served build. Digests, manifests,
and the commit itself run exactly as they do against GitHub.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.update_contract import (  # noqa: E402
    archive_asset_name,
    setup_asset_name,
)

_SETUP_PATTERN = re.compile(r"^OpenWhisper-Setup-(\d+\.\d+\.\d+)\.exe$")
_CHUNK = 1 << 20


def discover_version(directory: Path) -> str:
    versions = sorted(
        {
            match.group(1)
            for path in directory.glob("OpenWhisper-Setup-*.exe")
            if (match := _SETUP_PATTERN.match(path.name))
        }
    )
    if len(versions) != 1:
        raise SystemExit(
            f"Expected exactly one OpenWhisper-Setup-<version>.exe in {directory}, "
            f"found {versions or 'none'}. Pass --version."
        )
    return versions[0]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def build_payload(directory: Path, version: str, base_url: str, notes: str) -> dict:
    assets = []
    for name in (setup_asset_name(version), archive_asset_name(version)):
        path = directory / name
        if not path.is_file():
            continue
        assets.append(
            {
                "name": name,
                "size": path.stat().st_size,
                "state": "uploaded",
                "digest": f"sha256:{sha256_of(path)}",
                "browser_download_url": f"{base_url}/download/{name}",
            }
        )
    if not assets:
        raise SystemExit(f"No release assets for {version} in {directory}.")
    return {
        "tag_name": f"v{version}",
        "name": f"OpenWhisper {version}",
        "html_url": f"{base_url}/releases/tag/v{version}",
        "body": notes,
        "draft": False,
        "prerelease": False,
        "assets": assets,
    }


class _Handler(BaseHTTPRequestHandler):
    payload: dict = {}
    directory: Path = Path(".")

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/releases/latest":
            body = json.dumps(self.payload, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/download/"):
            name = self.path[len("/download/"):]
            if name in {asset["name"] for asset in self.payload["assets"]}:
                self._send_file(self.directory / name)
                return
        self.send_error(404)

    def _send_file(self, path: Path) -> None:
        total = path.stat().st_size
        start = 0
        range_header = self.headers.get("Range", "")
        match = re.match(r"^bytes=(\d+)-$", range_header)
        if match and int(match.group(1)) < total:
            start = int(match.group(1))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(total - start))
        self.end_headers()
        with open(path, "rb") as handle:
            handle.seek(start)
            for block in iter(lambda: handle.read(_CHUNK), b""):
                self.wfile.write(block)

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401 - http.server API
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dir",
        default=str(REPO_ROOT / "installer" / "Output"),
        help="directory holding the built setup exe and/or tar.xz",
    )
    parser.add_argument("--version", help="release version; discovered from the setup exe by default")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--notes", default="Local soak build.", help="release body shown in the prompt")
    args = parser.parse_args(argv)

    directory = Path(args.dir).resolve()
    version = args.version or discover_version(directory)
    base_url = f"http://{args.host}:{args.port}"
    _Handler.payload = build_payload(directory, version, base_url, args.notes)
    _Handler.directory = directory

    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"Serving OpenWhisper {version} from {directory}")
    for asset in _Handler.payload["assets"]:
        print(f"  {asset['name']}  {asset['size']} bytes  {asset['digest']}")
    print()
    print("Start the installed app with the feed override, then Help -> Check for Updates:")
    print(f'  $env:OPENWHISPER_UPDATE_FEED_URL = "{base_url}/releases/latest"')
    print('  & "$env:LOCALAPPDATA\\Programs\\OpenWhisper\\OpenWhisper.exe"')
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
