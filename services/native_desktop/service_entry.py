"""Dedicated Qt-free entry point for the packaged native audio service."""
from __future__ import annotations

import multiprocessing
import os
import sys


def main():
    # Frozen multiprocessing must dispatch child workers before opening the pipe.
    multiprocessing.freeze_support()
    os.environ["OPENWHISPER_DESKTOP_RUNTIME"] = "native"
    from services.native_desktop.bootstrap import reserve_output

    output = reserve_output()
    from services import native_libraries

    native_libraries.register_cuda_dll_directories()
    native_libraries.preload_cuda_libraries()
    try:
        from services.components import prune_orphans
        from services.component_runtime import activate_components

        prune_orphans()
        activate_components()
    except Exception:
        # An absent optional engine must leave API/remote transcription usable.
        pass

    if "--self-test" in sys.argv:
        import importlib
        import json
        from pathlib import Path

        modules = ["services.application_controller", "services.native_desktop.commands",
                   "services.native_desktop.clipboard", "sounddevice", "numpy", "av",
                   "ctranslate2", "faster_whisper", "sqlalchemy.dialects.sqlite",
                   "uvicorn", "services.agent_mcp.app", "reportlab", "docx", "PIL",
                   "uharfbuzz", "bidi.algorithm", "bidi.mirror"]
        for module in modules:
            importlib.import_module(module)
        from config import bundle_root
        from services.http_tls import verified_context

        root = Path(bundle_root())
        for relative in ("services/whisper_models.json", "native/assets/fonts/Inter-Regular.ttf"):
            if not (root / relative).is_file():
                raise RuntimeError(f"Missing service asset: {relative}")
        if not verified_context().get_ca_certs():
            raise RuntimeError("The service has no HTTPS trust store")
        # Exercise the bundled fonts, shaping extension and writers outside the
        # checkout; merely importing ReportLab would miss missing freeze assets.
        import tempfile
        from docx import Document
        from pypdf import PdfReader
        from services.walkthrough.document import write_markdown_pdf
        from services.walkthrough.word import write_markdown_docx

        with tempfile.TemporaryDirectory(prefix="OpenWhisper export check ") as temporary:
            folder = Path(temporary)
            text = "# Native export check\n\ncafé · 你好 · مرحبا · नमस्ते"
            pdf = write_markdown_pdf(text, folder / "notes.pdf", folder=folder, title="Native export check")
            word = write_markdown_docx(text, folder / "notes.docx", folder=folder, title="Native export check")
            if "Native export check" not in PdfReader(pdf).pages[0].extract_text() or Document(word).paragraphs[0].text != "Native export check":
                raise RuntimeError("Native document exports could not be read back")
        if any(name == "PyQt6" or name.startswith("PyQt6.") for name in sys.modules):
            raise RuntimeError("The native service imported PyQt6")
        output.write(json.dumps({"ok": True, "runtime": "native", "qt_loaded": False}) + "\n")
        output.flush()
        return 0
    from services.native_desktop.bootstrap import main as serve

    return serve(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
