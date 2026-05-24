# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for LiverPortalCrop."""
import sys
from pathlib import Path

block_cipher = None

# 定位 sdpc 包的 DLL 目录
_sdpc_dll_src = (
    Path(sys.prefix)
    / "Lib/site-packages/sdpc/WINDOWS/dll"
)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=(
        [(str(_sdpc_dll_src), 'sdpc/WINDOWS/dll')]
        if _sdpc_dll_src.exists() else []
    ),
    hiddenimports=[
        'sdpc.Sdpc',
        'sdpc.Sdpc_struct',
        'PIL',
        'PIL.ImageQt',
        'tifffile',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='病理裁剪工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico' if Path('icon.ico').exists() else None,
)
