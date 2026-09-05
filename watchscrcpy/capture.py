"""拉帧管线：投屏与录制共享同一条 Frame 流。

模式A PulledCapture（默认，已验证模式）：PC 逐帧驱动，删旧→触发→轮询→recv。
模式B LoopCapture（实验性）：caploop.sh 推上手表后台连拍（环形缓冲），PC 轮询
    序号文件只拉新帧；依赖 toybox sh 能力，真机验证不通过则用模式A。

所有模式产出统一的 Frame（含黑帧标记），黑帧/隐私页不中断循环。
"""
from __future__ import annotations

import io
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from PIL import Image, ImageFile

from .hdc import Hdc, HdcError, TMP_DIR

ImageFile.LOAD_TRUNCATED_IMAGES = True  # 轮询窗口内可能拉到半截 JPEG，能解就解


@dataclass
class Frame:
    jpeg: Optional[bytes]
    image: Optional[Image.Image]
    t: float                      # 相对采集起点的秒
    dark: bool = False            # 疑似黑帧/隐私页
    error: Optional[str] = None   # 本帧采集失败原因（帧本身为空）


FrameCallback = Callable[[Frame], None]


def _decode(data: bytes) -> tuple:
    """解码并做黑帧判定（最高灰度 < 16 视为疑似黑帧/隐私页）。"""
    im = Image.open(io.BytesIO(data))
    im.load()
    dark = im.convert("L").getextrema()[1] < 16
    return im, dark


class BaseCapture(threading.Thread):
    """后台采集线程。on_frame 在采集线程被调用，消费方需自行保证线程安全。"""

    def __init__(self, hdc: Hdc, interval: float, on_frame: FrameCallback,
                 on_status: Optional[Callable[[str], None]] = None,
                 on_lost: Optional[Callable[[], None]] = None):
        super().__init__(daemon=True)
        self.hdc = hdc
        self.interval = interval
        self.on_frame = on_frame
        self.on_status = on_status or (lambda msg: None)
        self.on_lost = on_lost or (lambda: None)   # 采集因设备失联等原因终止时回调
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def _emit(self, jpeg: Optional[bytes], t: float, err: Optional[str] = None) -> None:
        try:
            im, dark = _decode(jpeg) if jpeg else (None, False)
        except Exception:
            im, dark = None, False
            if not err:
                err = "JPEG 解码失败（可能拉到半截文件）"
        self.on_frame(Frame(jpeg=jpeg, image=im, t=t, dark=dark, error=err))


class PulledCapture(BaseCapture):
    """模式A：PC 逐帧驱动。"""

    def __init__(self, hdc: Hdc, interval: float, on_frame: FrameCallback, **kw):
        super().__init__(hdc, interval, on_frame, **kw)
        # 远端路径按进程隔离：多实例同时投屏时互不干扰
        self.remote = f"{TMP_DIR}/f{os.getpid()}.jpeg"

    def run(self) -> None:
        t0 = time.monotonic()
        fails = 0
        while not self._stop_evt.is_set():
            tic = time.monotonic()
            try:
                data = self.hdc.grab_frame(self.remote)
                fails = 0
                self._emit(data, time.monotonic() - t0)
            except Exception as e:          # 兜底：非预期异常也显示到状态栏，不让线程无声死亡
                fails += 1
                self._emit(None, time.monotonic() - t0, err=f"{type(e).__name__}: {e}")
                if fails >= 5:                      # 连续失败大概率是设备掉了
                    self.on_status("连续 5 帧失败，采集停止。请检查 USB/授权后重试")
                    self.on_lost()
                    break
            remain = self.interval - (time.monotonic() - tic)
            if remain > 0:
                self._stop_evt.wait(remain)


class LoopCapture(BaseCapture):
    """模式B（实验性）：手表端 caploop.sh 连拍，PC 只拉新帧。"""

    def __init__(self, hdc: Hdc, interval: float, on_frame: FrameCallback, **kw):
        super().__init__(hdc, interval, on_frame, **kw)
        from . import resources
        self.script_local = resources.find_caploop_script()
        self.remote_dir = TMP_DIR
        self.last_seq = 0

    def start_device_loop(self) -> None:
        """推送脚本并后台启动；返回前确认 seq 文件已在增长。"""
        self.hdc.shell("mkdir", "-p", self.remote_dir, timeout=10)
        self.hdc.send(self.script_local, f"{self.remote_dir}/caploop.sh")
        sleep_s = max(0.05, round(self.interval, 3))
        # nohup 不可用时直接 & 兜底；会话断开后子进程是否存活需真机验证
        self.hdc.shell("nohup", "sh", f"{self.remote_dir}/caploop.sh", str(sleep_s),
                       ">/dev/null", "2>&1", "&", timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self._read_seq() > 0:
                return
            time.sleep(0.2)
        raise HdcError("caploop 5s 内未产出任何帧（检查 scripts/caploop.sh 是否被 toybox sh 支持）")

    def stop_device_loop(self) -> None:
        try:
            self.hdc.shell("kill", "$(cat %s/pid 2>/dev/null)" % self.remote_dir, timeout=10)
        except HdcError:
            pass

    def _read_seq(self) -> int:
        try:
            out = self.hdc.shell("cat", f"{self.remote_dir}/seq", timeout=10).strip()
            return int(out) if out.isdigit() else 0
        except (HdcError, ValueError):
            return 0

    def run(self) -> None:
        t0 = time.monotonic()
        try:
            self.start_device_loop()
        except HdcError as e:
            self.on_status(f"caploop 启动失败：{e}。请退回 --mode pull")
            self.on_lost()
            return
        self.on_status("caploop 运行中（实验性）")
        while not self._stop_evt.is_set():
            tic = time.monotonic()
            cur = self._read_seq()
            # 环形缓冲 40 帧，一次落后超过缓冲即放弃中间帧
            for n in range(max(self.last_seq + 1, cur - 39), cur + 1):
                remote = f"{self.remote_dir}/f{n % 40:06d}.jpeg"
                local = os.path.join("/tmp", "wscrcpy_loop.jpeg")
                try:
                    self.hdc.recv(remote, local, timeout=15)
                    with open(local, "rb") as f:
                        data = f.read()
                    if len(data) > 100 and data[:2] == b"\xff\xd8":
                        self._emit(data, time.monotonic() - t0)
                except (HdcError, OSError):
                    pass  # 该帧可能正被覆盖，跳过
            self.last_seq = cur
            remain = max(0.05, self.interval / 2 - (time.monotonic() - tic))
            self._stop_evt.wait(remain)
        self.stop_device_loop()
