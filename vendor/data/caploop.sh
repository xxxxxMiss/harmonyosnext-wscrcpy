#!/bin/sh
# caploop.sh —— 手表端后台连拍（路线2 模式B，实验性）
# 用法: sh caploop.sh [sleep_sec]   （由 LoopCapture 经 hdc 推送并后台拉起）
# 环形缓冲 40 帧: f000000.jpeg ~ f000039.jpeg，序号单调递增写入 seq 文件，
# PC 轮询 seq 只拉新帧；toybox sh 兼容性需真机验证，不支持则退回 PC 逐帧驱动。
DIR=/data/local/tmp/wscrcpy
SLEEP=${1:-0.2}
mkdir -p "$DIR" || exit 1

echo $$ > "$DIR/pid"
seq=0
echo 0 > "$DIR/seq"

# snapshot_display 是否在后台 shell 的 PATH 中需真机确认，
# 失败时尝试常见绝对路径后放弃。
snap() {
    if command -v snapshot_display >/dev/null 2>&1; then
        snapshot_display -f "$DIR/tmp.jpeg"
    elif [ -x /system/bin/snapshot_display ]; then
        /system/bin/snapshot_display -f "$DIR/tmp.jpeg"
    else
        exit 2
    fi
}

while [ "$seq" -lt 999999 ]; do
    snap || exit 2
    seq=$((seq + 1))
    # mv 保证 PC 不会拉到写了一半的目标文件
    mv "$DIR/tmp.jpeg" "$(printf '%s/f%06d.jpeg' "$DIR" $((seq % 40)))"
    echo "$seq" > "$DIR/seq"
    # toybox sleep 支持小数秒；个别固件不支持时退化为 1 秒节拍
    sleep "$SLEEP" 2>/dev/null || sleep 1
done
