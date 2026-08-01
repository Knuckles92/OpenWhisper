"""Generate the multi-resolution application icon.

Renders the mark from ``ui_qt.utils.app_icon`` at every size Windows asks for
and packs them into ``ui_qt/assets/openwhisper.ico``, which is used for the
executable, the installer, the taskbar, and the system tray.

Run after changing the artwork:

    python scripts/generate_icon.py

Pillow is deliberately not used — ICO is a thin container around PNG data, so
assembling it here keeps the icon out of the dependency list.
"""

import os
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Qt needs a platform plugin even for offscreen rendering.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402

from ui_qt.utils.app_icon import ICON_SIZES, render_app_pixmap  # noqa: E402

OUTPUT_PATH = REPO_ROOT / "ui_qt" / "assets" / "openwhisper.ico"


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


# Held for the module lifetime: Qt objects must not outlive the application.
_app = None


def main() -> int:
    global _app
    _app = QGuiApplication.instance() or QGuiApplication([])

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_bytes(build_ico())

    kib = OUTPUT_PATH.stat().st_size / 1024
    print(f"Wrote {OUTPUT_PATH.relative_to(REPO_ROOT)} "
          f"({kib:.1f} KiB, sizes: {', '.join(str(s) for s in ICON_SIZES)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
