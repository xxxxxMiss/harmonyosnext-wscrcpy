#!/usr/bin/env python3
"""M5 最小验证：华为 scrcpy server 全链路（推送→daemon→fport→gRPC 收流）。

验证目标：
1. so 能被 uitest 加载（start-daemon 成功）
2. gRPC onStart 能收到流，data 为 H.264（起始码 00 00 00 01 或 AVCC 长度前缀）
3. onEnd 返回帧数，设备端 /data/local/tmp/mytest.mp4 可 pull 且为有效视频
"""
import glob
import subprocess
import sys
import time

sys.path.insert(0, ".")
import grpc
from watchscrcpy.hdc import Hdc
from watchscrcpy.proto import scrcpy_pb2, scrcpy_pb2_grpc

SO_RES = ("/Applications/DevEco_Testing_for_App.app/Contents/Python/"
          "lib/python3.12/site-packages/devicetest/res/recorder")
REMOTE_SO = "/data/local/tmp/libscreen_recorder.z.so"
LOCAL_PORT = 27183          # 随机挑的本地转发端口
DEVICE_MP4 = "/data/local/tmp/mytest.mp4"


def sh(hdc, *args, timeout=20):
    out = hdc._run(*args, timeout=timeout).decode("utf-8", "replace")
    return out


def main():
    so_files = sorted(glob.glob(f"{SO_RES}/libscrcpy_server*.z.so"))
    print("可用 so:", [s.split("/")[-1] for s in so_files])
    from watchscrcpy.hdc import Hdc as _H
    serials = _H.list_devices()
    if not serials:
        print("[!] 未发现设备"); return 1
    hdc = Hdc(serial=serials[0])
    print("hdc:", hdc.hdc_path, "| 设备:", hdc.serial)

    so = sys.argv[1] if len(sys.argv) > 1 else so_files[-1]   # 默认序号最大（最新）
    print(f"\n[1] 推送 {so.split('/')[-1]}")
    hdc.shell("rm", "-f", REMOTE_SO, timeout=10)
    hdc.send(so, REMOTE_SO)

    print("[2] 启动 uitest daemon（整条命令传参，避免 hdc 吞掉 -p/-m）")
    hdc.shell("/system/bin/uitest start-daemon singleness "
              "--extension-name libscreen_recorder.z.so -p 5001 -m 1 -screenId 0",
              timeout=15)
    time.sleep(2)
    # 确认监听形态：TCP 5001 还是 abstract socket
    net = hdc.shell("netstat", "-ltn", timeout=10)
    tcp_ok = "5001" in net
    unix = hdc.shell("cat", "/proc/net/unix", timeout=10)
    unix_ok = "screen_record_grpc_socket" in unix
    print(f"    TCP 5001 监听: {tcp_ok} | abstract socket: {unix_ok}")
    if not (tcp_ok or unix_ok):
        print("    [!] 两种监听都没有，daemon 可能启动失败")
        return 1

    print("[3] 端口转发")
    try:
        sh(hdc, "fport", "rm", f"tcp:{LOCAL_PORT}")           # 清旧转发（失败忽略）
    except Exception:
        pass
    fwd = "tcp:5001" if tcp_ok else f"localabstract:screen_record_grpc_socket"
    out = sh(hdc, "fport", f"tcp:{LOCAL_PORT}", fwd)
    print("   ", out.strip()[:80])

    print("[4] gRPC onStart 收流（收 5 秒）")
    frames = []
    channel = grpc.insecure_channel(f"127.0.0.1:{LOCAL_PORT}",
                                    options=[("grpc_max_receive_message_length", 10485760)])
    stub = scrcpy_pb2_grpc.ScrcpyServiceStub(channel)
    t0 = time.time()
    try:
        stream = stub.onStart(scrcpy_pb2.Empty(), timeout=10)
        for msg in stream:
            data = msg.data.encode("latin-1", "replace") if isinstance(msg.data, str) else msg.data
            frames.append((msg.reply_type, len(data), data[:8].hex()))
            if time.time() - t0 > 5 or len(frames) >= 60:
                break
    except grpc.RpcError as e:
        print("    gRPC 错误:", e.code(), e.details())
        return 1
    print(f"    收到 {len(frames)} 条消息，样例(前5):")
    for rt, size, head in frames[:5]:
        print(f"      reply_type={rt} size={size} head={head}")

    print("[5] onEnd 停止")
    try:
        r = stub.onEnd(scrcpy_pb2.Empty(), timeout=10)
        print(f"    帧数 result = {r.result}")
    except grpc.RpcError as e:
        print("    onEnd 错误:", e.code())

    time.sleep(1)
    print("[6] pull 设备端 mp4")
    sh(hdc, "file", "recv", DEVICE_MP4, "/tmp/m5_test.mp4")
    import os
    if os.path.exists("/tmp/m5_test.mp4"):
        print(f"    /tmp/m5_test.mp4 = {os.path.getsize('/tmp/m5_test.mp4')} 字节")
    sh(hdc, "fport", "rm", f"tcp:{LOCAL_PORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
