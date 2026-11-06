# -*- mode: python ; coding: utf-8 -*-
"""Onedir audio service for Rune. Build with requirements-native, never Qt."""
# SPECPATH and the build classes below are supplied by PyInstaller's spec loader.
import os
import sys
from importlib.util import find_spec
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821
sys.path.insert(0, str(ROOT))
os.environ["OPENWHISPER_DESKTOP_RUNTIME"] = "native"

datas = []
for path in ("services/local_asr", "services/whisper_models.json", "native/assets/fonts",
             "integrations/omarchy", "webui/dist", "docs/linux-system-audio.md",
             "THIRD_PARTY_NOTICES.md"):
    source = ROOT / path
    if source.exists():
        datas.append((str(source), path if source.is_dir() else str(Path(path).parent)))
binaries = []
hidden = ["sqlalchemy.dialects.sqlite", "services.models", "huggingface_hub", "pypdf",
          "docx", "pptx", "openpyxl", "lxml", "markdown_it", "PIL.ImageGrab",
          "uharfbuzz", "bidi.algorithm", "bidi.mirror"]
hidden += collect_submodules("services.native_desktop.commands")
packages = ["ctranslate2", "faster_whisper", "onnxruntime", "av", "tokenizers",
            "sounddevice", "lxml", "uvicorn", "mcp", "mcp_types",
            "jsonschema_specifications", "soundcard", "keyring", "secretstorage",
            "jeepney", "pynput", "reportlab", "PIL"]
if sys.platform == "darwin":
    hidden += ["keyring.backends.macOS", "AppKit"]
    packages += ["objc", "Foundation", "Cocoa", "CoreMedia", "Quartz",
                 "ScreenCaptureKit", "ApplicationServices", "CoreText", "HIServices"]
elif sys.platform == "win32":
    hidden += ["keyring.backends.Windows", "keyboard", "win32clipboard", "pythoncom", "win32com.shell.shell"]
else:
    hidden += ["keyring.backends.SecretService", "Xlib.display", "Xlib.protocol.event"]
for package in packages:
    if find_spec(package) is not None:
        data, binary, imports = collect_all(package, filter_submodules=lambda name: not name.startswith("mcp.cli"))
        datas += data
        binaries += binary
        hidden += imports
for package in ("keyring", "mcp", "fastapi", "pydantic", "reportlab"):
    datas += copy_metadata(package)

a = Analysis([str(ROOT / "services/native_desktop/service_entry.py")],  # noqa: F821
             pathex=[str(ROOT)], binaries=binaries, datas=datas,
             hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=["PyQt6", "PyQt5", "PySide6", "PySide2", "tkinter", "ui_qt"], noarchive=False)
if any("pyqt" in str(item).lower() or "/qt6/" in str(item).lower().replace("\\", "/") for item in [*a.binaries, *a.datas]):
    raise RuntimeError("Qt assets leaked into the native service bundle")
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="openwhisper-service",  # noqa: F821
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, disable_windowed_traceback=False, argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="openwhisper-service")  # noqa: F821
