# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：macOS 出 Wscrcpy.app，Windows 出 dist/Wscrcpy/Wscrcpy.exe。

vendor/bin/{hdc,ffmpeg}[.exe] 与 vendor/data/caploop.sh 由 build.sh / build.ps1 备齐；
ffmpeg 的动态库依赖由 PyInstaller 二进制依赖分析自动收集。
PyInstaller 不支持交叉编译：mac 包在 mac 构建，Windows 包在 Windows/CI 构建。
"""
import glob
import os
import platform

block_cipher = None

IS_WIN = os.name == "nt"
EXE = ".exe" if IS_WIN else ""

binaries = [
    ("vendor/bin/hdc" + EXE, "bin"),
    ("vendor/bin/ffmpeg" + EXE, "bin"),
]
if not IS_WIN:
    # macOS：hdc 的 dylib 需落在 Frameworks 根（其 rpath 为 @executable_path/../），
    # 同时在 bin/ 旁留一份；libexternal_hdc 会切到旧版 external server，刻意排除
    for d in glob.glob("vendor/bin/*.dylib"):
        if "libexternal_hdc" in d:
            continue
        binaries += [(d, "."), (d, "bin")]
else:
    # Windows：hdc.exe 的伴随 DLL 直接放 exe 旁（DLL 搜索含 exe 所在目录）
    binaries += [(d, "bin") for d in glob.glob("vendor/bin/*.dll")]

datas = [
    ("vendor/data/caploop.sh", "data"),
]

a = Analysis(
    ["wscrcpy.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Wscrcpy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI 应用：无控制台
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Wscrcpy",
)

if platform.system() == "Darwin":
    app = BUNDLE(
        coll,
        name="Wscrcpy.app",
        bundle_identifier="dev.wscrcpy.app",
        info_plist={
            "CFBundleName": "Wscrcpy",
            "CFBundleDisplayName": "Wscrcpy",
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
