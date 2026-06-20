# PyInstaller spec for Caption Studio (onedir build).
#   build with:  pyinstaller --noconfirm caption_studio.spec
#   output:      dist/CaptionStudio/CaptionStudio(.exe)
#
# Bundles the ffmpeg binary that imageio-ffmpeg ships (needed for the waveform,
# thumbnails, and rotation/size probe). PySide6's own PyInstaller hook collects
# the Qt multimedia / FFmpeg playback plugins automatically.
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for _pkg in ("imageio_ffmpeg",):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

a = Analysis(
    ["caption_studio.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CaptionStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # windowed app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="docs/app.ico",    # add an .ico/.icns here if you make one
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CaptionStudio",
)
