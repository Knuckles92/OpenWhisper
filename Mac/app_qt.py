"""Entrypoint for the OpenWhisper Qt application (macOS build)."""

import sys
import warnings

warnings.filterwarnings("ignore", message="pkg_resources is deprecated")

from ui_qt.bootstrap import main

__all__ = ["main"]


if __name__ == "__main__":
    sys.exit(main())