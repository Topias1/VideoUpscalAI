# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('upscale.py', '.'), ('upscaler', 'upscaler'), ('gui.py', '.'), ('logo.jpg', '.'), ('ffmpeg', '.'), ('ffprobe', '.')],
    hiddenimports=[],
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
    name='VideoUpscalAI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity='Developer ID Application: Antoine Weytens (K9KAJBBY5C)',
    entitlements_file=None,
    icon=['logo.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VideoUpscalAI',
)
app = BUNDLE(
    coll,
    name='VideoUpscalAI.app',
    icon='logo.icns',
    bundle_identifier='com.electrateam.VideoUpscalAI',
    codesign_identity='Developer ID Application: Antoine Weytens (K9KAJBBY5C)',
)
