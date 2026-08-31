"""Generate the application icons used by native packages.

Renders the mark from ``ui_qt.utils.app_icon`` at every size Windows asks for,
packs them into ``ui_qt/assets/openwhisper.ico``, and writes the 256 px PNG used
by the Linux desktop entry. On macOS, also builds a Retina ``.icns`` under
``build/macos/`` via ``iconutil`` for the app-bundle freeze.

Run after changing the artwork:

    python scripts/generate_icon.py

Pillow is deliberately not used — ICO is a thin container around PNG data, so
assembling it here keeps the icon out of the dependency list.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Qt needs a platform plugin even for offscreen rendering.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402

from ui_qt.utils.app_icon import ICON_SIZES, render_app_pixmap  # noqa: E402

ICO_OUTPUT_PATH = REPO_ROOT / "ui_qt" / "assets" / "openwhisper.ico"
PNG_OUTPUT_PATH = REPO_ROOT / "ui_qt" / "assets" / "openwhisper.png"
ICNS_OUTPUT_PATH = REPO_ROOT / "build" / "macos" / "openwhisper.icns"
ICONSET_DIR = REPO_ROOT / "build" / "macos" / "OpenWhisper.iconset"

# iconutil expects named 1x/2x slots. Render the logical size for 1x entries and
# twice that size for @2x entries so Dock/Finder get a Retina mark through 1024.
ICNS_BASE_SIZES = (16, 32, 128, 256, 512)


def _png_bytes(size: int) -> bytes:
    """Render the mark at ``size`` and return it as PNG data."""
    # The QByteArray needs a named reference: QBuffer does not take ownership,
    # and a temporary is collected out from under it (segfault on save).
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    saved = render_app_pixmap(size).save(buffer, "PNG")
    buffer.close()
    if not saved:
        raise RuntimeError(f"Failed to encode the {size}px icon as PNG")
    return bytes(data)


def build_ico(sizes=ICON_SIZES) -> bytes:
    """Pack PNG renderings into an ICO container.

    Every entry is PNG-compressed, which Windows has supported since Vista and
    which keeps the 256px entry from dominating the file size.
    """
    images = [(size, _png_bytes(size)) for size in sizes]

    # ICONDIR: reserved, type (1 = icon), image count.
    header = struct.pack("<HHH", 0, 1, len(images))
    # Each ICONDIRENTRY is 16 bytes and follows the 6-byte header.
    offset = len(header) + 16 * len(images)

    directory, payload = b"", b""
    for size, data in images:
        directory += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,  # 0 encodes 256
            size if size < 256 else 0,
            0,  # palette size (0 = truecolor)
            0,  # reserved
            1,  # color planes
            32,  # bits per pixel
            len(data),
            offset,
        )
        payload += data
        offset += len(data)

    return header + directory + payload


def build_icns(output_path: Path = ICNS_OUTPUT_PATH) -> Path:
    """Write a Retina ICNS under ``build/macos/`` using Apple's ``iconutil``.

    The ICNS is a build product, not a committed source asset. Callers must
    run this on Darwin before freezing ``OpenWhisper.app``.
    """
    if sys.platform != "darwin":
        raise RuntimeError("ICNS generation requires macOS iconutil")
    if shutil.which("iconutil") is None:
        raise RuntimeError("iconutil is required to build openwhisper.icns")

    if ICONSET_DIR.exists():
        shutil.rmtree(ICONSET_DIR)
    ICONSET_DIR.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    for base in ICNS_BASE_SIZES:
        (ICONSET_DIR / f"icon_{base}x{base}.png").write_bytes(_png_bytes(base))
        retina = base * 2
        (ICONSET_DIR / f"icon_{base}x{base}@2x.png").write_bytes(
            _png_bytes(retina)
        )

    completed = subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET_DIR), "-o", str(output_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            f"iconutil failed with exit {completed.returncode}"
            + (f": {detail}" if detail else "")
        )
    if not output_path.is_file() or output_path.stat().st_size < 16:
        raise RuntimeError(f"iconutil did not write a usable ICNS at {output_path}")
    return output_path


# Held for the module lifetime: Qt objects must not outlive the application.
_app = None


def main() -> int:
    global _app
    _app = QGuiApplication.instance() or QGuiApplication([])

    ICO_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ICO_OUTPUT_PATH.write_bytes(build_ico())
    PNG_OUTPUT_PATH.write_bytes(_png_bytes(256))

    ico_kib = ICO_OUTPUT_PATH.stat().st_size / 1024
    png_kib = PNG_OUTPUT_PATH.stat().st_size / 1024
    print(
        f"Wrote {ICO_OUTPUT_PATH.relative_to(REPO_ROOT)} "
        f"({ico_kib:.1f} KiB, sizes: {', '.join(str(s) for s in ICON_SIZES)})"
    )
    print(
        f"Wrote {PNG_OUTPUT_PATH.relative_to(REPO_ROOT)} "
        f"({png_kib:.1f} KiB, 256x256)"
    )

    if sys.platform == "darwin":
        icns_path = build_icns()
        icns_kib = icns_path.stat().st_size / 1024
        print(
            f"Wrote {icns_path.relative_to(REPO_ROOT)} "
            f"({icns_kib:.1f} KiB, Retina slots through 1024)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
