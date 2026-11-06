"""Word document exports selected for the active desktop runtime."""
from __future__ import annotations

import os
import sys

if os.environ.get("OPENWHISPER_DESKTOP_RUNTIME") == "native":
    from services.walkthrough.portable_document import write_docx, write_markdown_docx
    __all__ = ["write_docx", "write_markdown_docx"]
else:
    from services.walkthrough import word_qt
    sys.modules[__name__] = word_qt
