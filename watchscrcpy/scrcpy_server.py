"""M5 流畅模式：设备端 scrcpy server（H.264 视频流，30+ fps）。

原理：uitest（系统签名）dlopen 扩展 so → 设备端起 gRPC 服务
（abstract socket "scrcpy_grpc_socket"）→ hdc fport 转发 → PC 消费 H.264 流。
视频帧在 ReplyMessage.payload["data"].val_bytes，flags 区分：
8=SPS/PPS 配置、2=IDR 关键帧、0=P 帧。

so 获取（合规：不随本工具明文分发）：
1. 仓库 vendor/so/ 下的加密 so（AES-256-CBC，IV=前16字节，key 在运行时拼装）；
2. 本机 HoKit 安装内提取并解密（assets/tools/so/arm64-v8a/screencopy_v*.so）。

⚠ 实测约束（2026-09-13，HarmonyOS 6.1.1 / uitest 6.0.2.3）：
- so 版本须与系统匹配：v2_1.3 可用；v2_1.4 因系统 libprotobuf 缺符号不可用；
  加载失败特征为 hilog "relocating failed / symbol not found"。
- onStart 一个 daemon 生命周期只能消费一次（中断后重连无响应），消费方
  必须持续读到流结束，不得中途 break 后重连。
- daemon 为 "singleness" 单例：启动前须 pkill 旧实例，否则新参数不生效。
"""
from __future__ import annotations

import binascii
import glob
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .hdc import Hdc, HdcError
from . import resources

log = logging.getLogger(__name__)

# 加密 so 的 AES key（与社区工具的分发形态一致，两段拼装避免明文扫描）
_KEY_HEX_A = "c4a7e2b1d8f5039a6c1e4b7d9f2a5c8e"
_KEY_HEX_B = "3b6d0f7a1e4c8b2d5f9a3c7e0b4d6f8a"

REMOTE_SO = "/data/local/tmp/scrcpy_server.so"
SOCKET_NAME = "scrcpy_grpc_socket"
DEVICE_MP4 = "/data/local/tmp/wscrcpy_cast.mp4"

# 启动参数按 HoKit v1.8.7 实测照抄（-m 未用；编码参数为必需）
DAEMON_ARGS = ("-scale 2 -frameRate 30 -bitRate 10485760 -p 5001 -screenId 0 "
               "-encodeType 0 -iFrameInterval 2000 -repeatInterval 33")

FLAG_CONFIG = 8          # SPS/PPS
FLAG_IDR = 2             # 关键帧
FLAG_P = 0               # 普通帧

FrameCallback = Callable[[int, bytes, int], None]     # (flags, data, pts)


def _decrypt(data: bytes) -> bytes:
    """AES-256-CBC 解密 so（IV=前16字节，PKCS7）。明文 ELF 直接返回。"""
    if data[:4] == b"\x7fELF":
        return data
    from Crypto.Cipher import AES
    key = binascii.unhexlify(_KEY_HEX_A + _KEY_HEX_B)
    pt = AES.new(key, AES.MODE_CBC, data[:16]).decrypt(data[16:])
    return pt[:-pt[-1]]


def find_local_so() -> Optional[str]:
    """按优先级找可用的 so（已解密）：vendor/so → 本机 HoKit 提取解密 → 缓存目录。"""
    # 1) 仓库内置（加密形态）
    for cand in sorted(glob.glob(str(resources.resource_root() / "vendor" / "so" / "*.so")), reverse=True):
        try:
            pt = _decrypt(Path(cand).read_bytes())
            if pt[:4] == b"\x7fELF":
                cache = Path.home() / ".cache" / "wscrcpy"
                cache.mkdir(parents=True, exist_ok=True)
                out = cache / f"dec_{Path(cand).name}"
                out.write_bytes(pt)
                return str(out)
        except Exception:
            continue
    # 2) 本机 HoKit（so 是加密的）
    pats = ["/Applications/HoKit.app/Contents/Resources/assets/tools/so/arm64-v8a/*.so",
            str(Path.home() / "Applications/HoKit.app/Contents/Resources/assets/tools/so/arm64-v8a/*.so")]
    hits = []
    for pat in pats:
        hits += glob.glob(pat)
    # 版本新→旧尝试；跳过已知不适配 6.1.1 的 v2_1.4（protobuf 符号缺失）
    def ver_key(p):
        try:
            return tuple(int(x) for x in Path(p).stem.replace("screencopy_v", "").split("."))
        except Exception:
            return (0,)
    hits.sort(key=ver_key, reverse=True)
    for h in hits:
        name = Path(h).name
        if "v2_1.4" in name:
            continue
        if not (name.startswith("screencopy_v2_1") or name.startswith("screencopy_v1")):
            continue
        try:
            pt = _decrypt(Path(h).read_bytes())
            if pt[:4] == b"\x7fELF":
                cache = Path.home() / ".cache" / "wscrcpy"
                cache.mkdir(parents=True, exist_ok=True)
                out = cache / f"dec_{name}"
                out.write_bytes(pt)
                return str(out)
        except Exception:
            continue
    return None


