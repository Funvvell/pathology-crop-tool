# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for 病理裁剪工具."""
import sys
from pathlib import Path

import site

block_cipher = None

# 定位 sdpc 包的 DLL 目录（搜索系统和用户 site-packages）
_sdpc_dll_src = None
for sp in [Path(sys.prefix) / "Lib/site-packages"] + [Path(p) for p in site.getsitepackages()]:
    test = sp / "sdpc" / "WINDOWS" / "dll"
    if test.exists():
        _sdpc_dll_src = str(test)
        break
if _sdpc_dll_src is None:
    test = Path(site.getusersitepackages()) / "sdpc" / "WINDOWS" / "dll"
    if test.exists():
        _sdpc_dll_src = str(test)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=(
        ([(_sdpc_dll_src, 'sdpc/WINDOWS/dll')] if _sdpc_dll_src else [])
        + [
            ('icon.ico', '.'),
            ('icon.svg', '.'),
            ('liver_portal_crop/theme.qss', 'liver_portal_crop'),
            ('liver_portal_crop/theme_light.qss', 'liver_portal_crop'),
            ('liver_portal_crop/arrow_up.png', 'liver_portal_crop'),
            ('liver_portal_crop/arrow_down.png', 'liver_portal_crop'),
        ]
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
