"""Immutable release metadata for the separately installed OpenCode harness."""
SDK_VERSION = "0.0.0-dev-19291"
BUN_VERSION = "1.3.14"
COMPONENT_VERSION = "bun1.3.14-opencode19291-1"
RELEASE_TAG = "component-opencode-19291-1"


def catalog_entry() -> dict:
    return {"platforms": {"win_amd64": {
        # Staged artifact: enable after the immutable release asset is published and verified.
        "published": False,
        "version": COMPONENT_VERSION,
        "component_api": 1,
        "platform": "win_amd64",
        "install_bytes": 527_680_768,
        "archives": ({
            "name": f"meeting-agent-opencode-win_amd64-{COMPONENT_VERSION}.zip",
            "url": (
                "https://github.com/Knuckles92/OpenWhisper/releases/download/"
                f"{RELEASE_TAG}/meeting-agent-opencode-win_amd64-{COMPONENT_VERSION}.zip"
            ),
            "sha256": "b9b7669ad1985ff3e9b1a88ae107648b526ec52b77bcc8bc2576cc8a2fc00377",
            "size_bytes": 141_497_663,
            "extract": "zip",
        },),
    }}}
