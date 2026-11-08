# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from pathlib import Path
import re

source = Path('windowsdrop.py').read_text(encoding='utf-8')
match = re.search(r'^VERSION = "(\d+)\.(\d+)\.(\d+)"$', source, re.M)
if not match:
    raise RuntimeError('windowsdrop.py içindeki VERSION alanı x.y.z biçiminde olmalı.')
version = match.groups()
version_tuple = ', '.join((*version, '0'))
version_info = Path('build') / 'WindowsDrop.version'
version_info.parent.mkdir(parents=True, exist_ok=True)
version_info.write_text(
    f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({version_tuple}),
    prodvers=({version_tuple}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('FileDescription', 'WindowsDrop photo and video transfer'),
        StringStruct('FileVersion', '{'.'.join(version)}'),
        StringStruct('InternalName', 'WindowsDrop'),
        StringStruct('OriginalFilename', 'WindowsDrop.exe'),
        StringStruct('ProductName', 'WindowsDrop'),
        StringStruct('ProductVersion', '{'.'.join(version)}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)""",
    encoding='utf-8',
)

hiddenimports = ['pystray._win32', 'qrcode.image.pil']
hiddenimports += collect_submodules('webview')


a = Analysis(
    ['windowsdrop.py'],
    pathex=[],
    binaries=[],
    datas=[('icon-512.png', '.'), ('apple-touch-icon.png', '.')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='WindowsDrop',
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
    icon=['icon.ico'],
    version=str(version_info),
)
