# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build specification for OpenWhisper (Windows, onedir).

Build with::

    pyinstaller --noconfirm --clean OpenWhisper.spec

onedir rather than onefile: onefile re-extracts the whole ~300 MB bundle to a
temp directory on every launch, which is slow and a reliable source of
antivirus false positives. Inno Setup packs the directory afterwards, so the
user still downloads a single file.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

REPO_ROOT = Path(SPECPATH).resolve()
sys.path.insert(0, str(REPO_ROOT))

from _version import __version__  # noqa: E402

APP_NAME = "OpenWhisper"
ICON_PATH = REPO_ROOT / "ui_qt" / "assets" / "openwhisper.ico"

# ---------------------------------------------------------------------------
# Bundled read-only assets
#
# Relative layout is preserved because config.bundle_root() resolves assets as
# <root>/ui_qt/styles/theme.qss, where <root> is sys._MEIPASS when frozen.
# ---------------------------------------------------------------------------
datas = [
    (str(REPO_ROOT / "ui_qt" / "styles" / "theme.qss"), "ui_qt/styles"),
    (str(REPO_ROOT / "ui_qt" / "assets"), "ui_qt/assets"),
    (str(REPO_ROOT / "webui" / "dist"), "webui/dist"),
]
binaries = []
hiddenimports = [
    # SQLAlchemy resolves dialects by name at runtime, so static analysis
    # cannot see this one.
    "sqlalchemy.dialects.sqlite",
    "services.models",
    # Imported inside function bodies in services/hf_access.py.
    "huggingface_hub",
]

# ---------------------------------------------------------------------------
# Native packages needing explicit collection
#
# Each of these ships DLLs and/or data files that PyInstaller's module graph
# alone will not pick up:
#   ctranslate2   - ctranslate2.dll (~57 MB) + libiomp5md.dll
#   faster_whisper- the bundled Silero VAD ONNX model (config.FASTER_WHISPER_VAD_ENABLED)
#   onnxruntime   - capi/onnxruntime.dll next to the pybind extension
#   av            - av/ and av.libs/ MUST stay siblings; av/__init__.py calls
#                   os.add_dll_directory() on ../av.libs to find FFmpeg
#   sounddevice   - _sounddevice_data/portaudio-binaries/libportaudio64bit.dll
# ---------------------------------------------------------------------------
for package in ("ctranslate2", "faster_whisper", "onnxruntime", "av",
                "tokenizers", "sounddevice"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# ---------------------------------------------------------------------------
# Exclusions
#
# torch/torchaudio and the nvidia CUDA wheels are present in a development
# venv (pulled in transitively) but are NOT in requirements.txt. torch is
# reachable only from two `try: import torch` blocks in
# transcriber/local_backend.py that degrade silently. Including it would add
# ~2.5 GB. GPU support ships as a downloadable component instead.
#
# scipy was removed as a dependency; the exclusion keeps a stale venv from
# silently reintroducing it.
# ---------------------------------------------------------------------------
excludes = [
    "torch", "torchaudio", "torchgen", "functorch", "nvidia",
    "sympy", "networkx", "scipy",
    "matplotlib", "tkinter", "IPython", "notebook",
    "pytest", "_pytest", "pydoc_data",
    "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtQuick3D", "PyQt6.QtWebEngineCore",
    "PyQt6.QtMultimedia", "PyQt6.QtPdf", "PyQt6.QtDesigner", "PyQt6.QtBluetooth",
]

# Qt binaries and data with no path into this app. Matched case-insensitively
# against the destination name of each TOC entry.
#
# NOTE: opengl32sw.dll (19.7 MB) is deliberately KEPT. It is Qt's software
# OpenGL fallback, and while a pure-Widgets app normally never needs it, a
# machine with broken or absent GPU drivers can fail to start without it.
# ~6 MB compressed is cheap insurance against an app that will not launch.
QT_EXCLUDE_PATTERNS = (
    "qt6quick", "qt6qml", "qt6quick3d", "qt6quickcontrols", "qt6quickwidgets",
    "qt6pdf", "qt6designer", "qt6shadertools", "qt6webengine", "qt6webview",
    "qt6charts", "qt63d", "qt6datavisualization", "qt6multimedia",
    "qt6sensors", "qt6positioning", "qt6bluetooth", "qt6nfc", "qt6serialport",
    "qt6test", "qt6help", "qt6uitools", "qt6location", "qt6texttospeech",
    "qt6remoteobjects", "qt6scxml", "qt6statemachine", "qt6sql", "qt6spatialaudio",
    "qt6websockets", "qt6webchannel", "qt6labs", "qt6virtualkeyboard",
    # FFmpeg shipped for Qt Multimedia; the app uses sounddevice + PyAV instead.
    "avcodec-", "avformat-", "avutil-", "swscale-", "swresample-",
    "d3dcompiler_47",
    # Directory trees.
    "pyqt6/qt6/qml/", "pyqt6/qt6/translations/", "pyqt6/qt6/qsci/",
    "qt6/qml/", "qt6/translations/",
)


def _strip_qt(toc):
    """Drop unused Qt modules from a PyInstaller TOC.

    Only entries living under a Qt directory are considered. That scoping is
    load-bearing: PyAV ships its own FFmpeg build in ``av.libs/`` whose file
    names ("avcodec-62-<hash>.dll") match the avcodec-/avutil-/swscale-
    patterns intended for Qt Multimedia's copies. Matching on the bare name
    strips PyAV's DLLs too, and ``import av`` then fails at runtime with
    "DLL load failed while importing _core".

    Args:
        toc: List of (dest_name, source_path, typecode) tuples.

    Returns:
        The filtered TOC.
    """
    kept = []
    for entry in toc:
        name = entry[0].replace("\\", "/").lower()
        is_qt = "pyqt6/" in name or "/qt6/" in name or name.startswith("qt6/")
        if is_qt and any(pattern in name for pattern in QT_EXCLUDE_PATTERNS):
            continue
        kept.append(entry)
    return kept


a = Analysis(
    ["app_qt.py"],
    pathex=[str(REPO_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

a.binaries = _strip_qt(a.binaries)
a.datas = _strip_qt(a.datas)

pyz = PYZ(a.pure)


def _version_resource():
    """Build the Windows VERSIONINFO resource from _version.__version__."""
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable,
        VarFileInfo, VarStruct, VSVersionInfo,
    )

    parts = [int(p) for p in __version__.split(".")[:3]]
    while len(parts) < 4:
        parts.append(0)
    filevers = tuple(parts)

    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=filevers, prodvers=filevers,
                          mask=0x3F, flags=0x0, OS=0x40004,
                          fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "Fiori Labs"),
                StringStruct("FileDescription", "OpenWhisper speech-to-text"),
                StringStruct("FileVersion", __version__),
                StringStruct("InternalName", APP_NAME),
                StringStruct("LegalCopyright", "MIT License"),
                StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                StringStruct("ProductName", APP_NAME),
                StringStruct("ProductVersion", __version__),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-packed binaries are a common antivirus false-positive trigger
    console=False,
    disable_windowed_traceback=False,
    # asInvoker: the global hotkey hook (SetWindowsHookEx WH_KEYBOARD_LL) does
    # not need elevation, and requesting it would show a UAC prompt on every
    # launch and block clipboard interop with normal-integrity applications.
    uac_admin=False,
    uac_uiaccess=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_PATH) if ICON_PATH.exists() else None,
    version=_version_resource() if sys.platform == "win32" else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
