"""Explicit headless launch; no microphone, Qt, speech model, or dashboard."""

import argparse
import os
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Serve the read-only OpenWhisper History API on 127.0.0.1."
    )
    parser.add_argument(
        "--database",
        help="Existing database path; defaults to OpenWhisper's data directory.",
    )
    parser.add_argument(
        "--port", type=int, default=8766, help="Local TCP port (default: 8766)."
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    token = os.environ.get("OPENWHISPER_API_TOKEN", "")
    if not token:
        parser.error(
            "Set OPENWHISPER_API_TOKEN to a randomly generated token of at least 32 characters."
        )

    import uvicorn

    from config import config
    from services.agent_api.app import create_app

    try:
        app = create_app(args.database or config.DATABASE_FILE, token)
    except Exception as exc:
        # SQLAlchemy errors include filesystem paths and queries; keep startup
        # diagnostics useful without dumping the database connection details.
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else "Could not open the existing database. Open OpenWhisper first and verify --database."
        )
        print(f"History API: {message}", file=sys.stderr)
        return 1
    uvicorn.run(
        app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False
    )
    return 0
