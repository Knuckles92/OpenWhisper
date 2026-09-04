# -*- mode: python ; coding: utf-8 -*-
"""One-file PyInstaller spec for the windowless native updater helper."""

import sys
from pathlib import Path

if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

REPO_ROOT = Path(SPECPATH).resolve()
sys.path.insert(0, str(REPO_ROOT))

from _version import __version__  # noqa: E402

APP_NAME = "OpenWhisperUpdater"
ICON_PATH = REPO_ROOT / "ui_qt" / "assets" / "openwhisper.ico"


def _version_resource():
    parts = [int(p) for p in __version__.split(".")[:3]]
    while len(parts) < 4:
        parts.append(0)
    filevers = tuple(parts)
    return VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=filevers,
            prodvers=filevers,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("CompanyName", "Fiori Labs"),
                            StringStruct("FileDescription", "OpenWhisper updater"),
                            StringStruct("FileVersion", __version__),
                            StringStruct("InternalName", APP_NAME),
                            StringStruct("LegalCopyright", "MIT License"),
                            StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                            StringStruct("ProductName", "OpenWhisper"),
                            StringStruct("ProductVersion", __version__),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


a = Analysis(
    ["scripts/updater_helper.py"],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=["services.app_update_apply", "services.update_contract", "services.setup_update", "services.update_data"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PyQt6",
        "tkinter",
        "unittest",
        "pytest",
        "numpy",
        "sounddevice",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=False,
    uac_uiaccess=False,
    icon=str(ICON_PATH) if ICON_PATH.exists() else None,
    version=_version_resource() if sys.platform == "win32" else None,
)
