# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：macOS 出 Wscrcpy.app + DMG；Windows 出单文件 Wscrcpy.exe。

vendor/bin/{hdc,ffmpeg}[.exe] 与 vendor/data/caploop.sh 由 build.sh / build.ps1 备齐；
vendor/so 为加密形态的 scrcpy server so（运行时解密，不明文分发）。
PyInstaller 不支持交叉编译：mac 包在 mac 构建，Windows 包在 Windows/CI 构建。
"""
import glob
import os
import platform

block_cipher = None

IS_WIN = os.name == "nt"
EXE_SUFFIX = ".exe" if IS_WIN else ""   # 命名避开 PyInstaller 的 EXE() 全局

binaries = [
    ("vendor/bin/hdc" + EXE_SUFFIX, "bin"),
    ("vendor/bin/ffmpeg" + EXE_SUFFIX, "bin"),
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
    ("vendor/so", "vendor/so"),          # 加密形态的 scrcpy server（运行时解密）
]

a = Analysis(
    ["wscrcpy.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=["appdirs", "watchscrcpy.proto", "watchscrcpy.proto.scrcpy_pb2",
                   "watchscrcpy.proto.scrcpy_pb2_grpc"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if IS_WIN:
    # Windows：单文件形态（Release 直接分发 Wscrcpy.exe；代价是首启需解压到临时目录）
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="Wscrcpy",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,          # GUI 应用：无控制台
    )
else:
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
        console=False,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="Wscrcpy",
    )
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
