#!/usr/bin/env python3
"""Fail quickly when the pinned ASGI test stack cannot start TestClient.

The actual smoke check runs in a child process so a broken blocking portal cannot
stall an installer job until the workflow-level timeout expires.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import textwrap


_SMOKE_PROGRAM = textwrap.dedent(
    """
    import importlib.metadata

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    for package in ("fastapi", "starlette", "anyio", "httpx"):
        print(f"{package}={importlib.metadata.version(package)}", flush=True)

    app = FastAPI()

    @app.get("/__testclient_smoke__")
    def smoke():
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/__testclient_smoke__")
        response.raise_for_status()
        assert response.json() == {"ok": True}

    print("TestClient smoke passed", flush=True)
    """
)


def run_smoke(timeout_seconds: float = 15.0) -> int:
    command = [sys.executable, "-c", _SMOKE_PROGRAM]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        if exc.stdout:
            print(exc.stdout, end="", file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        print(
            f"TestClient smoke timed out after {timeout_seconds:g}s; "
            "check the pinned fastapi/starlette/anyio/httpx tuple.",
            file=sys.stderr,
        )
        return 124

    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode:
        print(
            "TestClient smoke failed; check the pinned "
            "fastapi/starlette/anyio/httpx tuple.",
            file=sys.stderr,
        )
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Maximum seconds allowed for the isolated smoke process (default: 15)",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return run_smoke(args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
