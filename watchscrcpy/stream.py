"""流畅模式配套：H.264 解码线程与流式录制。

解码：PyAV（ffmpeg）软解，Annex-B NALU 直喂，608x1344@37fps 实测余量充足。
渲染落后时丢最旧帧（花屏可持续到下一个 IDR，间隔 2s），队列恢复后自动追上。
录制：H.264 裸流直接落盘（零 CPU 开销），停止时 ffmpeg -c copy 封装 mp4
（无重编码，秒级完成）。
"""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
from typing import Callable, Optional

from . import resources
from .scrcpy_server import FLAG_CONFIG, FLAG_IDR

log = logging.getLogger(__name__)

ImageCallback = Callable[["object"], None]     # QImage（避免硬依赖 gui 导入循环）


class H264Decoder(threading.Thread):
    """独立解码线程：feed() 供流回调线程调用，解码出的 QImage 经 on_image 回调。

    on_image 在解码线程执行，消费方需自行跨线程投递（如 Qt Signal）。
    """

    def __init__(self, on_image: ImageCallback, on_status: Optional[Callable[[str], None]] = None,
                 queue_size: int = 24):
        super().__init__(daemon=True)
        self.on_image = on_image
        self.on_status = on_status or (lambda m: None)
        self._q: "queue.Queue[tuple[int, bytes]]" = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self.frames = 0

    def feed(self, flags: int, data: bytes):
        """流回调线程调用；队列满丢最旧，保证低延迟。"""
        try:
            self._q.put_nowait((flags, data))
        except queue.Full:
            try:
                self._q.get_nowait()          # 丢最旧
                self._q.put_nowait((flags, data))
            except (queue.Empty, queue.Full):
                pass

    def stop(self):
        self._stop.set()

    def run(self):
        import av
        from PySide6.QtGui import QImage
        cc = av.codec.CodecContext.create("h264", "r")
        while not self._stop.is_set():
            try:
                flags, data = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                for frame in cc.decode(av.Packet(data)):
                    arr = frame.to_ndarray(format="rgb24")
                    h, w, _ = arr.shape
                    img = QImage(arr.tobytes(), w, h, w * 3, QImage.Format_RGB888).copy()
                    self.frames += 1
                    self.on_image(img)
            except Exception as e:
                if not isinstance(e, subprocess.SubprocessError):
                    log.debug("decode err: %s", e)
                continue


class StreamRecorder:
    """H.264 裸流直写 + ffmpeg -c copy 封装 mp4（无重编码）。"""

    def __init__(self, out_path: str):
        self.out_path = os.path.abspath(out_path)
        self.raw_path = os.path.splitext(self.out_path)[0] + ".h264"
        self._f = None
        self.frames = 0
        self.bytes_in = 0

    @property
    def recording(self) -> bool:
        return self._f is not None

    def start(self, prefill: "list[bytes] | None" = None):
        """prefill：录制起始预填的字节（SPS/PPS + 最近 IDR），保证裸流可独立解码。"""
        self._f = open(self.raw_path, "wb")
        for b in (prefill or []):
            self._f.write(b)
        self.frames = 0
        self.bytes_in = 0

    def write(self, flags: int, data: bytes, pts: int = 0):
        if self._f is None:
            return
        self._f.write(data)
        self.frames += 1
        self.bytes_in += len(data)

    def stop(self) -> tuple:
        """返回 (kind, text)：kind ∈ composing / saved_frames / empty。"""
        if self._f is None:
            return ("empty", "没有进行中的录制")
        self._f.close()
        self._f = None
        if self.frames == 0:
            try:
                os.remove(self.raw_path)
            except OSError:
                pass
            return ("empty", "录制期间未捕获到任何帧")
        ff = resources.find_ffmpeg()
        if not ff:
            return ("saved_frames", f"未找到 ffmpeg，H.264 裸流已保留: {self.raw_path}")
        try:
            r = subprocess.run([ff, "-y", "-v", "error", "-f", "h264", "-r", "30",
                                "-i", self.raw_path,
                                "-c", "copy", "-movflags", "+faststart", self.out_path],
                               capture_output=True, text=True, timeout=120,
                               creationflags=resources.subprocess_flags())
            if r.returncode != 0:
                return ("saved_frames", f"mp4 封装失败（裸流保留 {self.raw_path}）：{r.stderr[-200:]}")
            os.remove(self.raw_path)
            return ("done", self.out_path)          # copy 封装即时完成，文件已就绪
        except Exception as e:
            return ("saved_frames", f"mp4 封装异常（裸流保留 {self.raw_path}）：{e}")
