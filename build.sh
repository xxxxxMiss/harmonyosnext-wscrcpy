#!/bin/bash
# build.sh —— 一键构建 Wscrcpy.app（macOS）
# 用法: ./build.sh
# 可覆盖: HDC_BIN=... FFMPEG_BIN=... PYTHON=/path/to/python3
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
echo "== [1/3] 备齐 vendor 资源 =="
mkdir -p vendor/bin vendor/data

HDC_SRC="${HDC_BIN:-}"
if [ -z "$HDC_SRC" ]; then
    # 版本感知：优先 OpenHarmony Sdk（新→旧），再 hmscore，再 DevEco Studio 内置；
    # 旧版 hdc 会被升级过的手机拒绝（E000001），必须取最新
    HDC_SRC=$(ls -d "$HOME/Library/OpenHarmony/Sdk/"*/toolchains/hdc 2>/dev/null \
        | awk -F'/Sdk/' '{split($2,a,"/"); n=a[1]; gsub(/\./,".",n); print n+0, $0}' \
        | sort -n | awk '{print $2}' | tail -1)
fi
if [ -z "$HDC_SRC" ]; then
    HDC_SRC=$(ls -d "$HOME/Library/Huawei/Sdk/hmscore/"*/toolchains/hdc 2>/dev/null \
        | awk -F'/hmscore/' '{split($2,a,"/"); print a[1]+0, $0}' | sort -n | awk '{print $2}' | tail -1)
fi
[ -n "$HDC_SRC" ] && [ -f "$HDC_SRC" ] || HDC_SRC="$(command -v hdc || true)"
[ -n "${HDC_SRC:-}" ] && [ -f "$HDC_SRC" ] || { echo "未找到 hdc（设 HDC_BIN=路径）"; exit 1; }
cp -f "$HDC_SRC" vendor/bin/hdc

# hdc 非单文件二进制：同目录动态库一并打包（libusb_shared 为链接依赖；
# libexternal_hdc 会把 hdc 切到旧版 external server，刻意不打包）
TC_DIR=$(dirname "$HDC_SRC")
for dy in "$TC_DIR"/*.dylib; do
    case "$(basename "$dy")" in
        libexternal_hdc.dylib) echo "跳过 $dy（避免旧版 external server 劫持 5037）"; continue ;;
    esac
    [ -f "$dy" ] && cp -f "$dy" vendor/bin/
done
[ -n "$(ls vendor/bin/*.dylib 2>/dev/null)" ] || echo "警告: hdc 目录未发现 .dylib，若目标机报 Library not loaded 需手动补"

FFMPEG_SRC="${FFMPEG_BIN:-$(command -v ffmpeg || echo /opt/homebrew/bin/ffmpeg)}"
[ -f "$FFMPEG_SRC" ] || { echo "未找到 ffmpeg（设 FFMPEG_BIN=路径，或 brew install ffmpeg）"; exit 1; }
rm -f vendor/bin/ffmpeg          # 源二进制常为只读权限，须先删再拷
cp "$FFMPEG_SRC" vendor/bin/ffmpeg

cp scripts/caploop.sh vendor/data/caploop.sh
chmod +x vendor/bin/hdc vendor/bin/ffmpeg
echo "hdc:    $(file -b vendor/bin/hdc | cut -d, -f1,2)"
echo "ffmpeg: $(file -b vendor/bin/ffmpeg | cut -d, -f1,2)"

echo "== [2/3] PyInstaller 构建 =="
"$PY" -m PyInstaller wscrcpy.spec --noconfirm

echo "== [3/3] ad-hoc 签名 =="
codesign --force --deep --sign - dist/Wscrcpy.app 2>/dev/null || echo "(跳过签名)"

echo "完成: dist/Wscrcpy.app （双击运行，或 open dist/Wscrcpy.app）"
