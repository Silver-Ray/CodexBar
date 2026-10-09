# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT = Path(SPECPATH)

a = Analysis(
    ["codexbar.pyw"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "codexbar" / "web_assets"), "codexbar/web_assets"),
        (str(ROOT / "codexbar" / "assets"), "codexbar/assets"),
    ],
    hiddenimports=[
        "webview",
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
    ],
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
    [],
    exclude_binaries=True,
    name="CodexBar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    manifest=str(ROOT / "codexbar" / "app.manifest"),
    icon=str(ROOT / "codexbar" / "assets" / "codexbar.ico"),
    version=str(ROOT / "codexbar" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CodexBar",
)
