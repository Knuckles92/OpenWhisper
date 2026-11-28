"""Build and validate OpenWhisper-<version>-win64.tar.xz from dist\\OpenWhisper."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.app_update_apply import (  # noqa: E402
    UpdateApplyError,
    load_manifest,
    pack_tar_xz,
    read_dist_edition,
    read_manifest_from_tar_xz,
    safe_extract_tar_xz,
    validate_manifest,
    verify_tree_against_manifest,
    write_manifest_file,
)
from services.update_contract import (  # noqa: E402
    APP_EXE_NAME,
    EDITION_ENV_VAR,
    MANIFEST_NAME,
    MINIMUM_UPDATER_VERSION,
    UPDATER_EXE_NAME,
    VALID_EDITIONS,
    archive_asset_name,
    normalize_edition,
    parse_strict_version,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True, help="Path to dist/OpenWhisper")
    parser.add_argument("--output", required=True, help="Destination tar.xz path")
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--edition",
        choices=VALID_EDITIONS,
        default=None,
        help="Edition to pack (defaults to OPENWHISPER_EDITION or dist edition.json)",
    )
    args = parser.parse_args()

    version = args.version
    if parse_strict_version(version) < parse_strict_version(
        MINIMUM_UPDATER_VERSION
    ):
        raise SystemExit(
            f"Native updater bootstrap must be {MINIMUM_UPDATER_VERSION} or newer."
        )
    dist = Path(args.dist)
    if not (dist / APP_EXE_NAME).is_file():
        raise SystemExit(f"Missing {APP_EXE_NAME} in {dist}")
    if not (dist / UPDATER_EXE_NAME).is_file():
        raise SystemExit(f"Missing {UPDATER_EXE_NAME} in {dist}")

    if args.edition is not None:
        edition = normalize_edition(args.edition)
    elif os.environ.get(EDITION_ENV_VAR, "").strip():
        edition = normalize_edition(os.environ.get(EDITION_ENV_VAR))
    else:
        edition = read_dist_edition(str(dist))

    write_manifest_file(str(dist), version, edition=edition)
    expected = archive_asset_name(version, edition=edition)
    output = Path(args.output)
    if output.name != expected:
        raise SystemExit(f"Output must be named {expected}, got {output.name}")
    if output.exists():
        output.unlink()
    pack_tar_xz(str(dist), str(output))

    import tempfile

    with tempfile.TemporaryDirectory(prefix="ow-archive-check-") as tmp:
        packed_manifest = read_manifest_from_tar_xz(str(output))
        validate_manifest(
            packed_manifest, expected_version=version, expected_edition=edition
        )
        safe_extract_tar_xz(str(output), tmp, manifest=packed_manifest)
        manifest = load_manifest(os.path.join(tmp, MANIFEST_NAME))
        if manifest != packed_manifest:
            raise UpdateApplyError("Packed manifest changed during extraction.")
        validate_manifest(
            manifest, expected_version=version, expected_edition=edition
        )
        verify_tree_against_manifest(tmp, manifest)
    print(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (UpdateApplyError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
