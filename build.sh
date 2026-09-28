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
# CI / 绿色构建兜底：直接用随仓库提交的 hdc
if { [ -z "${HDC_SRC:-}" ] || [ ! -f "${HDC_SRC:-/nonexistent}" ]; } && [ -f vendor/bin/hdc ]; then
    HDC_SRC=vendor/bin/hdc
fi
[ -n "${HDC_SRC:-}" ] && [ -f "$HDC_SRC" ] || { echo "未找到 hdc（设 HDC_BIN=路径）"; exit 1; }
# 同文件不拷（HDC_BIN=vendor/bin/hdc 时 cp -f x x 会报错终止 set -e 脚本）
if [ "$(realpath "$HDC_SRC" 2>/dev/null)" != "$(realpath vendor/bin/hdc 2>/dev/null)" ]; then
    cp -f "$HDC_SRC" vendor/bin/hdc
fi

# hdc 非单文件二进制：同目录动态库一并打包（libusb_shared 为链接依赖；
# libexternal_hdc 会把 hdc 切到旧版 external server，刻意不打包）
TC_DIR=$(dirname "$HDC_SRC")
# 只"跳过拷贝"不够：vendor/bin 里若留着**上一次构建**（或手工拷入）的旧 libexternal_hdc.dylib，
# hdc 仍会去 dlopen 它，dev 态每次调用都刷一屏
#   [F] uv_dlopen failed … code signature … not valid for use in process（Team ID 不一致）
# 看着像产品 bug，其实是残留文件。这里直接删掉，保证 vendor/bin 干净。
rm -f vendor/bin/libexternal_hdc.dylib
shopt -s nullglob
for dy in "$TC_DIR"/*.dylib; do
    case "$(basename "$dy")" in
        libexternal_hdc.dylib) echo "跳过 $dy（避免旧版 external server 劫持 5037）"; continue ;;
    esac
    cp -f "$dy" vendor/bin/
done
shopt -u nullglob
ls vendor/bin/*.dylib >/dev/null 2>&1 || echo "提示: 未发现 .dylib（无旧版外部模式依赖，通常正是期望行为）"

FFMPEG_SRC="${FFMPEG_BIN:-$(command -v ffmpeg || echo /opt/homebrew/bin/ffmpeg)}"
[ -f "$FFMPEG_SRC" ] || { echo "未找到 ffmpeg（设 FFMPEG_BIN=路径，或 brew install ffmpeg）"; exit 1; }
rm -f vendor/bin/ffmpeg          # 源二进制常为只读权限，须先删再拷
cp "$FFMPEG_SRC" vendor/bin/ffmpeg

cp scripts/caploop.sh vendor/data/caploop.sh
chmod +x vendor/bin/hdc vendor/bin/ffmpeg
echo "hdc:    $(file -b vendor/bin/hdc | cut -d, -f1,2)"
echo "ffmpeg: $(file -b vendor/bin/ffmpeg | cut -d, -f1,2)"

echo "== [2/3] PyInstaller 构建 =="
if ! "$PY" -m PyInstaller wscrcpy.spec --noconfirm 2>&1 | tee build-log.txt; then
    echo "PyInstaller 失败，日志尾部："; tail -40 build-log.txt; exit 1
fi

echo "== [3/3] ad-hoc 签名 =="
codesign --force --deep --sign - dist/Wscrcpy.app 2>/dev/null || echo "(跳过签名)"

echo "== [4/4] DMG 安装包 =="
rm -rf /tmp/dmg-staging Wscrcpy-macOS.dmg
# 上一次构建的 DMG 若还挂在 /Volumes/Wscrcpy*（Finder 里双击过没推出），
# 卷名被占用会让下面的 create 报 "目录非空"。只卸载**指向本仓库 DMG** 的挂载点。
for dev in $(hdiutil info 2>/dev/null \
        | awk -v p="$PWD/Wscrcpy-macOS.dmg" \
          '/^image-path/{img=$3} /^\/dev\/disk/{if (img==p) print $1}'); do
    echo "卸载上次构建的残留挂载 $dev"
    hdiutil detach "$dev" >/dev/null 2>&1 || true
done
mkdir -p /tmp/dmg-staging
cp -R dist/Wscrcpy.app /tmp/dmg-staging/
ln -s /Applications /tmp/dmg-staging/Applications   # 拖拽安装
hdiutil create -volname "Wscrcpy" -srcfolder /tmp/dmg-staging -ov -format UDZO \
    Wscrcpy-macOS.dmg >/dev/null
echo "完成: dist/Wscrcpy.app + Wscrcpy-macOS.dmg （双击 DMG 拖入 Applications 安装）"
