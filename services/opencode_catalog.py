"""Immutable release metadata for the separately installed OpenCode harness."""
SDK_VERSION = "2.0.18"
BUN_VERSION = "1.3.14"
COMPONENT_VERSION = "bun1.3.14-opencode2.0.18-1"
RELEASE_TAG = "component-opencode-2.0.18-1"
RELEASE_URL = f"https://github.com/Knuckles92/OpenWhisper/releases/download/{RELEASE_TAG}/"

# Copied from the .catalog.json that scripts/build_component.py meeting-agent-opencode
# writes next to each archive. A platform is offered in Downloads only once its archive
# is uploaded under RELEASE_TAG, downloaded back, and matches these pins.
_PLACEHOLDER = {"published": False, "sha256": "0" * 64, "size_bytes": 0, "install_bytes": 0}
ARCHIVES = {
    "win_amd64": dict(_PLACEHOLDER),
    "linux_x86_64": dict(_PLACEHOLDER),
    "linux_aarch64": dict(_PLACEHOLDER),
    "darwin_arm64": dict(_PLACEHOLDER),
}


def archive_name(platform: str) -> str:
    return f"meeting-agent-opencode-{platform}-{COMPONENT_VERSION}.zip"


def catalog_entry() -> dict:
    return {"platforms": {
        platform: {
            "published": pins["published"],
            "version": COMPONENT_VERSION,
            "component_api": 1,
            "platform": platform,
            "install_bytes": pins["install_bytes"],
            "archives": ({
                "name": archive_name(platform),
                "url": RELEASE_URL + archive_name(platform),
                "sha256": pins["sha256"],
                "size_bytes": pins["size_bytes"],
                "extract": "zip",
            },),
        }
        for platform, pins in ARCHIVES.items()
    }}
