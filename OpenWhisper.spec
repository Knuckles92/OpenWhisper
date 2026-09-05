# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir specification for OpenWhisper on Windows, Linux, and macOS.

Build on the target operating system with::

    pyinstaller --noconfirm --clean OpenWhisper.spec

PyInstaller does not cross-compile. The Windows tree is packed by Inno Setup;
the Linux tree is installed by the Debian and Arch packages built by
``scripts/build_installer.sh``; the macOS tree is wrapped as ``OpenWhisper.app``
by this spec and packed into a DMG by ``scripts/build_installer_macos.sh``.
``onedir`` avoids extracting the whole bundle on every launch and lets native
packages own ordinary files.
"""

import os
import sys
from importlib.metadata import distribution
from importlib.util import find_spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

REPO_ROOT = Path(SPECPATH).resolve()
sys.path.insert(0, str(REPO_ROOT))

from _version import __version__  # noqa: E402

APP_NAME = "OpenWhisper"
# Permanent reverse-DNS identity for the macOS app bundle and TCC grants.
MACOS_BUNDLE_IDENTIFIER = "tech.fiorilabs.openwhisper"
# Meeting Mode and the current arm64 wheel set establish Sonoma as the floor.
MACOS_MINIMUM_SYSTEM_VERSION = "14.0"
ICON_PATH = REPO_ROOT / "ui_qt" / "assets" / "openwhisper.ico"
ICNS_CANDIDATES = (
    REPO_ROOT / "build" / "macos" / "openwhisper.icns",
    REPO_ROOT / "ui_qt" / "assets" / "openwhisper.icns",
)

# Bundled read-only assets
#
# Relative layout is preserved because config.bundle_root() resolves assets as
# <root>/ui_qt/styles/theme.qss, where <root> is sys._MEIPASS when frozen.
def _distribution_license(package, suffix, destination):
    """Return one required wheel license as a PyInstaller data entry."""
    dist = distribution(package)
    normalized_suffix = suffix.replace("\\", "/").lower()
    for entry in dist.files or ():
        relative = str(entry).replace("\\", "/")
        if relative.lower().endswith(normalized_suffix):
            return (str(dist.locate_file(entry)), destination)
    raise RuntimeError(f"{package} wheel does not contain required license {suffix}")


# Keep the binding and toolkit terms beside every frozen binary. PyQt6 and Qt
# use different licenses and ship their texts in separate wheel metadata trees,
# which PyInstaller does not otherwise retain.
datas = [
    ("services/local_asr", "services/local_asr"),
    (str(REPO_ROOT / "ui_qt" / "styles" / "theme.qss"), "ui_qt/styles"),
    (str(REPO_ROOT / "ui_qt" / "assets"), "ui_qt/assets"),
    (str(REPO_ROOT / "webui" / "dist"), "webui/dist"),
    (str(REPO_ROOT / "docs" / "linux-system-audio.md"), "docs"),
    (str(REPO_ROOT / "THIRD_PARTY_NOTICES.md"), "."),
    _distribution_license("PyQt6", "licenses/LICENSE", "third_party_licenses/PyQt6"),
    _distribution_license("PyQt6-Qt6", ".dist-info/LICENSE", "third_party_licenses/Qt"),
]
binaries = []
hiddenimports = [
    # SQLAlchemy resolves dialects by name at runtime, so static analysis
    # cannot see this one.
    "sqlalchemy.dialects.sqlite",
    "services.models",
    # Imported inside function bodies in services/hf_access.py.
    "huggingface_hub",
    # Knowledge-folder extractors (imported lazily from meeting/context_folder).
    "pypdf",
    "docx",
    "pptx",
    "openpyxl",
    "lxml",
]

# services/credentials.py chooses one explicit OS backend at runtime rather
# than allowing keyring entry-point discovery. Keep the matching backend in
# each native bundle without dragging the other platforms' implementations in.
if sys.platform == "win32":
    hiddenimports.append("keyring.backends.Windows")
elif sys.platform == "darwin":
    hiddenimports.append("keyring.backends.macOS")
    # Lazy Meeting Mode / Accessibility imports must survive freeze analysis.
    hiddenimports.extend(
        (
            "objc",
            "Foundation",
            "HIServices",
            "ScreenCaptureKit",
            "CoreMedia",
            "Quartz",
            "ApplicationServices",
            "CoreText",
            "Cocoa",
        )
    )
else:
    hiddenimports.append("keyring.backends.SecretService")

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
_COLLECT_PACKAGES = [
    "ctranslate2",
    "faster_whisper",
    "onnxruntime",
    "av",
    "tokenizers",
    "sounddevice",
    "lxml",
    "uvicorn",
    "soundcard",
    # Plugin-style packages whose platform implementations are selected at
    # runtime. SecretStorage/jeepney back Linux's explicit keyring backend;
    # pynput selects its Xorg implementation dynamically.
    "keyring",
    "secretstorage",
    "jeepney",
    "pynput",
]
if sys.platform == "darwin":
    # ScreenCaptureKit, Accessibility (HIServices), and pynput's Darwin stack
    # are imported lazily. collect_all keeps their framework bindings.
    _COLLECT_PACKAGES.extend(
        (
            "objc",
            "Foundation",
            "Cocoa",
            "CoreMedia",
            "Quartz",
            "ScreenCaptureKit",
            "ApplicationServices",
            "CoreText",
            "HIServices",
        )
    )
for package in _COLLECT_PACKAGES:
    # Platform markers intentionally leave Windows-only or Linux-only packages
    # absent. collect_all() warns for a missing package, so skip it cleanly.
    if find_spec(package) is None:
        continue
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden


def _qt_icu_binaries():
    """Collect the ICU DLLs ``Qt6Core`` imports as ``icuuc.dll``.

    PyQt6 6.11 wheels omit these files even though Qt6Core's import table
    names ``icuuc.dll``. Prefer copies already sitting in ``Qt6/bin``; if
    the wheel has none, take the Windows 10+ system ICU so a frozen
    onedir does not depend on a restricted DLL search path.
    """
    collected = []
    try:
        import PyQt6
        qt_bin = Path(PyQt6.__file__).resolve().parent / "Qt6" / "bin"
    except Exception:
        return collected

    names = ("icuuc.dll", "icuin.dll", "icudt.dll", "icu.dll")
    found = {}
    for name in names:
        src = qt_bin / name
        if src.is_file():
            found[name] = src

    if "icuuc.dll" not in found and sys.platform == "win32":
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        for name in names:
            src = system32 / name
            if name not in found and src.is_file():
                found[name] = src

    for name, src in found.items():
        collected.append((str(src), "PyQt6/Qt6/bin"))
    return collected


binaries += _qt_icu_binaries()

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
    "qt6pdf", "libqpdf", "qt6designer", "qt6shadertools", "qt6webengine", "qt6webview",
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
        # SYMLINK entries may live at the bundle root while pointing into Qt
        # (for example libQt6Pdf.so.6 -> PyQt6/Qt6/lib/libQt6Pdf.so.6).
        # Inspect both names so filtering the target cannot leave a dangling
        # root symlink behind. Source scoping still protects PyAV's av.libs.
        names = [str(value).replace("\\", "/").lower() for value in entry[:2]]
        is_qt = any(
            "pyqt6/" in name or "/qt6/" in name or name.startswith("qt6/")
            for name in names
        )
        if is_qt and any(
            pattern in name for name in names for pattern in QT_EXCLUDE_PATTERNS
        ):
            continue
        kept.append(entry)
    return kept


# Build-host libstdc++ / libgcc_s must not ship in the Linux bundle. Arch's
# mesa and libglvnd are compiled against a newer GCC C++ ABI; if the older
# copies sit on LD_LIBRARY_PATH first, loading a system GL/EGL driver dies
# with missing GLIBCXX symbols. GCC's C++ ABI is backward compatible, so
# binaries frozen on Ubuntu 22.04 still run against a newer system runtime.
def _is_system_cxx_lib(name):
    """Return whether *name* is a libstdc++ or libgcc_s SONAME or symlink."""
    base = Path(str(name)).name.lower()
    return base.startswith("libstdc++.so") or base.startswith("libgcc_s.so")


def _strip_system_cxx(toc):
    """Leave libstdc++ and libgcc_s to the host OS on Linux.

    Args:
        toc: List of (dest_name, source_path, typecode) tuples.

    Returns:
        The filtered TOC. Unchanged on Windows and macOS.
    """
    if sys.platform != "linux":
        return toc
    return [entry for entry in toc if not _is_system_cxx_lib(entry[0])]


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

a.binaries = _strip_system_cxx(_strip_qt(a.binaries))
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


def _macos_icon_path():
    """Prefer a generated ICNS; fall back only when one is already present."""
    for candidate in ICNS_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def _macos_codesign_identity():
    """Return the optional Developer ID identity, or None for ad-hoc signing.

    The first public macOS app is intentionally ad-hoc signed and distributed
    in an unnotarized DMG. Setting ``OPENWHISPER_MACOS_CODESIGN_IDENTITY`` opts
    into a real identity later without changing the packaging layout.
    """
    identity = (os.environ.get("OPENWHISPER_MACOS_CODESIGN_IDENTITY") or "").strip()
    return identity or None


_macos_icon = _macos_icon_path() if sys.platform == "darwin" else None
_codesign_identity = _macos_codesign_identity() if sys.platform == "darwin" else None
_target_arch = "arm64" if sys.platform == "darwin" else None
_exe_icon = None
if sys.platform == "win32" and ICON_PATH.exists():
    _exe_icon = str(ICON_PATH)
elif sys.platform == "darwin" and _macos_icon is not None:
    _exe_icon = str(_macos_icon)

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
    target_arch=_target_arch,
    codesign_identity=_codesign_identity,
    # No App Sandbox and no permissive hardened-runtime entitlements for v1.
    entitlements_file=None,
    # Linux desktop environments take their icon from the installed PNG and
    # .desktop file; executable icon resources are a Windows/macOS concept.
    icon=_exe_icon,
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

if sys.platform == "darwin":
    if _macos_icon is None:
        raise SystemExit(
            "macOS freeze requires openwhisper.icns; run "
            "scripts/generate_icon.py on Darwin first"
        )
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=str(_macos_icon),
        bundle_identifier=MACOS_BUNDLE_IDENTIFIER,
        version=__version__,
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleName": APP_NAME,
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "CFBundlePackageType": "APPL",
            "LSMinimumSystemVersion": MACOS_MINIMUM_SYSTEM_VERSION,
            "NSHighResolutionCapable": True,
            "NSMicrophoneUsageDescription": (
                "OpenWhisper records microphone audio for local and cloud "
                "speech-to-text transcription."
            ),
            "NSAudioCaptureUsageDescription": (
                "OpenWhisper captures system audio in Meeting Mode so the "
                "other side of a call can appear in the transcript."
            ),
        },
    )