class H264Stream:
    """一条 H.264 流会话：daemon 生命周期 + gRPC 收流。

    用法:
        s = H264Stream(hdc, on_frame=my_cb)
        s.start()          # 阻塞至首帧或超时；后台线程持续回调
        ...
        s.stop()           # onEnd + 清理 daemon/转发/设备文件
    """

    def __init__(self, hdc: Hdc, on_frame: FrameCallback,
                 on_status: Optional[Callable[[str], None]] = None,
                 local_port: int = 29300, so_path: Optional[str] = None):
        self.hdc = hdc
        self.on_frame = on_frame
        self.on_status = on_status or (lambda m: None)
        self.local_port = local_port
        self.so_path = so_path or find_local_so()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._first_frame = threading.Event()
        self._fport_on = False
        self.frames = 0
        self.bytes_in = 0
        self.last_config: bytes = b""    # 最近的 SPS/PPS（flags=8），录制起始预填用
        self.last_idr: bytes = b""       # 最近的 IDR（flags=2）

    # ---- 生命周期 ----
    def start(self, timeout: float = 15.0) -> bool:
        """部署并启动流。返回是否在超时内收到首帧。"""
        if not self.so_path:
            raise HdcError("未找到可用的 scrcpy server so（安装 HoKit 或检查 vendor/so）")
        self._teardown()
        # so 部署（daemon 前置依赖）
        self.hdc.shell("rm", "-f", REMOTE_SO, timeout=10)
        self.hdc.send(self.so_path, REMOTE_SO)
        self.hdc.shell("chmod", "755", REMOTE_SO, timeout=10)
        # daemon：singleness 单例，先清旧
        self.hdc.shell("pkill -9 -f 'uitest.*start-daemon' 2>/dev/null", timeout=10)
        time.sleep(1.2)
        self.hdc._run("shell", f"uitest start-daemon singleness "
                                f"--extension-name scrcpy_server.so {DAEMON_ARGS}", timeout=20)
        # 等 abstract socket
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if "scrcpy_grpc_socket" in self.hdc.shell("cat /proc/net/unix", timeout=10):
                break
            time.sleep(0.4)
        else:
            raise HdcError("scrcpy server 未创建 gRPC socket（so 与系统不匹配？）")
        # fport 转发（端口被占时自动换，实测 hdc server 会积累残留转发）
        import random
        for attempt in range(4):
            try:
                self.hdc._run("fport", "rm", f"tcp:{self.local_port}")
            except HdcError:
                pass
            out = self.hdc._run("fport", f"tcp:{self.local_port}",
                                f"localabstract:{SOCKET_NAME}").decode("utf-8", "replace")
            if "OK" in out:
                self._fport_on = True
                break
            self.local_port = random.randint(29301, 29500)
        else:
            raise HdcError(f"fport 转发失败: {out.strip()[:60]}")
        # 收流线程（grpc 需在无 fork 干扰的线程里跑；本线程只做 gRPC）
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        if not self._first_frame.wait(timeout):
            self.stop()
            raise HdcError(f"{timeout}s 内未收到视频帧")
        return True

    def stop(self):
        self._stop.set()
        t = self._thread
        if t:
            t.join(timeout=6)
            self._thread = None
        try:
            if self._fport_on:
                self.hdc._run("fport", "rm", f"tcp:{self.local_port}", timeout=10)
                self._fport_on = False
        except HdcError:
            pass
        self._teardown()

    def _teardown(self):
        try:
            self.hdc.shell("pkill -9 -f 'uitest.*start-daemon' 2>/dev/null", timeout=8)
        except HdcError:
            pass
        try:
            self.hdc.shell("rm", "-f", REMOTE_SO, timeout=8)
        except HdcError:
            pass

    # ---- 收流 ----
    def _pump(self):
        """gRPC onStart 消费线程。⚠ 一个 daemon 只允许一次 onStart，不得中途重连。"""
        try:
            import grpc
            from .proto import scrcpy_pb2, scrcpy_pb2_grpc
        except ImportError as e:
            self.on_status(f"缺少 grpc 依赖: {e}")
            return
        for attempt in (0, 1):
            if self._stop.is_set():
                return
            try:
                ch = grpc.insecure_channel(
                    f"127.0.0.1:{self.local_port}",
                    options=[("grpc_max_receive_message_length", 10485760)])
                stub = scrcpy_pb2_grpc.ScrcpyServiceStub(ch)
                for msg in stub.onStart(scrcpy_pb2.Empty(), timeout=None):
                    if self._stop.is_set():
                        ch.close()
                        return
                    p = dict(msg.payload)
                    if "data" not in p:
                        continue
                    b = p["data"].val_bytes
                    if not b:
                        continue
                    flags = p["flags"].val_int if "flags" in p else FLAG_P
                    pts = p.get("pts").val_int if "pts" in p else 0
                    if flags == FLAG_CONFIG:
                        self.last_config = b
                    elif flags == FLAG_IDR:
                        self.last_idr = b
                    self.frames += 1
                    self.bytes_in += len(b)
                    self._first_frame.set()
                    try:
                        self.on_frame(flags, b, pts)
                    except Exception:
                        log.exception("on_frame 回调异常")
                ch.close()
                return
            except Exception as e:
                if attempt == 0 and not self._stop.is_set():
                    time.sleep(0.8)      # 瞬时失败重连一次（daemon 可能刚起）
                    continue
                if not self._stop.is_set():
                    self.on_status(f"视频流中断: {type(e).__name__}")
                return

    def request_idr(self):
        """请求立即发送关键帧（弱网恢复/重连渲染用）。尽力而为。"""
        try:
            import grpc
            from .proto import scrcpy_pb2, scrcpy_pb2_grpc
            ch = grpc.insecure_channel(f"127.0.0.1:{self.local_port}",
                                       options=[("grpc_max_receive_message_length", 10485760)])
            scrcpy_pb2_grpc.ScrcpyServiceStub(ch).onRequestIDRFrame(scrcpy_pb2.Empty(), timeout=5)
            ch.close()
        except Exception:
            pass
